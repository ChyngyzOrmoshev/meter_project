"""Вьюхи управления реле: доступ, районы, поиск счётчика, постановка задач, повтор, монитор."""
import json
import os
import tempfile
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.utils import timezone

from meters import views_relay
from meters.models import (
    CompareResult, Device, MeterModel, Region, RelayCommandHistory, RelayTask, Subscriber,
)
from meters.views_relay import find_relay_device, get_user_region, relay_region_denied

ACTION = '/relay-control/api/action/'
STATUS = '/relay-control/api/status/'
MASS_CREATE = '/relay-control/api/mass-create-tasks/'
MASS_SUBS = '/relay-control/api/mass-subscribers/'
BULK_START = '/relay-control/bulk-start/'
BULK_STATUS = '/relay-control/bulk-status/'
RETRY = '/relay-control/api/task-retry/'
MONITOR = '/relay-control/api/tasks-monitor/'


def local_time(minutes):
    return (timezone.now() + timedelta(minutes=minutes)).strftime('%Y-%m-%dT%H:%M:%S')


class RelayViewsBase(TestCase):
    def setUp(self):
        self.suzak = Region.objects.create(name='03 Сузак')
        self.manas = Region.objects.create(name='01 Манас')
        self.admin = User.objects.create_user('admin', password='x', is_staff=True)
        self.operator = self.user_in_groups('operator', 'operator')
        self.engineer = self.user_in_groups('engineer', 'Инженер АСКУЭ', 'ASKUE:03 Сузак')
        self.nobody = User.objects.create_user('nobody', password='x')
        self.own = Device.objects.create(serial_number='61002510', region=self.suzak)
        self.foreign = Device.objects.create(serial_number='70000111', region=self.manas)
        self.no_region = Device.objects.create(serial_number='80000222')

    def user_in_groups(self, username, *groups):
        user = User.objects.create_user(username, password='x')
        for name in groups:
            user.groups.add(Group.objects.get_or_create(name=name)[0])
        return user

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def task(self, device, action='off', status='pending', payload=None, minutes=0):
        return RelayTask.objects.create(
            device=device, action=action, status=status, hexing_payload=payload,
            scheduled_at=timezone.now() + timedelta(minutes=minutes),
        )


class AccessTests(RelayViewsBase):
    def test_anonymous_redirected_to_login(self):
        for url in ('/relay-control/', STATUS, MONITOR, BULK_STATUS):
            r = Client().get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn('/login/', r['Location'])

    def test_user_without_group_has_no_access(self):
        c = self.client_for(self.nobody)
        self.assertEqual(c.get('/relay-control/').status_code, 302)
        self.assertEqual(c.post(ACTION, {'action': 'on', 'serial': '61002510'}).status_code, 302)
        self.assertEqual(RelayTask.objects.count(), 0)

    def test_staff_operator_and_engineer_can_open_page(self):
        for user in (self.admin, self.operator, self.engineer):
            self.assertEqual(self.client_for(user).get('/relay-control/').status_code, 200, user.username)

    def test_engineer_sees_only_own_region_on_page(self):
        r = self.client_for(self.engineer).get('/relay-control/')
        self.assertEqual([x.name for x in r.context['regions']], ['03 Сузак'])
        r = self.client_for(self.admin).get('/relay-control/')
        self.assertEqual(r.context['regions'].count(), 2)


class FindRelayDeviceTests(RelayViewsBase):
    def test_exact_match(self):
        self.assertEqual(find_relay_device('61002510'), (self.own, None))

    def test_empty_serial(self):
        device, error = find_relay_device('  ')
        self.assertIsNone(device)
        self.assertIn('Не указан', error)

    def test_unique_suffix_without_leading_zeros(self):
        Device.objects.create(serial_number='086365005946')
        device, _ = find_relay_device('86365005946')
        self.assertEqual(device.serial_number, '086365005946')
        device, _ = find_relay_device('0086365005946')
        self.assertEqual(device.serial_number, '086365005946')

    def test_short_suffix_is_not_guessed(self):
        device, error = find_relay_device('2510')
        self.assertIsNone(device)
        self.assertIn('не найден', error)

    def test_ambiguous_suffix_selects_nothing(self):
        Device.objects.create(serial_number='AA12345')
        Device.objects.create(serial_number='BB12345')
        device, error = find_relay_device('12345')
        self.assertIsNone(device)
        self.assertIn('неоднозначен', error)

    def test_unknown_serial(self):
        self.assertIsNone(find_relay_device('99999999')[0])


