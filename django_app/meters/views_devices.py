"""Устройства: список, карточка, добавление и загрузка, группы, массовые операции, реестры и экспорт.

Вынесено из views.py. Маршруты подключены в urls.py через `views_devices`.
"""
import logging
from datetime import datetime, timedelta

import openpyxl
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q, OuterRef, Exists
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET
from openpyxl.styles import Font, Alignment

from .models import (
    Device, DeviceGroup, MeterModel, Reading, Region, Substation, Feeder,
    TransformerSubstation,
)
from .registration import register_devices_for_robot, ROBOT_CONFIGS
from .views import is_allowed_user, get_producer_display, producer_model_ids
from .report_helpers import window_page_range

logger = logging.getLogger(__name__)


# ===== УСТРОЙСТВА (Реестр) – ОБНОВЛЁННАЯ ВЕРСИЯ =====
@login_required
@user_passes_test(is_allowed_user)
def devices(request):
    # Получаем фильтры из GET
    search = request.GET.get('search', '')
    producer_filter = request.GET.get('producer', '')
    model_filter = request.GET.get('model', '')
    status_filter = request.GET.get('status', '')
    region_filter = request.GET.get('region', '')
    substation_filter = request.GET.get('substation', '')
    feeder_filter = request.GET.get('feeder', '')
    tp_filter = request.GET.get('tp', '')
    group_filter = request.GET.get('group', '')
    category_filter = request.GET.get('category', '')

    # Все группы для выпадающих списков (в модалку)
    groups_all = DeviceGroup.objects.all().order_by('name')

    # Пагинация
    page = int(request.GET.get('page', 1))
    per_page = 10

    # Базовый запрос с предзагрузкой связей
    qs = Device.objects.select_related('model', 'region', 'substation', 'feeder', 'tp').prefetch_related('groups')

    if search:
        qs = qs.filter(Q(serial_number__icontains=search) | Q(model__model_name__icontains=search))
    producer_name = request.GET.get('producer_name', '')
    if producer_filter:
        qs = qs.filter(model__device_type_str=producer_filter)
    if producer_name:
        qs = qs.filter(model_id__in=producer_model_ids(producer_name))
    if model_filter:
        qs = qs.filter(model__id=model_filter)
    if status_filter:
        qs = qs.filter(status=status_filter)
    if region_filter:
        qs = qs.filter(region_id=region_filter)
    if substation_filter:
        qs = qs.filter(substation_id=substation_filter)
    if feeder_filter:
        qs = qs.filter(feeder_id=feeder_filter)
    if tp_filter:
        qs = qs.filter(tp_id=tp_filter)
    if group_filter:
        qs = qs.filter(groups__id=group_filter)
    if category_filter == '__none__':
        qs = qs.filter(category__isnull=True)
    elif category_filter:
        qs = qs.filter(category=category_filter)

    total = qs.count()
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    if page > total_pages:
        page = total_pages
    devices_list = list(qs.order_by('serial_number')[(page-1)*per_page:page*per_page])
    # «Есть показание за 7 дней» считаем одним запросом только для приборов страницы
    from django.utils import timezone
    from datetime import timedelta
    recent_ids = set(Reading.objects.filter(
        device_id__in=[d.id for d in devices_list],
        timestamp__gte=timezone.now() - timedelta(days=7),
    ).values_list('device_id', flat=True).distinct())
    for d in devices_list:
        d.has_recent_reading = d.id in recent_ids
    page_range = window_page_range(page, total_pages)

    # Получаем списки для выпадающих фильтров
    codes = MeterModel.objects.values_list('device_type_str', flat=True).distinct().order_by('device_type_str')
    producers_with_selected = []
    for code in codes:
        display = get_producer_display(code, '')
        if display == "Неизвестно" and code:
            display = code
        selected = (code == producer_filter)
        producers_with_selected.append((display, code, selected))

    models_list = MeterModel.objects.all().order_by('model_name')
    models_with_selected = [(m, str(m.id) == model_filter) for m in models_list]

    statuses = [
        ('active', 'Активен'),
        ('repair', 'Ремонт'),
        ('inactive', 'Отключен'),
        ('temporary_off', 'Временно отключен'),
    ]
    statuses_with_selected = [(value, label, value == status_filter) for value, label in statuses]

    # Списки для иерархии
    regions_with_selected = [(r, str(r.id) == region_filter) for r in Region.objects.all().order_by('name')]
    substations_with_selected = [(s, str(s.id) == substation_filter) for s in Substation.objects.all().order_by('name')]
    feeders_with_selected = [(f, str(f.id) == feeder_filter) for f in Feeder.objects.all().order_by('name')]
    tps_with_selected = [(t, str(t.id) == tp_filter) for t in TransformerSubstation.objects.all().order_by('name')]
    groups_with_selected = [(g, str(g.id) == group_filter) for g in DeviceGroup.objects.all().order_by('name')]

    categories = list(Device.CATEGORY_CHOICES)
    categories_with_selected = [(c[0], c[1], c[0] == category_filter) for c in categories]
    categories_with_selected.insert(0, ('', 'Все', category_filter == ''))
    categories_with_selected.append(('__none__', 'Не указано', category_filter == '__none__'))

    # Обработка POST-запроса
    if request.method == 'POST':
        # Массовое обновление привязки к иерархии (существующий код)
        if 'update_hierarchy' in request.POST:
            selected_ids = request.POST.getlist('selected_devices')
            if not selected_ids:
                messages.warning(request, 'Выберите хотя бы одно устройство.')
                return redirect(request.path)

            target_region = request.POST.get('target_region')
            target_substation = request.POST.get('target_substation')
            target_feeder = request.POST.get('target_feeder')
            target_tp = request.POST.get('target_tp')

            update_fields = {}
            if target_region and target_region != '':
                update_fields['region_id'] = target_region
            if target_substation and target_substation != '':
                update_fields['substation_id'] = target_substation
            if target_feeder and target_feeder != '':
                update_fields['feeder_id'] = target_feeder
            if target_tp and target_tp != '':
                update_fields['tp_id'] = target_tp

            if not update_fields:
                messages.warning(request, 'Выберите хотя бы одно поле для обновления.')
                return redirect(request.path)

            updated_count = Device.objects.filter(id__in=selected_ids).update(**update_fields)
            messages.success(request, f'Обновлено {updated_count} устройств.')
            return redirect(request.path)

        # НОВОЕ: массовое добавление устройств в группы через модалку
        elif 'add_to_groups_modal' in request.POST:
            selected_ids = request.POST.getlist('selected_devices')
            group_ids = request.POST.getlist('group_ids')

            if not selected_ids:
                messages.warning(request, 'Выберите хотя бы одно устройство.')
            elif not group_ids:
                messages.warning(request, 'Выберите хотя бы одну группу.')
            else:
                devices = Device.objects.filter(id__in=selected_ids)
                groups_to_add = DeviceGroup.objects.filter(id__in=group_ids)
                for device in devices:
                    device.groups.add(*groups_to_add)
                messages.success(request, f'Устройства добавлены в {len(groups_to_add)} группу(ы).')
            return redirect(request.path)
        
        # НОВОЕ: массовое изменение устройств категории
        elif 'update_category' in request.POST:
            selected_ids = request.POST.getlist('selected_devices')
            new_category = request.POST.get('new_category')
            if not selected_ids:
                messages.warning(request, 'Выберите хотя бы одно устройство.')
            elif not new_category:
                messages.warning(request, 'Выберите категорию.')
            else:
                Device.objects.filter(id__in=selected_ids).update(category=new_category)
                messages.success(request, f'Категория обновлена для {len(selected_ids)} устройств.')
            return redirect(request.path)

    context = {
        'devices': devices_list,
        'total': total,
        'page': page,
        'per_page': per_page,
        'search': search,
        'total_pages': total_pages,
        'page_range': page_range,
        'producers_with_selected': producers_with_selected,
        'models_with_selected': models_with_selected,
        'statuses_with_selected': statuses_with_selected,
        'producer_filter': producer_filter,
        'model_filter': model_filter,
        'status_filter': status_filter,
        # 'regions': regions,
        # 'substations': substations,
        # 'feeders': feeders,
        # 'tps': tps,
        'groups_all': groups_all,
        'regions_with_selected': regions_with_selected,
        'substations_with_selected': substations_with_selected,
        'feeders_with_selected': feeders_with_selected,
        'tps_with_selected': tps_with_selected,
        'groups_with_selected': groups_with_selected,                     
        'selected_region': region_filter,
        'selected_substation': substation_filter,
        'selected_feeder': feeder_filter,
        'selected_tp': tp_filter,
        'selected_group': group_filter,
        'categories_with_selected': categories_with_selected,
        'category_filter': category_filter,
    }

    current_params = request.GET.copy()
    if 'page' in current_params:
        current_params.pop('page')
    current_params = current_params.urlencode()
    context['current_params'] = current_params

    return render(request, 'meters/devices.html', context)


