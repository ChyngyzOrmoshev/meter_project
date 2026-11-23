"""Воркер очереди реле: повторы, проверка долга, двухфазная схема по провайдерам.

Провайдеры подменяются заглушками: ни одна реальная команда не уходит.
"""
import io
import json
import time
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from meters.management.commands import process_relay_tasks as worker
from meters.management.commands.process_relay_tasks import Command
from meters.models import Device, RelayCommandHistory, RelayTask, Subscriber
from meters.relay_queue import READY_NOTE

MODULE = 'meters.management.commands.process_relay_tasks'


class SyncExecutor:
    """Вместо пула потоков: выполняет задачи последовательно в потоке теста."""
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def map(self, fn, items):
        return [fn(i) for i in items]


class WorkerBase(TestCase):
    ASKUE = '25'

    def setUp(self):
        self.user = User.objects.create_user('operator', password='x')
        self.device = Device.objects.create(serial_number='61002510', askue_id=self.ASKUE)
        self.cmd = Command(stdout=io.StringIO(), stderr=io.StringIO())
        self.cmd.batch_size = 50
        patcher = mock.patch('time.sleep')
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_task(self, action='on', payload=None, retry_count=0, scheduled=None, status='pending', device=None):
        return RelayTask.objects.create(
            device=device or self.device, action=action, status=status, retry_count=retry_count,
            scheduled_at=scheduled or timezone.now() - timedelta(minutes=1),
            hexing_payload=json.dumps(payload) if payload is not None else None,
            created_by=self.user,
        )

    def run_task(self, task):
        result = self.cmd._execute_task_safe(task)
        task.refresh_from_db()
        return result['status']

    def patch_service(self, name):
        patcher = mock.patch(f'{MODULE}.{name}')
        service_cls = patcher.start()
        self.addCleanup(patcher.stop)
        return service_cls.return_value

    def history(self):
        return list(RelayCommandHistory.objects.filter(device=self.device).order_by('id'))


class RetryAndErrorTests(WorkerBase):
    ASKUE = '99'  # неподдерживаемый тип: команда всегда падает

    def test_unsupported_meter_goes_to_retry(self):
        task = self.make_task()
        self.assertEqual(self.run_task(task), 'retry')
        self.assertEqual(task.status, 'pending')
        self.assertEqual(task.retry_count, 1)
        self.assertIn('Неподдерживаемый тип', task.error_message)
        self.assertEqual(self.history(), [])  # ошибки в историю не пишутся

    def test_retry_delays_are_5_10_20_minutes_then_error(self):
        task = self.make_task()
        for expected_minutes in (5, 10, 20):
            before = timezone.now()
            self.assertEqual(self.run_task(task), 'retry')
            delay = (task.scheduled_at - before).total_seconds() / 60
            self.assertAlmostEqual(delay, expected_minutes, delta=0.5)
        self.assertEqual(task.retry_count, 3)
        self.assertEqual(self.run_task(task), 'error')
        self.assertEqual(task.status, 'error')

    def test_error_clears_payload_so_command_is_resent(self):
        task = self.make_task(payload={'provider': 'x'})
        self.run_task(task)
        self.assertIsNone(task.hexing_payload)