class RegionRestrictionTests(RelayViewsBase):
    def test_get_user_region(self):
        self.assertEqual(get_user_region(self.engineer), '03 Сузак')
        self.assertIsNone(get_user_region(self.admin))
        self.assertIsNone(get_user_region(self.operator))

    def test_denied_rules(self):
        self.assertFalse(relay_region_denied(self.engineer, self.own))
        self.assertTrue(relay_region_denied(self.engineer, self.foreign))
        self.assertTrue(relay_region_denied(self.engineer, self.no_region))  # без района закрыт
        self.assertFalse(relay_region_denied(self.admin, self.foreign))
        self.assertFalse(relay_region_denied(self.operator, self.foreign))


class ApiActionTests(RelayViewsBase):
    def post(self, user, **data):
        return self.client_for(user).post(ACTION, data)

    def test_get_not_allowed(self):
        self.assertEqual(self.client_for(self.admin).get(ACTION).status_code, 405)

    def test_invalid_action(self):
        self.assertEqual(self.post(self.admin, action='x', serial='61002510').status_code, 400)

    def test_unknown_serial_is_404(self):
        self.assertEqual(self.post(self.admin, action='on', serial='99999999').status_code, 404)

    def test_creates_pending_task_without_sending_anything(self):
        before = timezone.now()
        r = self.post(self.admin, action='on', serial='61002510')
        self.assertEqual(r.status_code, 200)
        task = RelayTask.objects.get(id=r.json()['task_id'])
        self.assertEqual((task.action, task.status, task.created_by), ('on', 'pending', self.admin))
        self.assertLess((task.scheduled_at - before).total_seconds(), 5)
        self.assertEqual(RelayCommandHistory.objects.count(), 0)

    def test_engineer_own_region_ok_foreign_and_no_region_denied(self):
        self.assertEqual(self.post(self.engineer, action='on', serial='61002510').status_code, 200)
        self.assertEqual(self.post(self.engineer, action='on', serial='70000111').status_code, 403)
        self.assertEqual(self.post(self.engineer, action='on', serial='80000222').status_code, 403)
        self.assertEqual(RelayTask.objects.count(), 1)

    def test_pending_tasks_are_cancelled_by_new_command(self):
        old = self.task(self.own, 'off')
        self.post(self.admin, action='on', serial='61002510')
        old.refresh_from_db()
        self.assertEqual(old.status, 'cancelled')

    def test_same_command_in_flight_gives_409(self):
        self.task(self.own, 'off', status='processing')
        r = self.post(self.admin, action='off', serial='61002510')
        self.assertEqual(r.status_code, 409)
        self.assertEqual(RelayTask.objects.count(), 1)

    def test_same_command_waiting_second_phase_gives_409(self):
        self.task(self.own, 'on', payload='{"provider": "cenergo"}', minutes=11)
        self.assertEqual(self.post(self.admin, action='on', serial='61002510').status_code, 409)

    def test_opposite_in_flight_is_kept_and_new_command_delayed(self):
        running = self.task(self.own, 'off', status='processing')
        before = timezone.now()
        r = self.post(self.admin, action='on', serial='61002510')
        self.assertEqual(r.status_code, 200)
        self.assertIn('ещё выполняется', r.json()['message'])
        running.refresh_from_db()
        self.assertEqual(running.status, 'processing')
        new = RelayTask.objects.get(id=r.json()['task_id'])
        self.assertGreaterEqual(new.scheduled_at, before + timedelta(minutes=2))


