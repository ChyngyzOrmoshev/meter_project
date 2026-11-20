from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from meters.models import Device, RelayTask
from meters.relay_queue import (
    BUSY_DELAY_MIN, PHASE2_GAP_MIN, inflight_tasks, supersede_tasks,
)


class RelayQueueTests(TestCase):
    def setUp(self):
        self.device = Device.objects.create(serial_number='61002510')
        self.other = Device.objects.create(serial_number='99999999')
        self.now = timezone.now()

    def task(self, action='off', status='pending', payload=None, minutes=0, device=None):
        return RelayTask.objects.create(
            device=device or self.device, action=action, status=status,
            scheduled_at=self.now + timedelta(minutes=minutes), hexing_payload=payload,
        )

    def status(self, task):
        task.refresh_from_db()
        return task.status

    # --- inflight_tasks ---

    def test_inflight_processing_and_pending_with_payload(self):
        processing = self.task(status='processing')
        phase2 = self.task(payload='{"provider": "cenergo"}')
        self.task()                           # обычная pending
        self.task(payload='')                 # пустой payload — не в полёте
        self.task(status='success', payload='{"a": 1}')
        ids = set(inflight_tasks(self.device).values_list('id', flat=True))
        self.assertEqual(ids, {processing.id, phase2.id})

    def test_inflight_filters_by_action_and_device(self):
        off = self.task(action='off', status='processing')
        self.task(action='on', status='processing')
        self.task(action='off', status='processing', device=self.other)
        ids = list(inflight_tasks(self.device, actions=('off',)).values_list('id', flat=True))
        self.assertEqual(ids, [off.id])

    # --- supersede_tasks ---

    def test_no_inflight_keeps_time_and_cancels_pending(self):
        pending = self.task()
        when, busy = supersede_tasks(self.device, self.now, 'причина')
        self.assertEqual(when, self.now)
        self.assertFalse(busy)
        self.assertEqual(self.status(pending), 'cancelled')
        self.assertEqual(RelayTask.objects.get(id=pending.id).error_message, 'причина')

    def test_only_listed_actions_are_cancelled(self):
        off = self.task(action='off')
        on = self.task(action='on')
        supersede_tasks(self.device, self.now, 'x', actions=('off',))
        self.assertEqual(self.status(off), 'cancelled')
        self.assertEqual(self.status(on), 'pending')

    def test_other_device_untouched(self):
        foreign = self.task(device=self.other)
        supersede_tasks(self.device, self.now, 'x')
        self.assertEqual(self.status(foreign), 'pending')

    def test_processing_not_cancelled_and_new_task_delayed(self):
        running = self.task(status='processing')
        before = timezone.now()
        when, busy = supersede_tasks(self.device, self.now, 'x')
        self.assertEqual(self.status(running), 'processing')
        self.assertTrue(busy)
        self.assertGreaterEqual(when, before + timedelta(minutes=BUSY_DELAY_MIN))

    def test_phase2_task_not_cancelled_and_new_task_after_it(self):
        phase2 = self.task(payload='{"provider": "cenergo"}', minutes=11)
        when, busy = supersede_tasks(self.device, self.now, 'x')
        self.assertEqual(self.status(phase2), 'pending')
        self.assertTrue(busy)
        self.assertEqual(when, phase2.scheduled_at + timedelta(minutes=PHASE2_GAP_MIN))

    def test_requested_time_later_than_delay_is_kept(self):
        self.task(status='processing')
        later = self.now + timedelta(hours=2)
        when, busy = supersede_tasks(self.device, later, 'x')
        self.assertEqual(when, later)
        self.assertTrue(busy)

    def test_plain_pending_cancelled_next_to_inflight(self):
        phase2 = self.task(payload='{"p": 1}', minutes=5)
        plain = self.task()
        supersede_tasks(self.device, self.now, 'x')
        self.assertEqual(self.status(phase2), 'pending')
        self.assertEqual(self.status(plain), 'cancelled')

    def test_inflight_of_other_action_ignored_when_actions_limited(self):
        self.task(action='on', status='processing')
        when, busy = supersede_tasks(self.device, self.now, 'x', actions=('off',))
        self.assertFalse(busy)
        self.assertEqual(when, self.now)