class DebtCheckTests(WorkerBase):
    ASKUE = '22'  # Sanxing: после проверки долга просто вызывается set_relay

    def setUp(self):
        super().setUp()
        self.sanxing = self.patch_service('SanxingService')
        patcher = mock.patch('meters.onec_service.OneCService.check_debts')
        self.check_debts = patcher.start()
        self.addCleanup(patcher.stop)

    def test_off_with_paid_debt_is_cancelled_without_command(self):
        self.check_debts.return_value = {'61002510': 0.0}
        task = self.make_task('off')
        self.assertEqual(self.run_task(task), 'cancelled')
        self.assertEqual(task.status, 'cancelled')
        self.assertIn('долг оплачен', task.error_message)
        self.sanxing.set_relay.assert_not_called()

    def test_cancel_stores_debt_answered_by_1c(self):
        self.check_debts.return_value = {'61002510': -83.17}
        task = self.make_task('off')
        self.run_task(task)
        task.refresh_from_db()
        self.assertEqual(float(task.debt_snapshot), -83.17)
        self.assertIn('-83.17', task.error_message)

    def test_off_with_debt_sends_command(self):
        self.check_debts.return_value = {'61002510': 1500.0}
        task = self.make_task('off')
        self.assertEqual(self.run_task(task), 'success')
        self.sanxing.set_relay.assert_called_once_with('61002510', 'off')
        self.assertEqual([h.action for h in self.history()], ['off'])

    def test_1c_unavailable_means_no_disconnect_and_retry(self):
        self.check_debts.side_effect = Exception('1С недоступна')
        task = self.make_task('off')
        self.assertEqual(self.run_task(task), 'retry')
        self.assertIn('Не удалось проверить долг в 1С', task.error_message)
        self.sanxing.set_relay.assert_not_called()

    def test_on_never_checks_debt(self):
        task = self.make_task('on')
        self.assertEqual(self.run_task(task), 'success')
        self.check_debts.assert_not_called()

    def test_second_phase_does_not_recheck_debt(self):
        # payload есть — команда уже отправлена, долг не перепроверяем
        task = self.make_task('off', payload={'provider': 'x'})
        self.run_task(task)
        self.check_debts.assert_not_called()

    def test_local_debt_updated_from_1c(self):
        Subscriber.objects.create(device=self.device, account_number='1', debt=100)
        self.check_debts.return_value = {'61002510': 250.0}
        self.run_task(self.make_task('off'))
        self.assertEqual(float(Subscriber.objects.get(device=self.device).debt), 250.0)


class HexingTests(WorkerBase):
    ASKUE = '25'

    def setUp(self):
        super().setUp()
        self.hexing = self.patch_service('HexingService')

    def test_phase1_pending_defers_and_stores_payload(self):
        self.hexing.set_relay.return_value = {'status': 'pending', 'correlation_id': 'c1', 'xml_b64': 'eG1s'}
        task = self.make_task('on')
        before = timezone.now()
        self.assertEqual(self.run_task(task), 'deferred')
        self.assertEqual(task.status, 'pending')
        self.assertEqual(json.loads(task.hexing_payload), {'correlation_id': 'c1', 'xml_b64': 'eG1s'})
        self.assertAlmostEqual((task.scheduled_at - before).total_seconds(), Command.HEXING_RESULT_DELAY_SEC, delta=5)
        self.assertEqual(self.history(), [])

    def test_phase1_immediate_success(self):
        self.hexing.set_relay.return_value = {'status': 'success', 'correlation_id': 'c1'}
        task = self.make_task('on')
        self.assertEqual(self.run_task(task), 'success')
        self.assertEqual(self.history()[0].status, 'success')
        self.assertEqual(self.history()[0].user, self.user)

    def test_phase1_error_goes_to_retry(self):
        self.hexing.set_relay.return_value = {'status': 'error', 'message': 'нет ACK'}
        task = self.make_task('on')
        self.assertEqual(self.run_task(task), 'retry')
        self.assertEqual(task.error_message, 'нет ACK')

    def test_phase2_success(self):
        self.hexing.check_pending.return_value = {'status': 'success'}
        task = self.make_task('on', payload={'correlation_id': 'c1', 'xml_b64': 'eG1s'})
        self.assertEqual(self.run_task(task), 'success')
        self.hexing.check_pending.assert_called_once_with('c1', 'eG1s', 'on')
        self.hexing.set_relay.assert_not_called()  # команда повторно не отправляется
        self.assertEqual(self.history()[0].batch_id, 'c1')
        self.assertIsNone(task.hexing_payload)

    def test_phase2_error_resets_payload_and_retries(self):
        self.hexing.check_pending.return_value = {'status': 'error', 'message': 'отказ'}
        task = self.make_task('on', payload={'correlation_id': 'c1', 'xml_b64': 'eG1s'})
        self.assertEqual(self.run_task(task), 'retry')
        self.assertIsNone(task.hexing_payload)
        self.assertEqual(self.history(), [])


