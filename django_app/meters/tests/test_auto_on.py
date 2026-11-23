"""Автовключение при оплате долга (check_debts_auto_on)."""
import io
from datetime import timedelta

from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from meters.management.commands.check_debts_auto_on import OFF_WINDOW_DAYS, RETRY_PAUSE_HOURS
from meters.models import Device, Region, RelayCommandHistory, RelayTask, Subscriber


class AutoOnTests(TestCase):
    def setUp(self):
        self.device = Device.objects.create(serial_number='61002510')
        self.sub = Subscriber.objects.create(device=self.device, account_number='1', full_name='Иванов', debt=0)
        self.now = timezone.now()
        # по умолчанию район без базы 1С: данные из Subscriber, сети нет
        patcher = mock.patch('meters.onec_service.OneCService.has_1c', return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_command(self):
        call_command('check_debts_auto_on', stdout=io.StringIO())

    def history(self, action='off', status='success', device=None):
        return RelayCommandHistory.objects.create(device=device or self.device, action=action, status=status)

    def on_tasks(self):
        return RelayTask.objects.filter(device=self.device, action='on')

    def task(self, action, status, payload=None, minutes=0):
        return RelayTask.objects.create(
            device=self.device, action=action, status=status, hexing_payload=payload,
            scheduled_at=self.now + timedelta(minutes=minutes),
        )

    def test_paid_subscriber_after_off_gets_on_task(self):
        self.history('off', 'success')
        self.run_command()
        task = self.on_tasks().get()
        self.assertEqual((task.status, task.created_by, float(task.debt_snapshot)), ('pending', None, 0.0))
        self.assertLessEqual(task.scheduled_at, timezone.now())

    def test_off_with_pending_status_also_counts(self):
        self.history('off', 'pending')
        self.run_command()
        self.assertEqual(self.on_tasks().count(), 1)

    def test_subscriber_with_debt_is_skipped(self):
        Subscriber.objects.filter(id=self.sub.id).update(debt=100)
        self.history('off')
        self.run_command()
        self.assertEqual(self.on_tasks().count(), 0)

    def test_no_history_is_skipped(self):
        self.run_command()
        self.assertEqual(RelayTask.objects.count(), 0)

    def test_last_command_on_is_skipped(self):
        self.history('off')
        self.history('on')
        self.run_command()
        self.assertEqual(self.on_tasks().count(), 0)

    def test_failed_off_is_not_a_reason_to_enable(self):
        self.history('off', 'error')
        self.run_command()
        self.assertEqual(self.on_tasks().count(), 0)

    def test_active_on_task_is_not_duplicated(self):
        self.history('off')
        self.task('on', 'pending')
        self.run_command()
        self.assertEqual(self.on_tasks().count(), 1)
        RelayTask.objects.all().delete()
        self.task('on', 'processing')
        self.run_command()
        self.assertEqual(self.on_tasks().count(), 1)

    def test_no_loop_after_failed_on_task(self):
        self.history('off')
        self.task('on', 'error')
        self.run_command()
        self.run_command()
        self.assertEqual(self.on_tasks().count(), 1)  # новая задача не создана

    def test_retry_after_pause_expires(self):
        self.history('off')
        failed = self.task('on', 'error')
        old = timezone.now() - timedelta(hours=RETRY_PAUSE_HOURS + 1)
        RelayTask.objects.filter(id=failed.id).update(updated_at=old)
        self.run_command()
        self.assertEqual(self.on_tasks().count(), 2)

    def test_pending_off_tasks_are_cancelled(self):
        self.history('off')
        waiting = self.task('off', 'pending', minutes=60)
        self.run_command()
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, 'cancelled')
        self.assertIn('оплатил', waiting.error_message)

    def test_off_in_flight_is_kept_and_on_goes_after_it(self):
        self.history('off')
        running = self.task('off', 'processing')
        phase2 = self.task('off', 'pending', payload='{"provider": "cenergo"}', minutes=11)
        self.run_command()
        running.refresh_from_db()
        phase2.refresh_from_db()
        self.assertEqual((running.status, phase2.status), ('processing', 'pending'))
        new = self.on_tasks().get()
        self.assertGreaterEqual(new.scheduled_at, phase2.scheduled_at + timedelta(minutes=1))

    def test_other_subscribers_do_not_affect_each_other(self):
        other_device = Device.objects.create(serial_number='99999999')
        Subscriber.objects.create(device=other_device, account_number='2', debt=500)
        self.history('off')
        self.history('off', device=other_device)
        self.run_command()
        self.assertEqual(self.on_tasks().count(), 1)
        self.assertEqual(RelayTask.objects.filter(device=other_device).count(), 0)


