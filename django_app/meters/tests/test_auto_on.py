"""Автовключение при оплате долга (check_debts_auto_on)."""
import io
from datetime import timedelta

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from meters.management.commands.check_debts_auto_on import RETRY_PAUSE_HOURS
from meters.models import Device, RelayCommandHistory, RelayTask, Subscriber


class AutoOnTests(TestCase):
    def setUp(self):
        self.device = Device.objects.create(serial_number='61002510')
        self.sub = Subscriber.objects.create(device=self.device, account_number='1', full_name='Иванов', debt=0)
        self.now = timezone.now()

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