class SunRiseTests(WorkerBase):
    ASKUE = '26'

    def setUp(self):
        super().setUp()
        self.sunrise = self.patch_service('SunRiseService')

    def test_phase1_sends_and_defers_with_pending_history(self):
        task = self.make_task('off')
        with mock.patch('meters.onec_service.OneCService.check_debts', return_value={'61002510': 10.0}):
            self.assertEqual(self.run_task(task), 'deferred')
        self.sunrise.set_relay.assert_called_once_with('61002510', 'off')
        payload = json.loads(task.hexing_payload)
        self.assertEqual(payload['provider'], 'sunrise')
        self.assertGreater(payload['deadline'], time.time())
        self.assertEqual([h.status for h in self.history()], ['pending'])

    def test_phase2_state_matches_is_success(self):
        self.sunrise.get_relay_state.return_value = 0  # реле отключено
        task = self.make_task('off', payload={'deadline': time.time() + 100})
        self.assertEqual(self.run_task(task), 'success')
        self.sunrise.set_relay.assert_not_called()
        self.assertEqual([h.status for h in self.history()], ['success'])

    def test_phase2_waits_until_deadline_without_resending(self):
        self.sunrise.get_relay_state.return_value = 1  # всё ещё включено
        task = self.make_task('off', payload={'deadline': time.time() + 100})
        self.assertEqual(self.run_task(task), 'deferred')
        self.assertEqual(task.status, 'pending')
        self.sunrise.set_relay.assert_not_called()

    def test_phase2_after_deadline_is_error_and_retry(self):
        self.sunrise.get_relay_state.return_value = 1
        task = self.make_task('off', payload={'deadline': time.time() - 1})
        self.assertEqual(self.run_task(task), 'retry')
        self.assertIn('не подтверждена', task.error_message)

    def test_phase2_meter_not_responding_keeps_waiting(self):
        self.sunrise.get_relay_state.side_effect = Exception('счётчик не отвечает')
        task = self.make_task('on', payload={'deadline': time.time() + 100})
        self.assertEqual(self.run_task(task), 'deferred')

    def test_phase2_ready_state_after_on_is_success_with_note(self):
        """Состояние 2 (готов к включению, нужна кнопка) после команды on — не ошибка и не повтор."""
        self.sunrise.get_relay_state.return_value = 2
        task = self.make_task('on', payload={'deadline': time.time() + 100})
        self.assertEqual(self.run_task(task), 'success')
        task.refresh_from_db()
        self.assertEqual(task.status, 'success')
        self.assertIn('кнопки', task.error_message)
        self.sunrise.set_relay.assert_not_called()
        h = self.history()[0]
        self.assertEqual(h.status, 'success')
        self.assertIn('кнопки', h.error_message)

    def test_phase2_closes_pending_history_instead_of_adding(self):
        RelayCommandHistory.objects.create(device=self.device, action='on', status='pending')
        self.sunrise.get_relay_state.return_value = 1
        task = self.make_task('on', payload={'deadline': time.time() + 100})
        self.assertEqual(self.run_task(task), 'success')
        self.assertEqual([h.status for h in self.history()], ['success'])

    def test_phase2_ready_state_after_off_is_not_confirmation(self):
        self.sunrise.get_relay_state.return_value = 2
        task = self.make_task('off', payload={'deadline': time.time() + 100})
        self.assertEqual(self.run_task(task), 'deferred')