class AutoOn1CTests(TestCase):
    """Район с базой 1С: оплата подтверждается ответом checkdebt."""

    def setUp(self):
        self.region = Region.objects.create(name='03 Сузак')
        self.device = Device.objects.create(serial_number='086263509633', region=self.region,
                                            askue_id='26', api_id='SR')
        for name, ret in (('has_1c', True),):
            p = mock.patch(f'meters.onec_service.OneCService.{name}', return_value=ret)
            p.start()
            self.addCleanup(p.stop)

    def history(self, action='off', status='success', days_ago=0, device=None):
        h = RelayCommandHistory.objects.create(device=device or self.device, action=action, status=status)
        RelayCommandHistory.objects.filter(id=h.id).update(created_at=timezone.now() - timedelta(days=days_ago))

    def run_command(self, known):
        with mock.patch('meters.onec_service.OneCService.known_debts', side_effect=known) as kd:
            call_command('check_debts_auto_on', stdout=io.StringIO())
        return kd

    def on_tasks(self):
        return RelayTask.objects.filter(device=self.device, action='on')

    def test_paid_in_1c_creates_on_task_with_payload(self):
        self.history()
        kd = self.run_command(lambda region, meters: {'086263509633': 0.0})
        task = self.on_tasks().get()
        self.assertEqual((task.status, float(task.debt_snapshot)), ('pending', 0.0))
        self.assertEqual(kd.call_args.args[0], '03 Сузак')
        self.assertEqual(kd.call_args.args[1], [{'serial_number': '086263509633',
                                                 'askue_type_id': '26', 'askue_type_str': 'SR'}])

    def test_on_task_inherits_account_and_name_from_off_task(self):
        self.history()
        off = RelayTask.objects.create(device=self.device, action='off', status='success',
                                       scheduled_at=timezone.now(), account_snapshot='0800056',
                                       name_snapshot='Иванов И.')
        self.run_command(lambda region, meters: {'086263509633': 0.0})
        task = self.on_tasks().get()
        self.assertEqual((task.account_snapshot, task.name_snapshot), ('0800056', 'Иванов И.'))

    def test_still_in_debt_is_not_enabled(self):
        self.history()
        self.run_command(lambda region, meters: {'086263509633': 241.25})
        self.assertEqual(self.on_tasks().count(), 0)

    def test_meter_missing_in_1c_response_is_not_enabled(self):
        self.history()
        self.run_command(lambda region, meters: {})
        self.assertEqual(self.on_tasks().count(), 0)

    def test_1c_unavailable_creates_nothing(self):
        self.history()

        def boom(region, meters):
            raise Exception('1С недоступна')
        self.run_command(boom)
        self.assertEqual(RelayTask.objects.count(), 0)

    def test_off_older_than_window_is_not_checked(self):
        self.history(days_ago=OFF_WINDOW_DAYS + 1)
        kd = self.run_command(lambda region, meters: {'086263509633': 0.0})
        kd.assert_not_called()
        self.assertEqual(RelayTask.objects.count(), 0)

    def test_off_inside_window_is_checked(self):
        self.history(days_ago=OFF_WINDOW_DAYS - 1)
        self.run_command(lambda region, meters: {'086263509633': 0.0})
        self.assertEqual(self.on_tasks().count(), 1)

    def test_already_enabled_after_off_is_not_checked(self):
        self.history('off', days_ago=3)
        self.history('on', days_ago=1)
        kd = self.run_command(lambda region, meters: {'086263509633': 0.0})
        kd.assert_not_called()

    def test_one_request_per_region_batch(self):
        other = Device.objects.create(serial_number='61002510', region=self.region, askue_id='25', api_id='UK')
        self.history()
        self.history(device=other)
        kd = self.run_command(lambda region, meters: {m['serial_number']: 0.0 for m in meters})
        self.assertEqual(kd.call_count, 1)
        self.assertEqual(RelayTask.objects.filter(action='on').count(), 2)
