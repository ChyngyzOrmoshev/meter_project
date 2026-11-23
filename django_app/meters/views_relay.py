"""Управление реле: страница, API, массовые задачи, мониторинг очереди.

Вынесено из views.py. Маршруты подключены в urls.py через `views_relay`.
Бизнес-логика очереди — relay_queue.py, воркер — management/commands/process_relay_tasks.py.
"""
import os
import re
import json
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta

from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from .models import Device, Reading, Region, RelayTask, RelayCommandHistory, CompareResult
from .relay_queue import supersede_tasks, inflight_tasks, last_off_snapshot, READY_NOTE, is_ct_meter, CT_METER_TEXT
from .views import REVERSE_REGION_MAPPING


def can_manage_relay(user):
    return user.is_staff or user.groups.filter(name__in=['operator', 'Инженер АСКУЭ']).exists()


# Минимальная длина «хвоста» серийника для неточного поиска и серверный
# минимум времени до запланированного отключения (фронт требует 1 час,
# здесь запас на разницу часовых поясов браузера и сервера).
RELAY_MIN_SERIAL_SUFFIX = 5
RELAY_MIN_LEAD_MINUTES = 30

# Результат массовой загрузки по списку номеров (читает relay_bulk_status)
BULK_RESULT_PATH = '/app/exports/bulk_relay_result.json'


def find_relay_device(serial):
    """
    Единый поиск счётчика для управления реле: (device, error).
    Сначала точное совпадение; иначе — только однозначное совпадение по
    окончанию номера (без ведущих нулей, не короче RELAY_MIN_SERIAL_SUFFIX).
    Пустой или неоднозначный номер счётчик не выбирает.
    """
    serial = (serial or '').strip()
    if not serial:
        return None, 'Не указан серийный номер'

    device = Device.objects.filter(serial_number=serial).first()
    if device:
        return device, None

    suffix = serial.lstrip('0')
    if len(suffix) < RELAY_MIN_SERIAL_SUFFIX:
        return None, 'Счетчик не найден в базе'

    candidates = list(Device.objects.filter(serial_number__endswith=suffix)[:2])
    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) > 1:
        return None, 'Номер неоднозначен: найдено несколько счётчиков, укажите полный серийный номер'
    return None, 'Счетчик не найден в базе'


def relay_region_denied(user, device):
    """
    True, если пользователь ограничен районом (группа ASKUE:<район>) и счётчик
    не из его района. Счётчик без района для ограниченного пользователя закрыт.
    """
    user_region = get_user_region(user)
    if not user_region:
        return False
    return not (device.region and device.region.name == user_region)