class ReadyExpireTests(WorkerBase):
    """Задача включения «ждёт кнопку» старше 6 ч: перепроверка состояния SunRise."""
    ASKUE = '26'

    def setUp(self):
        super().setUp()
        self.sunrise = self.patch_service('SunRiseService')

    def ready_task(self, hours_ago):
        task = self.make_task('on', status='success')
        RelayTask.objects.filter(id=task.id).update(
            error_message=READY_NOTE, updated_at=timezone.now() - timedelta(hours=hours_ago))
        RelayCommandHistory.objects.create(device=self.device, action='on', status='success',
                                           error_message=READY_NOTE)
        return task

    def expire(self, task):
        self.cmd.expire_ready_tasks()
        task.refresh_from_db()

    def test_recent_task_is_not_checked(self):
        task = self.ready_task(hours_ago=1)
        self.expire(task)
        self.sunrise.get_relay_state.assert_not_called()
        self.assertEqual((task.status, task.error_message), ('success', READY_NOTE))

    def test_button_pressed_clears_note(self):
        self.sunrise.get_relay_state.return_value = 1
        task = self.ready_task(hours_ago=7)
        self.expire(task)
        self.assertEqual((task.status, task.error_message), ('success', None))
        h = self.history()[0]
        self.assertEqual((h.status, h.error_message), ('success', None))

    def test_button_not_pressed_becomes_error(self):
        self.sunrise.get_relay_state.return_value = 2
        task = self.ready_task(hours_ago=7)
        self.expire(task)
        self.assertEqual(task.status, 'error')
        self.assertIn('не нажата', task.error_message)
        self.assertEqual(self.history()[0].status, 'error')

    def test_meter_off_becomes_error(self):
        self.sunrise.get_relay_state.return_value = 0
        task = self.ready_task(hours_ago=7)
        self.expire(task)
        self.assertEqual(task.status, 'error')
        self.assertIn('не подтверждено', task.error_message)

    def test_unreachable_meter_becomes_error_and_is_not_rechecked(self):
        self.sunrise.get_relay_state.side_effect = Exception('таймаут')
        task = self.ready_task(hours_ago=7)
        self.expire(task)
        self.assertEqual(task.status, 'error')
        self.assertIn('таймаут', task.error_message)
        self.sunrise.get_relay_state.reset_mock()
        self.cmd.expire_ready_tasks()
        self.sunrise.get_relay_state.assert_not_called()


class RiseSunTests(WorkerBase):
    ASKUE = '24'

    def setUp(self):
        super().setUp()
        self.risesun = self.patch_service('RiseSunService')

    def test_phase1_creates_batch_and_defers(self):
        self.risesun.set_relay.return_value = 'batch-1'
        task = self.make_task('on')
        self.assertEqual(self.run_task(task), 'deferred')
        payload = json.loads(task.hexing_payload)
        self.assertEqual((payload['provider'], payload['batch_id']), ('risesun', 'batch-1'))
        self.assertEqual(self.history()[0].batch_id, 'batch-1')

    def test_phase2_done(self):
        self.risesun.get_item_result.return_value = {'state': 'done'}
        task = self.make_task('on', payload={'batch_id': 'b1', 'deadline': time.time() + 100})
        self.assertEqual(self.run_task(task), 'success')
        self.risesun.set_relay.assert_not_called()

    def test_phase2_failed_item_goes_to_retry_with_reason(self):
        self.risesun.get_item_result.return_value = {'state': 'failed', 'error': 'Счётчик недоступен (METER_UNREACHABLE)'}
        task = self.make_task('on', payload={'batch_id': 'b1', 'deadline': time.time() + 100})
        self.assertEqual(self.run_task(task), 'retry')
        self.assertIn('METER_UNREACHABLE', task.error_message)

    def test_phase2_pending_before_deadline_defers(self):
        self.risesun.get_item_result.return_value = {'state': 'pending'}
        task = self.make_task('on', payload={'batch_id': 'b1', 'deadline': time.time() + 100})
        self.assertEqual(self.run_task(task), 'deferred')

    def test_phase2_status_error_does_not_resend_command(self):
        self.risesun.get_item_result.side_effect = Exception('MMWS 502')
        task = self.make_task('on', payload={'batch_id': 'b1', 'deadline': time.time() + 100})
        self.assertEqual(self.run_task(task), 'deferred')
        self.risesun.set_relay.assert_not_called()

    def test_phase2_after_deadline_is_error(self):
        self.risesun.get_item_result.return_value = {'state': 'pending'}
        task = self.make_task('on', payload={'batch_id': 'b1', 'deadline': time.time() - 1})
        self.assertEqual(self.run_task(task), 'retry')
        self.assertIn('не завершился', task.error_message)


