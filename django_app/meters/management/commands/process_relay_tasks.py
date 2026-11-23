import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction, close_old_connections

from meters.models import RelayTask, RelayCommandHistory
from meters.hexing_service import HexingService
from meters.sunrise_service import SunRiseService, STATE_ON as SUNRISE_STATE_ON, STATE_READY as SUNRISE_STATE_READY
from meters.risesun_service import RiseSunService
from meters.cenergo_service import CEnergoService
from meters.sanxing_service import SanxingService
from meters.relay_queue import READY_NOTE, READY_EXPIRE_HOURS, is_ct_meter, CT_METER_TEXT


class Command(BaseCommand):
    help = 'Обработчик очереди задач на управление реле (Многопоточный)'
    STUCK_TIMEOUT_MIN = 30  # через сколько минут 'processing' считается зависшей
    CENERGO_WINDOW_MIN = 10  # окно повторов на стороне cEnergo (настройка сервера cEnergo)
    SUNRISE_POLL_SEC = 60  # интервал проверки состояния реле после отправки команды SunRise
    READY_NOTE = READY_NOTE
    SUNRISE_WAIT_MIN = 5   # сколько ждём подтверждения состояния, потом ошибка и обычные ретраи
    RISESUN_POLL_SEC = 60  # интервал опроса статуса пакета MMWS (регламент: не чаще 1 раза в 10 сек)
    RISESUN_WAIT_MIN = 15  # сколько ждём терминального статуса пакета, потом ошибка и обычные ретраи
    HEXING_RESULT_DELAY_SEC = 125  # через сколько запрашивать результат команды Hexing (по регламенту ~2 мин)

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Обработать пачку задач и выйти')
        parser.add_argument('--batch-size', type=int, default=50, help='Сколько задач брать за раз')

    def handle(self, *args, **options):
        # Берем размер пачки из аргументов (по умолчанию 50)
        self.batch_size = options['batch_size']
        
        if options.get('once'):
            self.process_batch()
            return

        self.stdout.write("🤖 Многопоточный воркер реле запущен в режиме демона...")
        while True:
            self.process_batch()
            time.sleep(5)

    def expire_ready_tasks(self):
        """
        Задачи включения, ждущие нажатия кнопки дольше READY_EXPIRE_HOURS: читаем состояние счётчика.
        Включён — пометку снимаем (задача просто «Успешно»). Иначе (кнопку не нажали или счётчик
        не ответил) — задача получает статус «Ошибка» с причиной; «Повтор» отправит команду заново.
        """
        limit = timezone.now() - timedelta(hours=READY_EXPIRE_HOURS)
        stale = RelayTask.objects.filter(
            status='success', action='on', error_message=READY_NOTE, updated_at__lt=limit
        ).select_related('device')[:20]
        for task in stale:
            try:
                state = SunRiseService().get_relay_state(task.device.serial_number)
                if state == SUNRISE_STATE_ON:
                    reason = None
                elif state == SUNRISE_STATE_READY:
                    reason = f'Кнопка на счётчике не нажата за {READY_EXPIRE_HOURS} ч (состояние: готов к включению)'
                else:
                    reason = f'Включение не подтверждено через {READY_EXPIRE_HOURS} ч (состояние: отключено)'
            except Exception as e:
                reason = f'Не удалось проверить состояние через {READY_EXPIRE_HOURS} ч: {str(e)[:300]}'

            if reason is None:
                task.error_message = None
                task.save(update_fields=['error_message', 'updated_at'])
            else:
                task.status = 'error'
                task.error_message = reason
                task.save(update_fields=['status', 'error_message', 'updated_at'])
            RelayCommandHistory.objects.filter(
                device=task.device, action='on', status='success', error_message=READY_NOTE
            ).update(error_message=reason, status='success' if reason is None else 'error')

    def process_batch(self):
        now = timezone.now()

        try:
            self.expire_ready_tasks()
        except Exception as e:
            self.stderr.write(self.style.WARNING(f"Проверка задач «ждёт кнопку»: {e}"))

        # Возвращаем в очередь «зависшие» задачи (воркер упал/был перезапущен
        # посреди обработки). Hexing ждёт ~2 мин, поэтому порог с запасом.
        stuck = RelayTask.objects.filter(
            status='processing',
            updated_at__lt=now - timedelta(minutes=self.STUCK_TIMEOUT_MIN),
        ).update(status='pending', updated_at=now)
        if stuck:
            self.stderr.write(self.style.WARNING(
                f"♻️ Возвращено в очередь зависших задач: {stuck}"
            ))

        # Берем пачку задач, время которых пришло
        with transaction.atomic():
            tasks = list(RelayTask.objects.select_for_update(skip_locked=True).filter(
                status='pending',
                scheduled_at__lte=now
            ).order_by('scheduled_at')[:self.batch_size])

            if not tasks:
                return

            # Сразу помечаем их как processing, чтобы другие воркеры их не взяли
            task_ids = [t.id for t in tasks]
            # update() не трогает auto_now, поэтому updated_at ставим явно
            RelayTask.objects.filter(id__in=task_ids).update(status='processing', updated_at=now)

        self.stdout.write(f"📦 Взято в работу задач: {len(tasks)}")
        
        # Счетчики для статистики
        success_count = 0
        error_count = 0
        cancelled_count = 0
        retry_count = 0
        deferred_count = 0

        # Обрабатываем пачку в пуле потоков (5 потоков параллельно)
        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(self._execute_task_safe, tasks))
            
            for res in results:
                if res['status'] == 'success':
                    success_count += 1
                elif res['status'] == 'cancelled':
                    cancelled_count += 1
                elif res['status'] == 'retry':
                    retry_count += 1
                elif res['status'] == 'deferred':
                    deferred_count += 1
                else:
                    error_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"✅ Пачка: Успешно: {success_count}, Ошибок: {error_count}, "
            f"Отложено на повтор: {retry_count}, Ждут результата Hexing: {deferred_count}, "
            f"Отменено (долг): {cancelled_count}"
        ))

    def _cancel_if_debt_paid(self, task):
        """
        Проверяет актуальный долг в 1С перед отключением.
        Возвращает True, если задача отменена (долг оплачен).
        Если 1С недоступна — бросает исключение (задача уйдёт в ретрай,
        отключать «наугад» нельзя).
        """
        from meters.onec_service import OneCService

        device = task.device
        region_name = device.region.name if device.region else ''
        onec = OneCService()

        meters_payload = [{
            "serial_number": device.serial_number,
            "askue_type_id": device.askue_id or '',
            "askue_type_str": device.api_id or '',
        }]

        try:
            debts = onec.check_debts(region_name, meters_payload)
        except Exception as e:
            raise Exception(f"Не удалось проверить долг в 1С: {e}")

        actual_debt = debts.get(device.serial_number, 0.0)

        # Обновляем локальную базу для истории
        sub = getattr(device, 'subscriber', None)
        if sub:
            sub.debt = actual_debt
            sub.save()

        if actual_debt <= 0:
            task.status = 'cancelled'
            task.error_message = f'Отменено: 1С сообщает, что долг оплачен (Долг: {actual_debt}).'
            task.save(update_fields=['status', 'error_message'])
            self.stdout.write(self.style.WARNING(
                f"⏭️ Задача #{task.id} ОТМЕНЕНА: 1С сообщает, что долг оплачен."
            ))
            return True
        return False

    def _execute_task_safe(self, task):
        close_old_connections()

        try:
            # Проверка актуального долга перед отключением.
            # Ошибка 1С попадает в общий except ниже → ретраи 5/10/20 мин.
            # (на второй фазе Hexing команда уже отправлена — долг не перепроверяем)
            # Страховка для задач, созданных до запрета: счётчик 5А (через ТТ) не отключить физически
            if task.action == 'off' and not task.hexing_payload and is_ct_meter(task.device):
                task.status = 'cancelled'
                task.error_message = f'Отменено: {CT_METER_TEXT}.'
                task.save(update_fields=['status', 'error_message'])
                return {'id': task.id, 'status': 'cancelled'}

            if task.action == 'off' and not task.hexing_payload and self._cancel_if_debt_paid(task):
                return {'id': task.id, 'status': 'cancelled'}

            outcome = self._execute_relay_command(task)
            if outcome == 'deferred':
                return {'id': task.id, 'status': 'deferred'}

            # УСПЕХ. 'ready' — команда принята, но счётчик ждёт нажатия кнопки на нём (SunRise, состояние 2):
            # для дистанционной работы дальше сделать нечего, поэтому не ошибка и не повтор команды
            task.status = 'success'
            task.error_message = self.READY_NOTE if outcome == 'ready' else None
            task.hexing_payload = None
            task.save(update_fields=['status', 'error_message', 'hexing_payload'])
            return {'id': task.id, 'status': 'success'}

        except Exception as e:
            # ОШИБКА
            task.error_message = str(e)[:500]
            # При повторе команда Hexing отправляется заново (новый correlation_id)
            task.hexing_payload = None
            
            # Если попыток меньше 3, ставим в очередь на повтор с нарастающим интервалом
            if task.retry_count < 3:
                # Умная задержка: 1-я попытка +5 мин, 2-я +10 мин, 3-я +20 мин
                delays = [5, 10, 20]
                delay_minutes = delays[task.retry_count]
                
                task.retry_count += 1
                task.status = 'pending'
                task.scheduled_at = timezone.now() + timedelta(minutes=delay_minutes)
                task.save(update_fields=['status', 'scheduled_at', 'retry_count', 'error_message', 'hexing_payload'])
                
                self.stderr.write(self.style.WARNING(
                    f"⚠️ Задача #{task.id} ({task.device.serial_number}): {e}. "
                    f"Повтор через {delay_minutes} мин (попытка {task.retry_count}/3)."
                ))
                return {'id': task.id, 'status': 'retry'}
                
            # Если это была 3-я попытка, окончательно фиксируем ошибку
            else:
                task.status = 'error'
                task.save(update_fields=['status', 'error_message', 'hexing_payload'])
                self.stderr.write(self.style.ERROR(
                    f"❌ Задача #{task.id} ({task.device.serial_number}): {e}. "
                    f"Исчерпаны все 3 попытки."
                ))
                return {'id': task.id, 'status': 'error'}

    def _execute_relay_command(self, task):
        device = task.device
        action = task.action

        # 1. Hexing KUK (HES)
        if device.askue_id == '25' or (device.api_id and device.api_id.upper().startswith('UK')):
            service = HexingService()

            # Фаза 2: команда уже принята (ACK) — запрашиваем результат тем же XML
            if task.hexing_payload:
                payload = json.loads(task.hexing_payload)
                final = service.check_pending(payload['correlation_id'], payload['xml_b64'], action)
                if final['status'] == 'error':
                    raise Exception(final.get('message'))
                RelayCommandHistory.objects.create(
                    device=device, user=task.created_by, action=action,
                    status='success' if final['status'] == 'success' else 'pending',
                    batch_id=payload['correlation_id'],
                )
                task.hexing_payload = None
                return None

            # Фаза 1: отправка команды
            result = service.set_relay(device.serial_number, action)

            if result['status'] == 'error':
                raise Exception(result.get('message'))

            if result['status'] == 'pending':
                # По регламенту Hexing результат готов через ~2 минуты. Поток не держим:
                # возвращаем задачу в очередь, второй проход запросит результат.
                task.hexing_payload = json.dumps({
                    'correlation_id': result['correlation_id'],
                    'xml_b64': result['xml_b64'],
                })
                task.status = 'pending'
                task.error_message = None
                task.scheduled_at = timezone.now() + timedelta(seconds=self.HEXING_RESULT_DELAY_SEC)
                task.save(update_fields=['hexing_payload', 'status', 'error_message', 'scheduled_at'])
                return 'deferred'

            RelayCommandHistory.objects.create(
                device=device, user=task.created_by, action=action, status='success', batch_id=result.get('correlation_id')
            )

        # 2. SunRise
        elif device.askue_id == '26' or (device.api_id and device.api_id.upper().startswith('SR')):
            service = SunRiseService()

            # Фаза 2: команда отправлена — читаем фактическое состояние реле (в реальном
            # времени, опрос счётчика). Пока состояние не совпало или счётчик не ответил,
            # ждём до дедлайна и команду заново НЕ шлём; по дедлайну — ошибка и обычные
            # ретраи 5/10/20 мин (команда отправится заново).
            if task.hexing_payload:
                payload = json.loads(task.hexing_payload)
                ready = False
                try:
                    state = service.get_relay_state(device.serial_number)
                    if state == SUNRISE_STATE_READY and action == 'on':
                        # Режим управления реле требует нажать кнопку: удалённо включить нельзя
                        ready = True
                        note = None
                    else:
                        current_state = {SUNRISE_STATE_ON: 'on', 0: 'off', SUNRISE_STATE_READY: 'готов к включению (ждёт кнопку)'}.get(state, f'код {state}')
                        note = None if current_state == action else f"состояние: {current_state}, ожидали: {action}"
                except Exception as e:
                    note = str(e)

                if note is None:
                    # Запись «pending» из первой фазы закрываем, а не оставляем висеть рядом с «success»
                    pending = RelayCommandHistory.objects.filter(
                        device=device, action=action, status='pending').order_by('-created_at').first()
                    if pending:
                        pending.status = 'success'
                        pending.error_message = self.READY_NOTE if ready else None
                        pending.save(update_fields=['status', 'error_message'])
                    else:
                        RelayCommandHistory.objects.create(
                            device=device, user=task.created_by, action=action, status='success',
                            error_message=self.READY_NOTE if ready else None
                        )
                    task.hexing_payload = None
                    return 'ready' if ready else None

                if time.time() > payload['deadline']:
                    raise Exception(f"SunRise: команда не подтверждена за {self.SUNRISE_WAIT_MIN} мин ({note})")

                task.status = 'pending'
                task.scheduled_at = timezone.now() + timedelta(seconds=self.SUNRISE_POLL_SEC)
                task.save(update_fields=['status', 'scheduled_at'])
                return 'deferred'

            # Фаза 1: отправка команды (исключение здесь — обычный ретрай)
            service.set_relay(device.serial_number, action)
            RelayCommandHistory.objects.create(
                device=device, user=task.created_by, action=action, status='pending',
                error_message='Команда отправлена в SunRise, ждём подтверждения состояния реле'
            )
            task.hexing_payload = json.dumps({
                'provider': 'sunrise',
                'deadline': time.time() + self.SUNRISE_WAIT_MIN * 60,
            })
            task.status = 'pending'
            task.error_message = None
            task.scheduled_at = timezone.now() + timedelta(seconds=self.SUNRISE_POLL_SEC)
            task.save(update_fields=['hexing_payload', 'status', 'error_message', 'scheduled_at'])
            return 'deferred'

        # 3. RiseSun
        elif device.askue_id == '24' or (device.api_id and device.api_id.upper().startswith('RS')):
            service = RiseSunService()

            # Фаза 2: пакет принят MMWS (202) — опрашиваем его статус (регламент: не чаще раза
            # в 10 сек, терминальные статусы completed / completed_with_errors / failed)
            if task.hexing_payload:
                payload = json.loads(task.hexing_payload)
                try:
                    res = service.get_item_result(payload['batch_id'], device.serial_number)
                except Exception as e:
                    # Сбой опроса статуса не повод слать команду заново: ждём до дедлайна
                    res = {'state': 'pending', 'note': str(e)}

                if res['state'] == 'done':
                    RelayCommandHistory.objects.create(
                        device=device, user=task.created_by, action=action,
                        status='success', batch_id=payload['batch_id']
                    )
                    task.hexing_payload = None
                    return None
                if res['state'] == 'failed':
                    raise Exception(f"RiseSun: {res['error']}")

                if time.time() > payload['deadline']:
                    note = f" ({res['note']})" if res.get('note') else ''
                    raise Exception(
                        f"RiseSun: пакет {payload['batch_id']} не завершился за "
                        f"{self.RISESUN_WAIT_MIN} мин{note}"
                    )
                task.status = 'pending'
                task.scheduled_at = timezone.now() + timedelta(seconds=self.RISESUN_POLL_SEC)
                task.save(update_fields=['status', 'scheduled_at'])
                return 'deferred'

            # Фаза 1: постановка пакета
            batch_id = service.set_relay(device.serial_number, action)
            RelayCommandHistory.objects.create(
                device=device, user=task.created_by, action=action, status='pending', batch_id=batch_id
            )
            task.hexing_payload = json.dumps({
                'provider': 'risesun',
                'batch_id': batch_id,
                'deadline': time.time() + self.RISESUN_WAIT_MIN * 60,
            })
            task.status = 'pending'
            task.error_message = None
            task.scheduled_at = timezone.now() + timedelta(seconds=self.RISESUN_POLL_SEC)
            task.save(update_fields=['hexing_payload', 'status', 'error_message', 'scheduled_at'])
            return 'deferred'

        # 4. cEnergo (Энергомера)
        elif device.askue_id == '23' or (device.api_id and device.api_id.upper().startswith('EM')):
            service = CEnergoService()

            # Фаза 2: cEnergo приняла команду в свою очередь (повтор каждые 5 мин, окно 10 мин,
            # потом «Сбой отправки команды управления реле»). Итог сама не сообщает —
            # проверяем по фактическому состоянию счётчика (GetMeterState, только чтение).
            if task.hexing_payload:
                state = service.get_status(device.serial_number)
                if state != action:
                    raise Exception(
                        f"cEnergo: команда не выполнена за {self.CENERGO_WINDOW_MIN} мин "
                        f"(состояние счётчика: {state}, ожидали: {action})"
                    )
                RelayCommandHistory.objects.create(
                    device=device, user=task.created_by, action=action, status='success'
                )
                task.hexing_payload = None
                return None

            result = service.set_status(device.serial_number, action)

            if result == 'success':
                time.sleep(5)
                RelayCommandHistory.objects.create(device=device, user=task.created_by, action=action, status='success')
            elif result == 'queued':
                # Счётчик спит: cEnergo будет пытаться сама ~10 минут. Возвращаем задачу в очередь
                # и проверяем итог второй фазой; если не сработало — обычные ретраи 5/10/20 мин
                # (команда отправится заново, для отключения долг проверится ещё раз).
                RelayCommandHistory.objects.create(
                    device=device, user=task.created_by, action=action, status='pending',
                    error_message=f"cEnergo взяла в работу (авто-повтор ~{self.CENERGO_WINDOW_MIN} мин)"
                )
                task.hexing_payload = json.dumps({'provider': 'cenergo'})
                task.status = 'pending'
                task.error_message = None
                task.scheduled_at = timezone.now() + timedelta(minutes=self.CENERGO_WINDOW_MIN + 1)
                task.save(update_fields=['hexing_payload', 'status', 'error_message', 'scheduled_at'])
                self.stdout.write(
                    f"  ⏳ cEnergo приняла задачу (счётчик спит). Итог проверим через {self.CENERGO_WINDOW_MIN + 1} мин."
                )
                return 'deferred'

        # 5. Sanxing (SanPlat) - Работает быстро через HTTP (requests + ddddocr)
        elif device.askue_id == '22' or (device.api_id and device.api_id.upper().startswith('SX')):
            service = SanxingService()
            service.set_relay(device.serial_number, action)
            time.sleep(3) # Короткая пауза для сервера Sanxing
            RelayCommandHistory.objects.create(device=device, user=task.created_by, action=action, status='success')
            
        else:
            raise Exception(f"Неподдерживаемый тип счетчика: askue_id={device.askue_id}")