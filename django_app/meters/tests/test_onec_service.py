import base64
import os
from unittest import mock

import requests
from django.test import TestCase

from meters.models import Device, Region, Subscriber
from meters.onec_service import OneCService


def response(payload):
    r = mock.Mock()
    r.raise_for_status = lambda: None
    r.json = lambda: payload
    return r


ENV = {
    'ONEC_SERVER_URL': 'http://1c.test/',
    'ONEC_USER': 'АСКУЭ',
    'ONEC_PASSWORD': 'пароль',
    'ONEC_DB_SUZAK': 'spree',
    'ONEC_DB_OVERRIDE': '',
}


class OneCServiceTests(TestCase):
    def service(self, **env):
        with mock.patch.dict(os.environ, {**ENV, **env}):
            svc = OneCService()
        svc.auth = (env.get('ONEC_USER', ENV['ONEC_USER']), env.get('ONEC_PASSWORD', ENV['ONEC_PASSWORD']))
        return svc

    # --- get_debtors: запрос ---

    def test_request_url_body_and_utf8_basic_auth(self):
        svc = self.service()
        ok = {'status': 'ok', 'data': [{'serial_number': '1', 'debt': 10}]}
        with mock.patch('requests.post', return_value=response(ok)) as post:
            rows = svc.get_debtors('03 Сузак', 500)
        self.assertEqual(rows, ok['data'])
        self.assertEqual(post.call_args.args[0], 'http://1c.test/spree/hs/askue/readdebt')
        body = post.call_args.kwargs['json']
        self.assertEqual(body['min_debt'], 500.0)
        self.assertTrue(all(isinstance(t, str) for t in body['askue_types']))
        token = post.call_args.kwargs['headers']['Authorization'].split()[1]
        self.assertEqual(base64.b64decode(token).decode('utf-8'), 'АСКУЭ:пароль')

    def test_status_not_means_empty_list(self):
        svc = self.service()
        with mock.patch('requests.post', return_value=response({'status': 'Not', 'Message': 'нет'})):
            self.assertEqual(svc.get_debtors('03 Сузак', 20000), [])

    def test_unknown_status_raises_with_message(self):
        svc = self.service()
        with mock.patch('requests.post', return_value=response({'status': 'err', 'Message': 'бум'})):
            with self.assertRaisesRegex(Exception, "'err'.*бум"):
                svc.get_debtors('03 Сузак', 1)

    def test_missing_credentials_raise(self):
        svc = self.service()
        svc.auth = ('', '')
        with mock.patch('requests.post') as post:
            with self.assertRaisesRegex(Exception, 'ONEC_USER'):
                svc.get_debtors('03 Сузак', 1)
        post.assert_not_called()

    def test_network_error_wrapped(self):
        svc = self.service()
        with mock.patch('requests.post', side_effect=requests.ConnectionError('нет связи')):
            with self.assertRaisesRegex(Exception, 'Ошибка связи с 1С'):
                svc.get_debtors('03 Сузак', 1)

    def test_db_override_replaces_region_db(self):
        svc = self.service(ONEC_DB_OVERRIDE='proba83')
        with mock.patch('requests.post', return_value=response({'status': 'Not'})) as post:
            svc.get_debtors('03 Сузак', 1)
        self.assertEqual(post.call_args.args[0], 'http://1c.test/proba83/hs/askue/readdebt')

    # --- локальный режим (район без базы 1С) ---

    def test_region_without_db_uses_local_subscribers_and_no_http(self):
        region = Region.objects.create(name='ЖПЭС')
        dev = Device.objects.create(serial_number='555', region=region)
        Subscriber.objects.create(device=dev, account_number='1', full_name='Иванов', debt=700, region='ЖПЭС')
        svc = self.service()
        with mock.patch('requests.post') as post:
            rows = svc.get_debtors('ЖПЭС', 500)
            debts = svc.check_debts('ЖПЭС', [{'serial_number': '555'}, {'serial_number': '404'}])
        post.assert_not_called()
        self.assertEqual([r['device__serial_number'] for r in rows], ['555'])
        self.assertEqual(debts, {'555': 700.0, '404': 0.0})

    def test_no_server_url_uses_local_mode(self):
        svc = self.service(ONEC_SERVER_URL='')
        with mock.patch('requests.post') as post:
            self.assertEqual(svc.get_debtors('03 Сузак', 1), [])
        post.assert_not_called()

    # --- check_debts через checkdebt ---

    def test_check_debts_sends_checkdebt_request(self):
        svc = self.service()
        ok = {'status': 'ok', 'data': [{'serial_number': '31000374', 'askue_type_id': '22', 'debt': 18911.4}]}
        meters = [{'serial_number': '31000374', 'askue_type_id': '22', 'askue_type_str': 'SX'}]
        with mock.patch('requests.post', return_value=response(ok)) as post:
            res = svc.check_debts('03 Сузак', meters)
        self.assertEqual(res, {'31000374': 18911.4})
        self.assertEqual(post.call_args.args[0], 'http://1c.test/spree/hs/askue/checkdebt')
        self.assertEqual(post.call_args.kwargs['json'], {'meters': meters})

    def test_check_debts_matches_serial_ignoring_leading_zeros(self):
        svc = self.service()
        ok = {'status': 'ok', 'data': [{'serial_number': '086365005946', 'debt': 1418.07}]}
        with mock.patch('requests.post', return_value=response(ok)):
            res = svc.check_debts('03 Сузак', [{'serial_number': '86365005946'}, {'serial_number': '999'}])
        self.assertEqual(res, {'86365005946': 1418.07, '999': 0.0})

    def test_check_debts_status_not_means_zero_debt(self):
        svc = self.service()
        with mock.patch('requests.post', return_value=response({'status': 'Not'})):
            self.assertEqual(svc.check_debts('03 Сузак', [{'serial_number': '1'}]), {'1': 0.0})

    def test_check_debts_unknown_status_raises(self):
        svc = self.service()
        with mock.patch('requests.post', return_value=response({'status': 'Error', 'Message': 'сбой'})):
            with self.assertRaisesRegex(Exception, 'Ошибка проверки долга в 1С'):
                svc.check_debts('03 Сузак', [{'serial_number': '1'}])

    def test_check_debts_1c_down_raises_so_task_is_retried(self):
        svc = self.service()
        with mock.patch('requests.post', side_effect=requests.Timeout('таймаут')):
            with self.assertRaisesRegex(Exception, 'Ошибка проверки долга в 1С'):
                svc.check_debts('03 Сузак', [{'serial_number': '1'}])