# ===== УПРАВЛЕНИЕ ГРУППАМИ ДЛЯ ОТДЕЛЬНОГО УСТРОЙСТВА =====
@login_required
@user_passes_test(is_allowed_user)
def device_add_to_groups(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    if request.method == 'POST':
        group_ids = request.POST.getlist('group_ids')
        if group_ids:
            groups = DeviceGroup.objects.filter(id__in=group_ids)
            device.groups.add(*groups)
            messages.success(request, f'Устройство {device.serial_number} добавлено в выбранные группы.')
        else:
            messages.warning(request, 'Не выбрано ни одной группы.')
        return redirect('devices')
    groups = DeviceGroup.objects.all().order_by('name')
    return render(request, 'meters/device_add_to_groups.html', {'device': device, 'groups': groups})


@login_required
@user_passes_test(is_allowed_user)
def device_remove_from_groups(request, device_id):
    device = get_object_or_404(Device, id=device_id)
    if request.method == 'POST':
        group_ids = request.POST.getlist('group_ids')
        if group_ids:
            groups = DeviceGroup.objects.filter(id__in=group_ids)
            device.groups.remove(*groups)
            messages.success(request, f'Устройство {device.serial_number} удалено из выбранных групп.')
        else:
            messages.warning(request, 'Не выбрано ни одной группы.')
        return redirect('devices')
    groups = device.groups.all().order_by('name')
    return render(request, 'meters/device_remove_from_groups.html', {'device': device, 'groups': groups})


@login_required
@user_passes_test(is_allowed_user)
def edit_device(request, serial_number):
    device = get_object_or_404(Device, serial_number=serial_number)
    if request.method == 'POST':
        new_serial = request.POST.get('serial_number', '').strip()
        if not new_serial:
            messages.error(request, 'Номер не может быть пустым.')
        elif new_serial == device.serial_number:
            messages.warning(request, 'Номер не изменён.')
        elif Device.objects.filter(serial_number=new_serial).exclude(id=device.id).exists():
            messages.error(request, f'Номер "{new_serial}" уже используется другим устройством.')
        else:
            device.serial_number = new_serial
            device.save()
            messages.success(request, f'Заводской номер изменён на {new_serial}.')
            return redirect('devices')
    context = {'device': device}
    return render(request, 'meters/edit_device.html', context)


@login_required
@user_passes_test(is_allowed_user)
def delete_device(request, serial_number):
    device = get_object_or_404(Device, serial_number=serial_number)
    if request.method == 'POST':
        device.delete()
        messages.success(request, f'Устройство {serial_number} удалено.')
        return redirect('devices')
    return render(request, 'meters/confirm_delete.html', {'object': device, 'type': 'устройство'})


@login_required
@user_passes_test(is_allowed_user)
def add_device(request):
    if request.method == 'POST':
        if 'single_submit' in request.POST:
            serial = request.POST.get('serial_number', '').strip()
            model_id = request.POST.get('model')
            status = request.POST.get('status', 'active')
            if not serial:
                messages.error(request, 'Серийный номер обязателен.')
            elif Device.objects.filter(serial_number=serial).exists():
                messages.error(request, f'Прибор с номером {serial} уже существует.')
            else:
                try:
                    model = MeterModel.objects.get(id=model_id) if model_id else None
                    if model:
                        nominal_current = model.nominal_current
                        phase = model.phases
                        askue_id = model.device_type_id or ''
                        api_id = model.device_type_str or ''
                    else:
                        nominal_current = ''
                        phase = None
                        askue_id = ''
                        api_id = ''
                    jpes_region, _ = Region.objects.get_or_create(name='ЖПЭС')
                    Device.objects.create(
                        serial_number=serial,
                        model=model,
                        status=status,
                        region=jpes_region,
                        nominal_current=nominal_current,
                        phase=phase,
                        askue_id=askue_id,
                        api_id=api_id,
                    )
                    messages.success(request, f'✅ Отчет: Прибор {serial} успешно добавлен в реестр.')
                    return redirect('devices')
                except Exception as e:
                    messages.error(request, f'Ошибка: {e}')
        elif 'bulk_submit' in request.POST:
            serials_text = request.POST.get('bulk_serials', '').strip()
            model_id = request.POST.get('bulk_model')
            status = request.POST.get('bulk_status', 'active')
            if not serials_text:
                messages.error(request, 'Введите хотя бы один номер.')
            else:
                serials = [s.strip() for s in serials_text.split('\n') if s.strip()]
                added = 0
                skipped = []
                try:
                    model = MeterModel.objects.get(id=model_id) if model_id else None
                    if model:
                        nominal_current = model.nominal_current
                        phase = model.phases
                        askue_id = model.device_type_id or ''
                        api_id = model.device_type_str or ''
                    else:
                        nominal_current = ''
                        phase = None
                        askue_id = ''
                        api_id = ''
                    jpes_region, _ = Region.objects.get_or_create(name='ЖПЭС')
                    for sn in serials:
                        if Device.objects.filter(serial_number=sn).exists():
                            skipped.append(sn)
                            continue
                        Device.objects.create(
                            serial_number=sn,
                            model=model,
                            status=status,
                            region=jpes_region,
                            nominal_current=nominal_current,
                            phase=phase,
                            askue_id=askue_id,
                            api_id=api_id,
                        )
                        added += 1
                except Exception as e:
                    skipped.append(f"Ошибка: {e}")
                                # Формируем красивый отчет
                if added > 0:
                    messages.success(request, f'✅ Отчет по импорту: Успешно добавлено {added} приборов.')
                if skipped:
                    # Если пропущенных много, показываем первых 5
                    skipped_str = ", ".join(skipped[:5])
                    if len(skipped) > 5:
                        skipped_str += f" и еще {len(skipped) - 5}..."
                    messages.warning(request, f'⚠️ Пропущено {len(skipped)} дубликатов: {skipped_str}')
                if not added and not skipped:
                    messages.info(request, 'Ни один прибор не был добавлен. Проверьте данные.')
                
                return redirect('devices')
    context = {
        'models': MeterModel.objects.all().order_by('model_name'),
    }
    return render(request, 'meters/add_device.html', context)


@login_required
@user_passes_test(is_allowed_user)
def upload_devices(request):
    if request.method == 'POST' and request.FILES.get('excel_file'):
        file = request.FILES['excel_file']
        try:
            wb = openpyxl.load_workbook(file)
            ws = wb.active
            added = 0
            skipped = []
            errors = []
            for row in ws.iter_rows(min_row=2, values_only=True):
                sn = str(row[0]).strip() if row[0] else None
                model_name = str(row[1]).strip() if row[1] else None
                status = str(row[2]).strip() if row[2] else 'active'
                nominal_current = str(row[3]).strip() if row[3] else ''
                phase = row[4]
                askue_id = str(row[5]).strip() if row[5] else ''
                api_id = str(row[6]).strip() if row[6] else ''
                if not sn:
                    continue
                if Device.objects.filter(serial_number=sn).exists():
                    skipped.append(sn)
                    continue
                try:
                    model = MeterModel.objects.filter(model_name=model_name).first() if model_name else None
                    jpes_region, _ = Region.objects.get_or_create(name='ЖПЭС')
                    Device.objects.create(
                        serial_number=sn,
                        model=model,
                        status=status,
                        region=jpes_region,
                        nominal_current=nominal_current,
                        phase=int(phase) if phase else None,
                        askue_id=askue_id,
                        api_id=api_id,
                    )
                    added += 1
                except Exception as e:
                    errors.append(f"{sn}: {e}")
            messages.success(request, f'Добавлено: {added}. Пропущено (дубликаты): {len(skipped)}. Ошибок: {len(errors)}')
            if errors:
                messages.warning(request, f'Ошибки: {"; ".join(errors[:3])}')
        except Exception as e:
            messages.error(request, f'Ошибка обработки файла: {e}')
        return redirect('devices')
    return redirect('add_device')


@login_required
@user_passes_test(is_allowed_user)
def download_template(request):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Devices"
    headers = ["serial_number", "model_name", "status", "nominal_current", "phase", "askue_id", "api_id"]
    ws.append(headers)
    ws.append(["084600001431", "DTZY217", "active", "5(80)", "3", "", ""])
    ws.append(["084600001432", "P34S02", "active", "10(80)", "3", "", ""])
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=device_template.xlsx'
    wb.save(response)
    return response


# исправление статуса active, inactive
@login_required
@user_passes_test(is_allowed_user)
def bulk_update_status_page(request):
    if request.method == 'POST':
        serials_text = request.POST.get('serials', '').strip()
        new_status = request.POST.get('status', 'active')
        if not serials_text:
            messages.error(request, 'Список серийных номеров пуст.')
            return redirect('bulk_update_status_page')
        serials = [s.strip() for s in serials_text.split('\n') if s.strip()]
        updated = 0
        not_found = []
        for sn in serials:
            device = Device.objects.filter(serial_number=sn).first()
            if device:
                device.status = new_status
                device.save()
                updated += 1
            else:
                not_found.append(sn)
        if updated:
            messages.success(request, f'Обновлено устройств: {updated}')
        if not_found:
            messages.warning(request, f'Не найдены: {", ".join(not_found[:10])}')
        return redirect('bulk_update_status_page')
    return render(request, 'meters/bulk_update_status.html')


# ===== УПРАВЛЕНИЕ ГРУППАМИ =====
@login_required
@user_passes_test(is_allowed_user)
def groups_list(request):
    groups = DeviceGroup.objects.all().order_by('name')
    return render(request, 'meters/groups_list.html', {'groups': groups})


@login_required
@user_passes_test(is_allowed_user)
def group_create(request):
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        if not name:
            messages.error(request, 'Название группы обязательно.')
        elif DeviceGroup.objects.filter(name=name).exists():
            messages.error(request, 'Группа с таким названием уже существует.')
        else:
            DeviceGroup.objects.create(name=name, description=description)
            messages.success(request, f'Группа "{name}" создана.')
            return redirect('groups_list')
    return render(request, 'meters/group_form.html', {'action': 'create'})


@login_required
@user_passes_test(is_allowed_user)
def group_edit(request, group_id):
    group = get_object_or_404(DeviceGroup, id=group_id)
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        if not name:
            messages.error(request, 'Название группы обязательно.')
        elif DeviceGroup.objects.exclude(id=group_id).filter(name=name).exists():
            messages.error(request, 'Группа с таким названием уже существует.')
        else:
            group.name = name
            group.description = description
            group.save()
            messages.success(request, f'Группа "{name}" обновлена.')
            return redirect('groups_list')
    return render(request, 'meters/group_form.html', {'group': group, 'action': 'edit'})


@login_required
@user_passes_test(is_allowed_user)
def group_delete(request, group_id):
    group = get_object_or_404(DeviceGroup, id=group_id)
    if request.method == 'POST':
        group.delete()
        messages.success(request, f'Группа "{group.name}" удалена.')
        return redirect('groups_list')
    return render(request, 'meters/confirm_delete.html', {'object': group, 'type': 'группу'})


@login_required
@user_passes_test(is_allowed_user)
def register_devices(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    robot_name = request.POST.get('robot_name')
    limit = request.POST.get('limit')
    limit = int(limit) if limit and limit.isdigit() else None
    
    if not robot_name:
        return JsonResponse({'error': 'robot_name required'}, status=400)
    
    if robot_name not in ROBOT_CONFIGS:
        return JsonResponse({'error': f'No registration config for {robot_name}'}, status=400)
    
    try:
        count = register_devices_for_robot(robot_name, limit)
        return JsonResponse({'message': f'Registered {count} new devices for {robot_name}', 'count': count})
    except Exception as e:
        logger.error(f"Error registering devices for {robot_name}: {e}")
        return JsonResponse({'error': str(e)}, status=500)


def register_devices_page(request):
    robots = list(ROBOT_CONFIGS.keys())
    return render(request, 'meters/register_devices.html', {'robots': robots})


@login_required
@user_passes_test(is_allowed_user)
def registered_devices_report(request):
    # Получаем параметры фильтра
    source_filter = request.GET.get('source', '')
    search = request.GET.get('search', '').strip()
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    export = request.GET.get('export', False)

    # Базовый запрос – устройства, зарегистрированные через автоматическую регистрацию
    # Предполагаем, что при регистрации мы заполняем поле api_id значением источника (например, 'SR', 'SX', 'HK')
    devices_qs = Device.objects.select_related('model').filter(
        Q(api_id__isnull=False) & ~Q(api_id='')
    ).order_by('-created_at')

    # Фильтр по источнику
    if source_filter:
        devices_qs = devices_qs.filter(api_id=source_filter)

    # Поиск по серийному номеру
    if search:
        devices_qs = devices_qs.filter(serial_number__icontains=search)

    # Фильтр по дате регистрации
    if date_from:
        try:
            date_from_obj = datetime.strptime(date_from, '%Y-%m-%d').date()
            devices_qs = devices_qs.filter(created_at__date__gte=date_from_obj)
        except ValueError:
            pass
    if date_to:
        try:
            date_to_obj = datetime.strptime(date_to, '%Y-%m-%d').date()
            devices_qs = devices_qs.filter(created_at__date__lte=date_to_obj)
        except ValueError:
            pass

    # Экспорт в Excel
    if export == 'xlsx':
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Зарегистрированные счетчики"

        headers = ['Заводской номер', 'Тип ПУ', 'Источник', 'Дата регистрации', 'Статус']
        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal='center')

        for row_idx, device in enumerate(devices_qs, 2):
            ws.cell(row=row_idx, column=1, value=device.serial_number)
            ws.cell(row=row_idx, column=2, value=device.model.model_name if device.model else '')
            ws.cell(row=row_idx, column=3, value=device.api_id or '—')
            ws.cell(row=row_idx, column=4, value=device.created_at.strftime('%Y-%m-%d %H:%M:%S') if device.created_at else '')
            ws.cell(row=row_idx, column=5, value=device.status)

        for col in ws.columns:
            max_length = 0
            col_letter = col[0].column_letter
            for cell in col:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            ws.column_dimensions[col_letter].width = max_length + 2

        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="registered_devices_{timezone.now().date()}.xlsx"'
        wb.save(response)
        return response

    # Пагинация
    page = int(request.GET.get('page', 1))
    per_page = 20
    total = devices_qs.count()
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    if page > total_pages:
        page = total_pages
    devices = devices_qs[(page-1)*per_page:page*per_page]
    page_range = window_page_range(page, total_pages)

    # Список источников (роботов) для фильтра с флагом selected
    sources = [
        ('SR', 'SunRise', source_filter == 'SR'),
        ('SX', 'Sanxing_old', source_filter == 'SX'),
        ('HK', 'Hexing KUK', source_filter == 'HK'),
        # Добавьте другие источники по мере необходимости
    ]

    context = {
        'devices': devices,
        'total': total,
        'page': page,
        'total_pages': total_pages,
        'page_range': page_range,
        'sources': sources,
        'selected_source': source_filter,
        'search': search,
        'date_from': date_from,
        'date_to': date_to,
        'start_index': (page - 1) * per_page,
    }
    return render(request, 'meters/registered_devices.html', context)


@login_required
@user_passes_test(is_allowed_user)
def bulk_update_region_page(request):
    if request.method == 'POST':
        serials_text = request.POST.get('serials', '').strip()
        region_id = request.POST.get('region_id', '')
        substation_id = request.POST.get('substation_id', '')
        feeder_id = request.POST.get('feeder_id', '')
        tp_id = request.POST.get('tp_id', '')

        if not serials_text:
            messages.error(request, 'Список серийных номеров пуст.')
            return redirect('bulk_update_region_page')

        serials = [s.strip() for s in serials_text.split('\n') if s.strip()]
        if not serials:
            messages.error(request, 'Нет валидных номеров.')
            return redirect('bulk_update_region_page')

        # Подготавливаем поля для обновления
        update_fields = {}
        if region_id:
            update_fields['region_id'] = region_id
        if substation_id:
            update_fields['substation_id'] = substation_id
        if feeder_id:
            update_fields['feeder_id'] = feeder_id
        if tp_id:
            update_fields['tp_id'] = tp_id

        if not update_fields:
            messages.warning(request, 'Выберите хотя бы одно поле для обновления.')
            return redirect('bulk_update_region_page')

        updated = 0
        not_found = []
        for sn in serials:
            device = Device.objects.filter(serial_number=sn).first()
            if device:
                Device.objects.filter(id=device.id).update(**update_fields)
                updated += 1
            else:
                not_found.append(sn)

        if updated:
            messages.success(request, f'Обновлено устройств: {updated}')
        if not_found:
            messages.warning(request, f'Не найдены: {", ".join(not_found[:10])}')

        return redirect('bulk_update_region_page')

    # GET – показываем форму
    regions = Region.objects.all().order_by('name')
    substations = Substation.objects.all().order_by('name')
    feeders = Feeder.objects.all().order_by('name')
    tps = TransformerSubstation.objects.all().order_by('name')

    context = {
        'regions': regions,
        'substations': substations,
        'feeders': feeders,
        'tps': tps,
    }
    return render(request, 'meters/bulk_update_region.html', context)


# @login_required
# @user_passes_test(is_allowed_user)
def export_devices_csv(request):
    """Экспорт реестра приборов в CSV (потоково, без ограничений)."""
    # Берём те же фильтры, что и в devices
    qs = Device.objects.select_related('model', 'region', 'substation', 'feeder', 'tp').prefetch_related('groups')

    # Применяем фильтры (копируем из devices)
    search = request.GET.get('search', '')
    producer_filter = request.GET.get('producer', '')
    model_filter = request.GET.get('model', '')
    status_filter = request.GET.get('status', '')
    region_filter = request.GET.get('region', '')
    substation_filter = request.GET.get('substation', '')
    feeder_filter = request.GET.get('feeder', '')
    tp_filter = request.GET.get('tp', '')
    group_filter = request.GET.get('group', '')

    if search:
        qs = qs.filter(Q(serial_number__icontains=search) | Q(model__model_name__icontains=search))
    if producer_filter:
        qs = qs.filter(model__device_type_str=producer_filter)
    if model_filter:
        qs = qs.filter(model__id=model_filter)
    if status_filter:
        qs = qs.filter(status=status_filter)
    if region_filter:
        qs = qs.filter(region_id=region_filter)
    if substation_filter:
        qs = qs.filter(substation_id=substation_filter)
    if feeder_filter:
        qs = qs.filter(feeder_id=feeder_filter)
    if tp_filter:
        qs = qs.filter(tp_id=tp_filter)
    if group_filter:
        qs = qs.filter(groups__id=group_filter)

    # Определяем онлайн-статус для каждого устройства
    # Аннотируем has_recent_reading (как в devices)
    seven_days_ago = timezone.now() - timedelta(days=7)
    qs = qs.annotate(
        has_recent_reading=Exists(
            Reading.objects.filter(
                device=OuterRef('pk'),
                timestamp__gte=seven_days_ago
            )
        )
    )

    # Генератор строк CSV
    def generate_csv():
        # Заголовки
        yield ['Заводской номер', 'Тип ПУ', 'Статус', 'Категория', 'Район', 'Подстанция', 'Фидер', 'ТП', 'Группы', 'Ток', 'Фазность', 'Статус связи', 'Дата добавления']

        for device in qs.iterator(chunk_size=1000):
            groups = ', '.join(g.name for g in device.groups.all())
            online_status = 'Онлайн' if device.id in recent_ids else 'Оффлайн'
            yield [
                device.serial_number,
                device.model.model_name if device.model else '',
                device.status,
                device.get_category_display() or '',
                device.region.name if device.region else '',
                device.substation.name if device.substation else '',
                device.feeder.name if device.feeder else '',
                device.tp.name if device.tp else '',
                groups,
                device.nominal_current or '',
                device.model.phases if device.model else '',
                online_status,
                device.created_at.strftime('%Y-%m-%d %H:%M:%S') if device.created_at else '',
            ]

    # Создаём StreamingHttpResponse
    response = StreamingHttpResponse(
        generate_csv(),
        content_type='text/csv; charset=utf-8'
    )
    response['Content-Disposition'] = f'attachment; filename="registry_devices_{timezone.now().date()}.csv"'
    return response


@login_required
@user_passes_test(is_allowed_user)
def export_devices_xlsx(request):
    """Экспорт реестра приборов в Excel (потоковая запись, без ограничения по числу строк)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill
    from openpyxl.utils import get_column_letter
    # Базовый queryset с фильтрами (как в devices)
    qs = Device.objects.select_related('model', 'region', 'substation', 'feeder', 'tp').prefetch_related('groups')
    seven_days_ago = timezone.now() - timedelta(days=7)
    # Приборы с показаниями за 7 дней одним запросом (Exists на каждую строку
    # давал ~90 с на 180 тыс. приборов и упирался в таймаут gunicorn).
    recent_ids = set(
        Reading.objects.filter(timestamp__gte=seven_days_ago)
        .values_list('device_id', flat=True).distinct()
    )

    # Фильтры (копируем из devices)
    search = request.GET.get('search', '')
    producer_filter = request.GET.get('producer', '')
    model_filter = request.GET.get('model', '')
    status_filter = request.GET.get('status', '')
    region_filter = request.GET.get('region', '')
    substation_filter = request.GET.get('substation', '')
    feeder_filter = request.GET.get('feeder', '')
    tp_filter = request.GET.get('tp', '')
    group_filter = request.GET.get('group', '')

    if search:
        qs = qs.filter(Q(serial_number__icontains=search) | Q(model__model_name__icontains=search))
    if producer_filter:
        qs = qs.filter(model__device_type_str=producer_filter)
    if model_filter:
        qs = qs.filter(model__id=model_filter)
    if status_filter:
        qs = qs.filter(status=status_filter)
    if region_filter:
        qs = qs.filter(region_id=region_filter)
    if substation_filter:
        qs = qs.filter(substation_id=substation_filter)
    if feeder_filter:
        qs = qs.filter(feeder_id=feeder_filter)
    if tp_filter:
        qs = qs.filter(tp_id=tp_filter)
    if group_filter:
        qs = qs.filter(groups__id=group_filter)

    # Потоковая запись (write_only): память не растёт с числом строк,
    # лимит самого Excel - 1 048 575 строк данных.
    from io import BytesIO
    from openpyxl.cell import WriteOnlyCell

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Реестр приборов")

    headers = [
        'Заводской номер', 'Тип ПУ', 'Статус', 'Категория', 'Район', 'Подстанция',
        'Фидер', 'ТП', 'Группы', 'Ток', 'Фазность', 'Статус связи', 'Дата добавления'
    ]
    widths = [18, 20, 14, 16, 20, 22, 16, 14, 24, 14, 10, 14, 20]
    for idx, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(idx)].width = w
    head_cells = []
    for header in headers:
        cell = WriteOnlyCell(ws, value=header)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill(start_color='2d3e50', end_color='2d3e50', fill_type='solid')
        cell.alignment = Alignment(horizontal='center')
        head_cells.append(cell)
    ws.append(head_cells)

    for device in qs.order_by('id').iterator(chunk_size=2000):
        groups = ', '.join(g.name for g in device.groups.all())
        ws.append([
            device.serial_number,
            device.model.model_name if device.model else '',
            device.status,
            device.get_category_display(),
            device.region.name if device.region else '',
            device.substation.name if device.substation else '',
            device.feeder.name if device.feeder else '',
            device.tp.name if device.tp else '',
            groups,
            device.nominal_current or '',
            device.model.phases if device.model else '',
            'Онлайн' if device.id in recent_ids else 'Оффлайн',
            device.created_at.strftime('%Y-%m-%d %H:%M:%S') if device.created_at else '',
        ])

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    response = HttpResponse(
        output.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="registry_devices_{timezone.now().date()}.xlsx"'
    return response


@login_required
@user_passes_test(is_allowed_user)
def bulk_update_category_page(request):
    if request.method == 'POST':
        serials_text = request.POST.get('serials', '').strip()
        new_category = request.POST.get('category', '')
        if not serials_text:
            messages.error(request, 'Список серийных номеров пуст.')
            return redirect('bulk_update_category_page')
        if not new_category:
            messages.error(request, 'Выберите категорию.')
            return redirect('bulk_update_category_page')
        serials = [s.strip() for s in serials_text.split('\n') if s.strip()]
        updated = 0
        not_found = []
        for sn in serials:
            device = Device.objects.filter(serial_number=sn).first()
            if device:
                device.category = new_category
                device.save()
                updated += 1
            else:
                not_found.append(sn)
        if updated:
            messages.success(request, f'Обновлено устройств: {updated}')
        if not_found:
            messages.warning(request, f'Не найдены: {", ".join(not_found[:10])}')
        return redirect('bulk_update_category_page')
    # GET – показываем форму
    categories = Device.CATEGORY_CHOICES
    return render(request, 'meters/bulk_update_category.html', {'categories': categories})


@require_GET
def search_devices(request):
    """API для поиска устройств по заводскому номеру (автодополнение)."""
    term = request.GET.get('term', '').strip()
    if not term or len(term) < 2:
        return JsonResponse([], safe=False)
    devices = Device.objects.filter(
        serial_number__icontains=term,
        status='active'
    ).order_by('serial_number')[:20]
    data = [{'id': d.id, 'text': d.serial_number} for d in devices]
    return JsonResponse(data, safe=False)