@login_required
@user_passes_test(can_manage_relay)
def relay_mass_get_subscribers(request):
    """Поиск абонентов для массового отключения (через 1С)"""
    region = request.GET.get('region', '')
    min_debt = request.GET.get('min_debt', '0')
    
    # Проверка прав: инженер может искать только свой район
    user_region = get_user_region(request.user)
    if user_region and region != user_region:
        return JsonResponse({'error': f'У вас нет прав на просмотр района {region}'}, status=403)

    try:
        from meters.onec_service import OneCService
        service = OneCService()
        debtors = service.get_debtors(region, float(min_debt))
        
        found_items = []
        not_found_items = []
        
        for d in debtors:
            serial_1c = str(d.get('device__serial_number') or d.get('serial_number')).strip()
            norm_serial = re.sub(r'\D', '', serial_1c).lstrip('0')
            
            # Умный поиск в нашей базе (сначала точный, потом без ведущих нулей)
            device = Device.objects.filter(serial_number=serial_1c).first()
            if not device and norm_serial:
                device = Device.objects.filter(serial_number__endswith=norm_serial).first()

            if device:
                # Тип ПУ и ампераж: из ответа 1С, если она их вернула, иначе из карточки прибора
                dev_model = device.model
                found_items.append({
                    'id': device.id,
                    'serial': device.serial_number,
                    'model': d.get('model') or (dev_model.model_name if dev_model else '—'),
                    'amperage': d.get('amperage') or ((dev_model.nominal_current or '—') if dev_model else '—'),
                    'is_ct': is_ct_meter(device),
                    'account': d.get('account_number'),
                    'name': d.get('full_name'),
                    'debt': float(d.get('debt')),
                    'region': region
                })
            else:
                # Сохраняем сирот для передачи их обратно при создании задач
                not_found_items.append({
                    'serial': serial_1c,
                    'account': d.get('account_number'),
                    'name': d.get('full_name'),
                    'debt': float(d.get('debt'))
                })
        
        return JsonResponse({
            'found': found_items, 
            'not_found': not_found_items, 
            'total_found': len(found_items),
            'total_not_found': len(not_found_items)
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@user_passes_test(can_manage_relay)
def relay_mass_create_tasks(request):
    """Создание пачки задач на отключение/включение и заявок для сирот"""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
        
    device_ids = request.POST.getlist('device_ids[]')
    action = request.POST.get('action')
    scheduled_str = request.POST.get('scheduled_at')
    orphan_devices_json = request.POST.get('orphan_devices', '[]')
    region_name = request.POST.get('region_name', '')
    try:
        devices_info = json.loads(request.POST.get('devices_info', '{}'))
        if not isinstance(devices_info, dict):
            devices_info = {}
    except ValueError:
        devices_info = {}

    if action not in ('on', 'off'):
        return JsonResponse({'error': 'Неверное действие'}, status=400)

    # Время обязательно: молча подставлять «сейчас» нельзя — отключение
    # уйдёт на счётчики немедленно вместо запланированного времени.
    if not scheduled_str:
        return JsonResponse({'error': 'Не указано время выполнения'}, status=400)
    try:
        scheduled_at = parse_datetime(scheduled_str)
    except ValueError:
        scheduled_at = None
    if not scheduled_at:
        return JsonResponse({'error': 'Неверный формат даты и времени'}, status=400)
    if getattr(scheduled_at, 'tzinfo', None) is not None:
        scheduled_at = scheduled_at.replace(tzinfo=None)
    if scheduled_at < timezone.now() + timedelta(minutes=RELAY_MIN_LEAD_MINUTES):
        return JsonResponse(
            {'error': 'Время выполнения должно быть минимум через 1 час от текущего момента'},
            status=400
        )

    created_tasks = 0
    skipped_region = 0
    skipped_duplicate = 0
    skipped_ct = []
    # 1. Создаем обычные задачи
    for dev_id in dict.fromkeys(device_ids):  # без повторов, порядок сохраняется
        try:
            device = Device.objects.select_related('region', 'model').get(id=dev_id)
        except (Device.DoesNotExist, ValueError):
            continue

        # Инженер района не может ставить задачи на чужие счётчики
        if relay_region_denied(request.user, device):
            skipped_region += 1
            continue

        # Счётчики 5А (через ТТ) физически не отключаются: задачу не создаём
        if action == 'off' and is_ct_meter(device):
            skipped_ct.append(device.serial_number)
            continue

        # Такая же активная задача уже есть (например, двойной клик)
        if RelayTask.objects.filter(
            device=device, action=action, status__in=['pending', 'processing']
        ).exists():
            skipped_duplicate += 1
            continue

        # Отменяем ожидающие противоположные задачи; выполняющуюся не трогаем (новая пойдёт после неё)
        opposite_action = 'on' if action == 'off' else 'off'
        task_time, _busy = supersede_tasks(
            device, scheduled_at, 'Отменено из-за создания новой задачи', actions=(opposite_action,)
        )

        # Данные из 1С (пришли из формы) главнее локального Subscriber
        info = devices_info.get(str(device.id))
        info = info if isinstance(info, dict) else {}
        sub = getattr(device, 'subscriber', None)
        try:
            current_debt = Decimal(str(info['debt'])).quantize(Decimal('0.01'))
        except (KeyError, InvalidOperation, ValueError, TypeError):
            current_debt = sub.debt if sub else 0
        account = str(info.get('account') or '').strip()[:50] or (sub.account_number if sub else None)
        full_name = str(info.get('name') or '').strip()[:255] or (sub.full_name if sub else None)

        RelayTask.objects.create(
            device=device, action=action, scheduled_at=task_time,
            created_by=request.user, status='pending', debt_snapshot=current_debt,
            account_snapshot=account, name_snapshot=full_name,
        )
        created_tasks += 1

    # 2. Создаем заявки (CompareResult) для ненайденных
    created_issues = 0
    try:
        orphans = json.loads(orphan_devices_json)
        # Найдем объект Region для привязки
        region_obj = Region.objects.filter(name__icontains=region_name).first()
        
        for o in orphans:
            serial_1c = str(o.get('serial', '')).strip()
            if not serial_1c:
                continue
                
            # Проверяем, нет ли уже активной заявки на этот счетчик
            existing_issue = CompareResult.objects.filter(
                serial_1c=serial_1c, 
                action_type='not_found',
                status__in=['new', 'in_progress']
            ).exists()
            
            if not existing_issue:
                CompareResult.objects.create(
                    serial_1c=serial_1c,
                    account_1c=o.get('account'),
                    subscriber_1c=o.get('name'),
                    region=region_obj,
                    upload_region=region_obj,
                    action_type='not_found',
                    action_text=f"Счетчик не найден в АСКУЭ. Долг по 1С: {o.get('debt')}",
                    status='new',
                    check_date=timezone.now().date()
                )
                created_issues += 1
    except Exception as e:
        print(f"Ошибка при создании заявок для сирот: {e}")

    message = f'Успешно создано задач: {created_tasks}. Заявок на проверку: {created_issues}.'
    if skipped_duplicate:
        message += f' Пропущено (уже есть такая задача): {skipped_duplicate}.'
    if skipped_region:
        message += f' Пропущено (чужой район): {skipped_region}.'
    if skipped_ct:
        message += f' Не поставлено (счётчики 5А, физически не отключаются): {len(skipped_ct)}.'

    return JsonResponse({
        'skipped_ct': skipped_ct,
        'message': message,
        'count_tasks': created_tasks,
        'count_issues': created_issues,
        'skipped_duplicate': skipped_duplicate,
        'skipped_region': skipped_region,
    })

@login_required
@user_passes_test(can_manage_relay)
def relay_task_monitor_data(request):
    """Выгрузка задач для вкладки Мониторинг с фильтрами и пагинацией"""
    tasks = RelayTask.objects.select_related('device', 'device__subscriber', 'device__model', 'created_by').order_by('-scheduled_at')

    # --- ФИЛЬТРЫ ---
    # 1. Фильтр по периоду
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    
    # Если даты не переданы, по умолчанию ставим СЕГОДНЯ
    if not date_from and not date_to:
        today = timezone.now().date()
        date_from = today.strftime('%Y-%m-%d')
        date_to = today.strftime('%Y-%m-%d')

    if date_from:
        try:
            d_from = datetime.strptime(date_from, '%Y-%m-%d').date()
            tasks = tasks.filter(scheduled_at__date__gte=d_from)
        except ValueError:
            account, name = last_off_snapshot(t.device, before=t.id)
            
    if date_to:
        try:
            d_to = datetime.strptime(date_to, '%Y-%m-%d').date()
            tasks = tasks.filter(scheduled_at__date__lte=d_to)
        except ValueError:
            account, name = last_off_snapshot(t.device, before=t.id)

    # 2. Фильтр по району
    region = request.GET.get('region')
    if region:
        # Переводим "03 Сузак" в "Сузак ЭС" для поиска
        sub_region_name = REVERSE_REGION_MAPPING.get(region, region)
        
        # Ищем совпадения в карточке прибора (Device) ИЛИ в карточке абонента (Subscriber)
        tasks = tasks.filter(
            Q(device__region__name__icontains=region) |
            Q(device__region__name__icontains=sub_region_name) |
            Q(device__subscriber__region__icontains=region) |
            Q(device__subscriber__region__icontains=sub_region_name)
        )

    action = request.GET.get('action')
    if action:
        tasks = tasks.filter(action=action)

    status = request.GET.get('status')
    if status:
        tasks = tasks.filter(status=status)

    search = request.GET.get('search')
    if search:
        tasks = tasks.filter(
            Q(device__serial_number__icontains=search) |
            Q(device__subscriber__account_number__icontains=search)
        )

    # --- ПАГИНАЦИЯ ---
    page_num = request.GET.get('page', 1)
    page_size_str = request.GET.get('page_size', '20')
    
    try:
        page_size = int(page_size_str)
    except ValueError:
        page_size = 20

    # Ограничиваем максимальный размер страницы, чтобы не положить сервер
    if page_size not in [10, 20, 50, 100]:
        page_size = 20

    paginator = Paginator(tasks, page_size)
    
    try:
        page_obj = paginator.page(page_num)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    data = []
    for t in page_obj.object_list:
        sub = getattr(t.device, 'subscriber', None)
        # Данные из 1С при создании; для включения без них — из задачи на отключение; иначе Subscriber
        account, name = t.account_snapshot, t.name_snapshot
        if not account and t.action == 'on':
            account, name = last_off_snapshot(t.device, before=t.id)
        account = account or (sub.account_number if sub else None)
        name = name or (sub.full_name if sub else None)

        # Убираем sub.debt и используем t.debt_snapshot
        data.append({
            'id': t.id,
            'serial': t.device.serial_number,
            'model': t.device.model.model_name if t.device.model else '—',
            'amperage': (t.device.model.nominal_current or '—') if t.device.model else '—',
            'account': account or '—',
            'name': name or '—',
            'debt': float(t.debt_snapshot) if t.debt_snapshot is not None else 0, # <--- БЕРЕМ ИЗ ЗАДАЧИ
            'action': t.action,
            'action_display': t.get_action_display(),
            'status': t.status,
            'status_display': t.get_status_display(),
            'scheduled_at': t.scheduled_at.strftime('%Y-%m-%d %H:%M'),
            'user': t.created_by.username if t.created_by else 'Система',
            'error': t.error_message or '',
            'note': (t.error_message or '') if t.status == 'success' else '',
            'awaiting_button': t.status == 'success' and t.action == 'on' and t.error_message == READY_NOTE,
        })

    return JsonResponse({
        'items': data,
        'pagination': {
            'current_page': page_obj.number,
            'total_pages': paginator.num_pages,
            'total_items': paginator.count,
            'has_next': page_obj.has_next(),
            'has_previous': page_obj.has_previous(),
        }
    })

@login_required
@user_passes_test(lambda u: u.is_staff)
def relay_task_retry(request):
    """Переотправка упавшей задачи"""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
        
    task_id = request.POST.get('task_id')
    try:
        task = RelayTask.objects.get(id=task_id)
        if task.status == 'error':
            task.status = 'pending'
            task.scheduled_at = timezone.now()
            task.error_message = None
            # Иначе первая же новая ошибка сразу даст 'error' без повторов
            task.retry_count = 0
            task.hexing_payload = None
            task.save()
            return JsonResponse({'message': 'Задача возвращена в очередь'})
        return JsonResponse({'error': 'Можно переотправить только упавшие задачи'}, status=400)
    except RelayTask.DoesNotExist:
        return JsonResponse({'error': 'Задача не найдена'}, status=404)


# Вспомогательная функция для определения региона инженера
def get_user_region(user):
    if user.is_staff or user.is_superuser:
        return None  # Админы видят всё
    for group in user.groups.all():
        if group.name.startswith('ASKUE:'):
            return group.name.split('ASKUE:')[1].strip()
    return None

@login_required
@user_passes_test(can_manage_relay)
def relay_control_page(request):
    """Страница управления реле (только для админов и инженеров)"""
    from meters.models import Region
    
    user_region = get_user_region(request.user)
    
    if user_region:
        # Инженер видит только свой район
        regions = Region.objects.filter(name__icontains=user_region).order_by('name')
    else:
        # Админ видит все районы
        regions = Region.objects.all().order_by('name')
        
    return render(request, 'meters/relay_control.html', {
        'regions': regions,
        'user_region': user_region
    })


from .sunrise_service import SunRiseService
from .cenergo_service import CEnergoService
from .hexing_service import HexingService


@login_required
@user_passes_test(can_manage_relay)
def relay_api_status(request):
    # Тот же поиск, что и в relay_api_action: на экране и в команде один счётчик
    device, error = find_relay_device(request.GET.get('serial', ''))
    if not device:
        return JsonResponse({'error': error}, status=404)
    if relay_region_denied(request.user, device):
        return JsonResponse({'error': 'Счётчик не из вашего района'}, status=403)

    # Сбор информации о приборе (оставляем как было)
    last_reading = Reading.objects.filter(device=device).order_by('-timestamp').first()
    last_reading_date = last_reading.timestamp.strftime('%Y-%m-%d %H:%M:%S') if last_reading else None
    last_reading_val = float(last_reading.reading_value) if last_reading else None
    is_online = False
    if last_reading:
        is_online = (timezone.now() - last_reading.timestamp).days < 7

    # Собираем историю (для отображения в правой колонке)
    history_qs = RelayCommandHistory.objects.filter(device=device).order_by('-created_at')[:5]
    history_data = [{
        'action': h.get_action_display(),
        'status': h.status,
        'error': h.error_message,
        'time': h.created_at.strftime('%d.%m.%Y %H:%M'),
        'user': h.user.username if h.user else 'Система'
    } for h in history_qs]

    device_info = {
        'serial': device.serial_number,
        'model': device.model.model_name if device.model else '—',
        'status_display': device.get_status_display(),
        'status_code': device.status,
        'is_online': is_online,
        'current': device.nominal_current or '—',
        'phases': device.model.phases if device.model else '—',
        'created_at': device.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        'region': device.region.name if device.region else '—',
        'substation': device.substation.name if device.substation else '—',
        'feeder': device.feeder.name if device.feeder else '—',
        'tp': device.tp.name if device.tp else '—',
        'last_reading_date': last_reading_date,
        'last_reading_val': last_reading_val,
        'history': history_data,
    }

    # --- ПРОВЕРКА СТАТУСА ---
    # 1. Сначала смотрим, нет ли задачи в очереди (pending или processing)
    active_task = RelayTask.objects.filter(device=device, status__in=['pending', 'processing']).order_by('-created_at').first()
    
    if active_task:
        return JsonResponse({
            'status': 'pending', 
            'provider': active_task.device.model.device_type_str if active_task.device.model else 'АСКУЭ',
            'message': f'Команда в очереди (попытка {active_task.retry_count + 1}). Ожидаем выполнения.',
            'device': device_info
        })

    # 2. Если активных задач нет, пробуем узнать статус напрямую из API (Hexing/SunRise)
    try:
        if device.askue_id == '25' or (device.api_id and device.api_id.upper().startswith('UK')):
            service = HexingService()
            status_str = service.get_relay_status_from_db(device.serial_number)
            if status_str != 'unknown':
                return JsonResponse({'status': status_str, 'provider': 'Hexing', 'device': device_info})
        
        elif device.askue_id == '26' or (device.api_id and device.api_id.upper().startswith('SR')):
            service = SunRiseService()
            state = service.get_relay_state(device.serial_number)
            # 2 — готов к включению: команда принята, ждёт нажатия кнопки на счётчике
            status = {1: 'on', 0: 'off', 2: 'ready'}.get(state, 'unknown')
            return JsonResponse({'status': status, 'provider': 'SunRise', 'device': device_info})
            
        elif device.askue_id == '23' or (device.api_id and device.api_id.upper().startswith('EM')):
            service = CEnergoService()
            status_str = service.get_status(device.serial_number)
            if status_str != 'unknown':
                return JsonResponse({'status': status_str, 'provider': 'Энергомера', 'device': device_info})
                
    except Exception as e:
        # Если API выдало ошибку (счетчик офлайн), молча идем дальше и берем статус из истории
        pass

    # 3. Если API недоступно, берем последнюю успешную команду из истории
    last_success = RelayCommandHistory.objects.filter(device=device, status='success').order_by('-created_at').first()
    if last_success:
        state = 'off' if last_success.action == 'off' else 'on'
        return JsonResponse({'status': state, 'provider': 'История', 'device': device_info})

    return JsonResponse({'status': 'unknown', 'provider': 'Нет данных', 'device': device_info})


@login_required
@user_passes_test(can_manage_relay)
@require_POST
def relay_task_confirm_button(request):
    """
    «Кнопка нажата»: задача включения ждёт нажатия кнопки на счётчике (SunRise, состояние 2).
    Сначала читаем состояние счётчика: включён — снимаем пометку. Иначе 409; если счётчик не
    отвечает, пользователь может подтвердить вручную (force=1).
    """
    task = RelayTask.objects.select_related('device', 'device__region').filter(
        id=request.POST.get('task_id') or 0).first()
    if not task or task.status != 'success' or task.action != 'on' or task.error_message != READY_NOTE:
        return JsonResponse({'error': 'Задача не ждёт нажатия кнопки'}, status=400)
    if relay_region_denied(request.user, task.device):
        return JsonResponse({'error': 'Нет прав на этот счётчик'}, status=403)

    if request.POST.get('force') != '1':
        try:
            state = SunRiseService().get_relay_state(task.device.serial_number)
        except Exception as e:
            return JsonResponse({'error': f'Не удалось проверить счётчик: {e}', 'can_force': True}, status=502)
        if state != 1:
            text = 'готов к включению (кнопка ещё не нажата)' if state == 2 else 'отключено'
            return JsonResponse({'error': f'Состояние счётчика: {text}'}, status=409)

    task.error_message = None
    task.save(update_fields=['error_message', 'updated_at'])
    RelayCommandHistory.objects.filter(
        device=task.device, action='on', status='success', error_message=READY_NOTE
    ).update(error_message=None)
    return JsonResponse({'status': 'ok'})


@login_required
@user_passes_test(can_manage_relay)
@require_POST
def relay_api_action(request):
    action = request.POST.get('action')
    if action not in ['on', 'off']:
        return JsonResponse({'error': 'Неверное действие'}, status=400)

    device, error = find_relay_device(request.POST.get('serial', ''))
    if not device:
        return JsonResponse({'error': error}, status=404)
    if relay_region_denied(request.user, device):
        return JsonResponse({'error': 'Счётчик не из вашего района'}, status=403)

    if action == 'off' and is_ct_meter(device):
        return JsonResponse({'error': f'Задача не создана: {CT_METER_TEXT}. Проверьте выбранный счётчик.'}, status=409)

    # Такая же команда уже выполняется воркером — дубль не нужен
    if inflight_tasks(device, actions=(action,)).exists():
        return JsonResponse({'error': 'Эта команда уже выполняется, дождитесь результата'}, status=409)

    # Отменяем ожидающие задачи счётчика; выполняющуюся противоположную не трогаем,
    # новая команда пойдёт после неё (через пару минут)
    task_time, busy = supersede_tasks(
        device, timezone.now(), 'Отменено из-за новой команды из ручного управления'
    )

    task = RelayTask.objects.create(
        device=device,
        action=action,
        scheduled_at=task_time,
        created_by=request.user,
        status='pending'
    )

    return JsonResponse({
        'message': (f'Команда "{action}" поставлена в очередь на {task_time:%H:%M}: '
                    f'предыдущая команда ещё выполняется, новая пойдёт после неё.') if busy
                   else f'Команда "{action}" успешно поставлена в очередь на выполнение.',
        'status': 'pending',
        'task_id': task.id
    })


@login_required
@user_passes_test(can_manage_relay)
@require_POST
def relay_bulk_start(request):
    """
    Массовое управление реле по списку серийных номеров.
    Команды на счётчики не отправляются напрямую: для каждого номера создаётся
    RelayTask, дальше всё делает воркер process_relay_tasks (проверка долга в 1С
    перед отключением, ретраи, история). Результат пишется в тот же JSON-файл,
    который читает relay_bulk_status.
    """
    serials_text = request.POST.get('serials', '')
    action = request.POST.get('action')

    if not serials_text or action not in ('on', 'off'):
        return JsonResponse({'error': 'Нет данных или неверное действие'}, status=400)

    # Без повторов, порядок сохраняется
    serials = list(dict.fromkeys(
        line.strip() for line in serials_text.splitlines() if line.strip()
    ))
    if len(serials) > 2000:
        return JsonResponse({'error': 'Слишком длинный список (максимум 2000 номеров)'}, status=400)

    now = timezone.now()
    # Отключение — не раньше, чем через RELAY_MIN_LEAD_MINUTES: время проверить задачи
    # в мониторинге и отменить. Включение выполняется сразу.
    scheduled_at = now + timedelta(minutes=RELAY_MIN_LEAD_MINUTES) if action == 'off' else now
    opposite_action = 'on' if action == 'off' else 'off'

    results = {'status': 'running', 'total': len(serials), 'processed': 0, 'success': 0, 'errors': []}

    for serial in serials:
        results['processed'] += 1
        device, error = find_relay_device(serial)
        if not device:
            results['errors'].append(f'{serial}: {error}')
            continue
        if relay_region_denied(request.user, device):
            results['errors'].append(f'{serial}: счётчик не из вашего района')
            continue
        if action == 'off' and is_ct_meter(device):
            results['errors'].append(f'{serial}: {CT_METER_TEXT}')
            continue
        if RelayTask.objects.filter(
            device=device, action=action, status__in=['pending', 'processing']
        ).exists():
            results['errors'].append(f'{serial}: уже есть активная задача')
            continue

        task_time, _busy = supersede_tasks(
            device, scheduled_at, 'Отменено из-за создания новой задачи', actions=(opposite_action,)
        )

        sub = getattr(device, 'subscriber', None)
        RelayTask.objects.create(
            device=device, action=action, scheduled_at=task_time,
            created_by=request.user, status='pending',
            debt_snapshot=sub.debt if sub else 0,
        )
        results['success'] += 1

    results['status'] = 'completed'
    os.makedirs(os.path.dirname(BULK_RESULT_PATH), exist_ok=True)
    with open(BULK_RESULT_PATH, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False)

    return JsonResponse({
        'message': f'Поставлено в очередь задач: {results["success"]} из {results["total"]}',
        'scheduled_at': scheduled_at.strftime('%Y-%m-%d %H:%M'),
        'errors_count': len(results['errors']),
    })

@login_required
@user_passes_test(can_manage_relay)
def relay_bulk_status(request):
    if os.path.exists(BULK_RESULT_PATH):
        with open(BULK_RESULT_PATH, 'r', encoding='utf-8') as f:
            return JsonResponse(json.load(f))
    return JsonResponse({'status': 'starting', 'total': 0, 'processed': 0})