class CEnergoTests(WorkerBase):
    ASKUE = '23'

    def setUp(self):
        super().setUp()
        self.cenergo = self.patch_service('CEnergoService')

    def test_success_writes_history(self):
        self.cenergo.set_status.return_value = 'success'
        task = self.make_task('on')
        self.assertEqual(self.run_task(task), 'success')
        self.assertEqual([h.status for h in self.history()], ['success'])

    def test_queued_defers_by_window_plus_one_minute(self):
        self.cenergo.set_status.return_value = 'queued'
        task = self.make_task('on')
        before = timezone.now()
        self.assertEqual(self.run_task(task), 'deferred')
        minutes = (task.scheduled_at - before).total_seconds() / 60
        self.assertAlmostEqual(minutes, Command.CENERGO_WINDOW_MIN + 1, delta=0.5)
        self.assertEqual(json.loads(task.hexing_payload), {'provider': 'cenergo'})
        self.assertEqual([h.status for h in self.history()], ['pending'])

    def test_phase2_state_matches_is_success(self):
        self.cenergo.get_status.return_value = 'on'
        task = self.make_task('on', payload={'provider': 'cenergo'})
        self.assertEqual(self.run_task(task), 'success')
        self.cenergo.set_status.assert_not_called()

    def test_phase2_state_differs_is_retry_with_resend_later(self):
        self.cenergo.get_status.return_value = 'off'
        task = self.make_task('on', payload={'provider': 'cenergo'})
        self.assertEqual(self.run_task(task), 'retry')
        self.assertIn('команда не выполнена', task.error_message)
        self.assertIsNone(task.hexing_payload)


class SanxingTests(WorkerBase):
    ASKUE = '22'

    def test_command_sent_and_history_written(self):
        sanxing = self.patch_service('SanxingService')
        task = self.make_task('on')
        self.assertEqual(self.run_task(task), 'success')
        sanxing.set_relay.assert_called_once_with('61002510', 'on')
        self.assertEqual(self.history()[0].user, self.user)

    def test_offline_meter_goes_to_retry(self):
        sanxing = self.patch_service('SanxingService')
        sanxing.set_relay.side_effect = Exception('Off line(code:-2)')
        task = self.make_task('on')
        self.assertEqual(self.run_task(task), 'retry')
        self.assertEqual(self.history(), [])


class ProcessBatchTests(WorkerBase):
    ASKUE = '22'

    def setUp(self):
        super().setUp()
        self.sanxing = self.patch_service('SanxingService')
        patcher = mock.patch(f'{MODULE}.ThreadPoolExecutor', SyncExecutor)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_due_pending_tasks_are_taken(self):
        due = self.make_task('on')
        future = self.make_task('on', scheduled=timezone.now() + timedelta(hours=1),
                                device=Device.objects.create(serial_number='2', askue_id='22'))
        done = self.make_task('on', status='success',
                              device=Device.objects.create(serial_number='3', askue_id='22'))
        self.cmd.process_batch()
        for t in (due, future, done):
            t.refresh_from_db()
        self.assertEqual(due.status, 'success')
        self.assertEqual(future.status, 'pending')
        self.assertEqual(done.status, 'success')
        self.sanxing.set_relay.assert_called_once_with('61002510', 'on')

    def test_stuck_processing_task_returns_to_queue_and_runs(self):
        stuck = self.make_task('on', status='processing')
        old = timezone.now() - timedelta(minutes=Command.STUCK_TIMEOUT_MIN + 1)
        RelayTask.objects.filter(id=stuck.id).update(updated_at=old)
        self.cmd.process_batch()
        stuck.refresh_from_db()
        self.assertEqual(stuck.status, 'success')

    def test_fresh_processing_task_is_left_alone(self):
        fresh = self.make_task('on', status='processing')
        self.cmd.process_batch()
        fresh.refresh_from_db()
        self.assertEqual(fresh.status, 'processing')
        self.sanxing.set_relay.assert_not_called()

    def test_batch_size_limits_tasks(self):
        self.cmd.batch_size = 2
        for i in range(4):
            self.make_task('on', device=Device.objects.create(serial_number=f'10{i}', askue_id='22'))
        self.cmd.process_batch()
        self.assertEqual(RelayTask.objects.filter(status='success').count(), 2)
        self.assertEqual(RelayTask.objects.filter(status='pending').count(), 2)