class ApiStatusTests(RelayViewsBase):
    def get(self, user, serial):
        return self.client_for(user).get(STATUS, {'serial': serial})

    def test_unknown_and_foreign(self):
        self.assertEqual(self.get(self.admin, '99999999').status_code, 404)
        self.assertEqual(self.get(self.engineer, '70000111').status_code, 403)

    def test_active_task_means_pending_status(self):
        self.task(self.own, 'off')
        r = self.get(self.admin, '61002510')
        self.assertEqual(r.json()['status'], 'pending')

    def test_no_data_is_unknown(self):
        r = self.get(self.admin, '61002510')
        self.assertEqual((r.json()['status'], r.json()['provider']), ('unknown', 'Нет данных'))

    def test_falls_back_to_last_successful_command(self):
        RelayCommandHistory.objects.create(device=self.own, action='off', status='success', user=self.admin)
        r = self.get(self.admin, '61002510')
        self.assertEqual((r.json()['status'], r.json()['provider']), ('off', 'История'))

    def test_failed_commands_are_ignored_in_fallback(self):
        RelayCommandHistory.objects.create(device=self.own, action='off', status='pending')
        self.assertEqual(self.get(self.admin, '61002510').json()['status'], 'unknown')

    def test_sunrise_state_read_from_provider(self):
        Device.objects.filter(id=self.own.id).update(askue_id='26')
        with mock.patch('meters.views_relay.SunRiseService') as svc:
            svc.return_value.get_relay_status.return_value = True
            r = self.get(self.admin, '61002510')
        self.assertEqual((r.json()['status'], r.json()['provider']), ('on', 'SunRise'))

    def test_provider_error_falls_back_to_history(self):
        Device.objects.filter(id=self.own.id).update(askue_id='23')
        RelayCommandHistory.objects.create(device=self.own, action='on', status='success')
        with mock.patch('meters.views_relay.CEnergoService') as svc:
            svc.return_value.get_status.side_effect = Exception('cEnergo недоступна')
            r = self.get(self.admin, '61002510')
        self.assertEqual((r.json()['status'], r.json()['provider']), ('on', 'История'))

    def test_hexing_unknown_state_falls_back(self):
        Device.objects.filter(id=self.own.id).update(askue_id='25')
        with mock.patch('meters.views_relay.HexingService') as svc:
            svc.return_value.get_relay_status_from_db.return_value = 'unknown'
            r = self.get(self.admin, '61002510')
        self.assertEqual(r.json()['status'], 'unknown')


class MassCreateTests(RelayViewsBase):
    def post(self, user, **extra):
        data = {'action': 'off', 'scheduled_at': local_time(90), 'device_ids[]': [self.own.id]}
        data.update(extra)
        data = {k: v for k, v in data.items() if v is not None}
        return self.client_for(user).post(MASS_CREATE, data)

    def test_get_not_allowed(self):
        self.assertEqual(self.client_for(self.admin).get(MASS_CREATE).status_code, 405)

    def test_validation_errors(self):
        self.assertEqual(self.post(self.admin, action='x').status_code, 400)
        self.assertEqual(self.post(self.admin, scheduled_at=None).status_code, 400)
        self.assertEqual(self.post(self.admin, scheduled_at='не дата').status_code, 400)
        self.assertEqual(self.post(self.admin, scheduled_at=local_time(10)).status_code, 400)  # < +30 мин
        self.assertEqual(RelayTask.objects.count(), 0)

    def test_creates_task_with_debt_snapshot_and_user(self):
        Subscriber.objects.create(device=self.own, account_number='1', debt=777)
        r = self.post(self.admin)
        self.assertEqual(r.json()['count_tasks'], 1)
        task = RelayTask.objects.get()
        self.assertEqual((task.action, task.status, task.created_by), ('off', 'pending', self.admin))
        self.assertEqual(float(task.debt_snapshot), 777.0)

    def test_duplicate_active_task_is_skipped(self):
        self.task(self.own, 'off')
        r = self.post(self.admin)
        self.assertEqual((r.json()['count_tasks'], r.json()['skipped_duplicate']), (0, 1))

    def test_repeated_ids_create_one_task(self):
        r = self.post(self.admin, **{'device_ids[]': [self.own.id, self.own.id]})
        self.assertEqual(r.json()['count_tasks'], 1)

    def test_foreign_region_skipped_for_engineer_but_not_for_admin(self):
        ids = [self.own.id, self.foreign.id, self.no_region.id]
        r = self.post(self.engineer, **{'device_ids[]': ids})
        self.assertEqual((r.json()['count_tasks'], r.json()['skipped_region']), (1, 2))
        r = self.post(self.admin, **{'device_ids[]': [self.foreign.id]})
        self.assertEqual(r.json()['count_tasks'], 1)

    def test_unknown_ids_are_ignored(self):
        r = self.post(self.admin, **{'device_ids[]': [999999, 'abc']})
        self.assertEqual(r.json()['count_tasks'], 0)

    def test_opposite_pending_cancelled_but_in_flight_kept(self):
        waiting = self.task(self.own, 'on')
        other = Device.objects.create(serial_number='55555555', region=self.suzak)
        running = self.task(other, 'on', status='processing')
        self.post(self.admin, **{'device_ids[]': [self.own.id, other.id]})
        waiting.refresh_from_db()
        running.refresh_from_db()
        self.assertEqual(waiting.status, 'cancelled')
        self.assertEqual(running.status, 'processing')

    def test_orphans_create_issue_once(self):
        orphans = json.dumps([{'serial': '12345678', 'account': '7', 'name': 'Иванов', 'debt': 900}, {'serial': ''}])
        self.post(self.admin, orphan_devices=orphans, region_name='Сузак', **{'device_ids[]': []})
        r = self.post(self.admin, orphan_devices=orphans, region_name='Сузак', **{'device_ids[]': []})
        issues = CompareResult.objects.filter(serial_1c='12345678', action_type='not_found')
        self.assertEqual(issues.count(), 1)
        self.assertEqual(r.json()['count_issues'], 0)
        self.assertEqual(issues.get().status, 'new')


