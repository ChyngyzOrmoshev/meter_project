"""Внешний API v1: токен, приборы, показания (только чтение)."""
from datetime import datetime, timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import Client, TestCase

from meters.api_v1 import create_token
from meters.models import ApiToken, Device, MeterModel, Reading, Region

DEV = '/api/v1/devices/'
READ = '/api/v1/readings/'
LATEST = '/api/v1/readings/latest/'


class ApiBase(TestCase):
    def setUp(self):
        self.token, self.key = create_token('Тест')
        self.client = Client(HTTP_AUTHORIZATION=f'Bearer {self.key}')
        region = Region.objects.create(name='ЖПЭС')
        model = MeterModel.objects.create(catalog_code='X1', model_name='SX-1', digit_capacity='6', phases=1,
                                          nominal_current='5(60)А')
        self.dev = Device.objects.create(serial_number='1001', model=model, region=region, status='active')
        self.other = Device.objects.create(serial_number='1002', status='repair')
        self.t0 = datetime(2026, 11, 20, 12, 0, 0)
        for i in range(3):
            Reading.objects.create(device=self.dev, timestamp=self.t0 + timedelta(hours=i),
                                   reading_value=Decimal('100.5') + i, direction='aplus')
        Reading.objects.create(device=self.dev, timestamp=self.t0, reading_value=Decimal('7'), direction='aminus')

    def get(self, url, **params):
        return self.client.get(url, params)


class AuthTests(ApiBase):
    def test_requires_token(self):
        for url in (DEV, READ, LATEST, DEV + '1001/'):
            self.assertEqual(Client().get(url).status_code, 401)

    def test_wrong_and_revoked_token(self):
        self.assertEqual(Client(HTTP_AUTHORIZATION='Bearer nope').get(DEV).status_code, 401)
        self.assertEqual(Client(HTTP_AUTHORIZATION=self.key).get(DEV).status_code, 401)
        ApiToken.objects.filter(id=self.token.id).update(is_active=False)
        self.assertEqual(self.get(DEV).status_code, 401)

    def test_key_not_stored_and_last_used_set(self):
        self.assertNotIn(self.key, [self.token.key_hash, self.token.prefix])
        self.assertEqual(self.get(DEV).status_code, 200)
        self.token.refresh_from_db()
        self.assertIsNotNone(self.token.last_used_at)

    def test_read_only(self):
        self.assertEqual(self.client.post(DEV, {}).status_code, 405)
        self.assertEqual(self.client.delete(DEV + '1001/').status_code, 405)


class DevicesTests(ApiBase):
    def test_list_and_filters(self):
        data = self.get(DEV).json()
        self.assertEqual({i['serial_number'] for i in data['items']}, {'1001', '1002'})
        self.assertFalse(data['has_next'])
        self.assertEqual([i['serial_number'] for i in self.get(DEV, region='ЖПЭС').json()['items']], ['1001'])
        self.assertEqual([i['serial_number'] for i in self.get(DEV, status='repair').json()['items']], ['1002'])

    def test_pagination(self):
        data = self.get(DEV, page_size=1).json()
        self.assertEqual(len(data['items']), 1)
        self.assertTrue(data['has_next'])
        self.assertFalse(self.get(DEV, page_size=1, page=2).json()['has_next'])

    def test_detail(self):
        data = self.get(DEV + '1001/').json()
        self.assertEqual((data['serial_number'], data['model'], data['region']), ('1001', 'SX-1', 'ЖПЭС'))
        self.assertEqual(self.get(DEV + '9999/').status_code, 404)

    def test_bad_page(self):
        self.assertEqual(self.get(DEV, page='x').status_code, 400)


class ReadingsTests(ApiBase):
    def test_requires_serial(self):
        self.assertEqual(self.get(READ).status_code, 400)

    def test_period_and_values_as_strings(self):
        data = self.get(READ, serial='1001', date_from='2026-11-20', date_to='2026-11-20', direction='aplus').json()
        self.assertEqual([i['value'] for i in data['items']], ['100.50000000', '101.50000000', '102.50000000'])
        self.assertEqual(data['items'][0]['timestamp'], '2026-11-20T12:00:00')
        self.assertEqual(data['items'][0]['direction'], 'aplus')

    def test_date_to_inclusive_end_of_day_and_exclusion(self):
        self.assertEqual(len(self.get(READ, serial='1001', date_from='2026-11-21', date_to='2026-11-21').json()['items']), 0)
        data = self.get(READ, serial='1001', date_from='2026-11-20 13:00:00', date_to='2026-11-20 13:00:00').json()
        self.assertEqual([i['value'] for i in data['items']], ['101.50000000'])

    def test_both_directions_and_not_found(self):
        data = self.get(READ, serial='1001,0000', date_from='2026-11-20', date_to='2026-11-20').json()
        self.assertEqual(len(data['items']), 4)
        self.assertEqual(data['not_found'], ['0000'])

    def test_pagination(self):
        data = self.get(READ, serial='1001', date_from='2026-11-20', date_to='2026-11-20', page_size=3).json()
        self.assertEqual(len(data['items']), 3)
        self.assertTrue(data['has_next'])

    def test_limits_and_validation(self):
        base = dict(serial='1001')
        self.assertEqual(self.get(READ, **base, date_from='2026-01-01', date_to='2026-11-20').status_code, 400)
        self.assertEqual(self.get(READ, **base, date_from='2026-11-21', date_to='2026-11-20').status_code, 400)
        self.assertEqual(self.get(READ, **base, date_from='вчера').status_code, 400)
        self.assertEqual(self.get(READ, **base, direction='x').status_code, 400)
        self.assertEqual(self.get(READ, serial=','.join(str(i) for i in range(101))).status_code, 400)

    def test_latest(self):
        data = self.get(LATEST, serial='1001,1002,0000').json()
        by_dir = {i['direction']: i['value'] for i in data['items']}
        self.assertEqual(by_dir, {'aplus': '102.50000000', 'aminus': '7.00000000'})
        self.assertEqual(data['not_found'], ['0000'])


class TokenCommandTests(TestCase):
    def run_cmd(self, *args):
        out = StringIO()
        call_command('api_token', *args, stdout=out)
        return out.getvalue()

    def test_create_use_and_revoke(self):
        key = self.run_cmd('create', 'Партнёр').strip().splitlines()[-1]
        self.assertEqual(Client(HTTP_AUTHORIZATION=f'Bearer {key}').get(DEV).status_code, 200)
        self.assertIn('Партнёр', self.run_cmd('list'))
        token = ApiToken.objects.get()
        self.run_cmd('revoke', str(token.id))
        self.assertEqual(Client(HTTP_AUTHORIZATION=f'Bearer {key}').get(DEV).status_code, 401)