class MassSubscribersTests(RelayViewsBase):
    def get(self, user, region='03 Сузак'):
        return self.client_for(user).get(MASS_SUBS, {'region': region, 'min_debt': '500'})

    def test_split_found_and_not_found(self):
        debtors = [
            {'serial_number': '61002510', 'account_number': '1', 'full_name': 'А', 'debt': 900},
            {'serial_number': '0099999999', 'account_number': '2', 'full_name': 'Б', 'debt': 800},
        ]
        with mock.patch('meters.onec_service.OneCService.get_debtors', return_value=debtors) as gd:
            data = self.get(self.admin).json()
        gd.assert_called_once_with('03 Сузак', 500.0)
        self.assertEqual((data['total_found'], data['total_not_found']), (1, 1))
        self.assertEqual(data['found'][0]['id'], self.own.id)
        self.assertEqual(data['not_found'][0]['serial'], '0099999999')

    def test_local_fallback_key_is_understood(self):
        debtors = [{'device__serial_number': '61002510', 'account_number': '1', 'full_name': 'А', 'debt': 900}]
        with mock.patch('meters.onec_service.OneCService.get_debtors', return_value=debtors):
            self.assertEqual(self.get(self.admin).json()['total_found'], 1)

    def test_engineer_cannot_query_foreign_region(self):
        with mock.patch('meters.onec_service.OneCService.get_debtors') as gd:
            self.assertEqual(self.get(self.engineer, '01 Манас').status_code, 403)
        gd.assert_not_called()

    def test_1c_error_is_reported(self):
        with mock.patch('meters.onec_service.OneCService.get_debtors', side_effect=Exception('1С недоступна')):
            r = self.get(self.admin)
        self.assertEqual(r.status_code, 500)
        self.assertIn('1С недоступна', r.json()['error'])


class BulkStartTests(RelayViewsBase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.mkdtemp()
        patcher = mock.patch.object(views_relay, 'BULK_RESULT_PATH', os.path.join(tmp, 'result.json'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, user, serials, action='off'):
        return self.client_for(user).post(BULK_START, {'serials': serials, 'action': action})

    def result(self):
        with open(views_relay.BULK_RESULT_PATH, encoding='utf-8') as f:
            return json.load(f)

    def test_validation(self):
        self.assertEqual(self.start(self.admin, '', 'off').status_code, 400)
        self.assertEqual(self.start(self.admin, '61002510', 'x').status_code, 400)
        self.assertEqual(self.start(self.admin, '\n'.join(str(i) for i in range(2001))).status_code, 400)
        self.assertEqual(RelayTask.objects.count(), 0)

    def test_off_is_scheduled_30_minutes_ahead(self):
        before = timezone.now()
        self.assertEqual(self.start(self.admin, '61002510', 'off').status_code, 200)
        task = RelayTask.objects.get()
        self.assertGreaterEqual(task.scheduled_at, before + timedelta(minutes=30))
        self.assertEqual(self.result()['success'], 1)

    def test_on_is_immediate(self):
        before = timezone.now()
        self.start(self.admin, '61002510', 'on')
        self.assertLess((RelayTask.objects.get().scheduled_at - before).total_seconds(), 5)

    def test_errors_are_collected_and_duplicates_in_list_collapsed(self):
        self.task(self.own, 'off')
        text = '61002510\n61002510\n99999999\n70000111\n'
        r = self.start(self.engineer, text, 'off')
        errors = '\n'.join(self.result()['errors'])
        self.assertEqual(r.json()['errors_count'], 3)
        self.assertIn('уже есть активная задача', errors)
        self.assertIn('99999999', errors)
        self.assertIn('70000111: счётчик не из вашего района', errors)
        self.assertEqual(self.result()['total'], 3)
        self.assertEqual(RelayTask.objects.count(), 1)  # новых не создано

    def test_status_endpoint_returns_saved_result(self):
        self.start(self.admin, '61002510', 'on')
        data = self.client_for(self.admin).get(BULK_STATUS).json()
        self.assertEqual((data['status'], data['total'], data['success']), ('completed', 1, 1))

    def test_status_without_file(self):
        data = self.client_for(self.admin).get(BULK_STATUS).json()
        self.assertEqual(data['status'], 'starting')


class RetryTests(RelayViewsBase):
    def post(self, user, task_id):
        return self.client_for(user).post(RETRY, {'task_id': task_id})

    def failed(self):
        return RelayTask.objects.create(
            device=self.own, action='on', status='error', retry_count=3, error_message='сбой',
            hexing_payload='{"a": 1}', scheduled_at=timezone.now() - timedelta(hours=1),
        )

    def test_retry_resets_state(self):
        task = self.failed()
        before = timezone.now()
        self.assertEqual(self.post(self.admin, task.id).status_code, 200)
        task.refresh_from_db()
        self.assertEqual((task.status, task.retry_count), ('pending', 0))
        self.assertIsNone(task.hexing_payload)
        self.assertIsNone(task.error_message)
        self.assertGreaterEqual(task.scheduled_at, before)

    def test_only_error_tasks_can_be_retried(self):
        for status in ('pending', 'processing', 'success', 'cancelled'):
            task = RelayTask.objects.create(device=self.own, action='on', status=status, scheduled_at=timezone.now())
            self.assertEqual(self.post(self.admin, task.id).status_code, 400, status)

    def test_unknown_task(self):
        self.assertEqual(self.post(self.admin, 999999).status_code, 404)

    def test_only_staff(self):
        task = self.failed()
        for user in (self.operator, self.engineer):
            self.assertEqual(self.post(user, task.id).status_code, 302, user.username)
        task.refresh_from_db()
        self.assertEqual(task.status, 'error')

    def test_get_not_allowed(self):
        self.assertEqual(self.client_for(self.admin).get(RETRY).status_code, 405)


class MonitorTests(RelayViewsBase):
    def get(self, **params):
        return self.client_for(self.admin).get(MONITOR, params).json()

    def test_defaults_to_today(self):
        today = self.task(self.own, 'on')
        old = self.task(self.own, 'on', minutes=-3 * 24 * 60)
        ids = [i['id'] for i in self.get()['items']]
        self.assertIn(today.id, ids)
        self.assertNotIn(old.id, ids)

    def test_filters(self):
        Subscriber.objects.create(device=self.own, account_number='ACC-42', region='Сузак ЭС')
        a = self.task(self.own, 'on', status='success')
        b = self.task(self.foreign, 'off', status='error')
        day = timezone.now().strftime('%Y-%m-%d')
        base = {'date_from': day, 'date_to': day}
        ids = lambda **p: {i['id'] for i in self.get(**base, **p)['items']}
        self.assertEqual(ids(status='error'), {b.id})
        self.assertEqual(ids(action='on'), {a.id})
        self.assertEqual(ids(search='6100'), {a.id})
        self.assertEqual(ids(search='ACC-42'), {a.id})
        self.assertEqual(ids(region='03 Сузак'), {a.id})  # через район абонента и маппинг имён
        self.assertEqual(ids(), {a.id, b.id})

    def test_pagination_and_page_size_whitelist(self):
        for i in range(25):
            self.task(Device.objects.create(serial_number=f'9{i:07d}'), 'on')
        data = self.get(page_size=10)
        self.assertEqual((len(data['items']), data['pagination']['total_pages'], data['pagination']['total_items']), (10, 3, 25))
        self.assertEqual(len(self.get(page_size=7)['items']), 20)       # не из белого списка => 20
        self.assertEqual(len(self.get(page_size='abc')['items']), 20)
        self.assertEqual(self.get(page_size=10, page=99)['pagination']['current_page'], 3)  # за пределами => последняя
        self.assertEqual(self.get(page_size=10, page='x')['pagination']['current_page'], 1)

    def test_item_fields(self):
        Subscriber.objects.create(device=self.own, account_number='1', full_name='Иванов')
        RelayTask.objects.create(device=self.own, action='off', scheduled_at=timezone.now(),
                                 debt_snapshot=500, created_by=self.admin)
        item = self.get()['items'][0]
        self.assertEqual((item['serial'], item['name'], item['debt'], item['user']), ('61002510', 'Иванов', 500.0, 'admin'))
