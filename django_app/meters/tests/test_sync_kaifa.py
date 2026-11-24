"""sync_kaifa: повторы и проверка кода в теле ответа, постраничная выгрузка, сопоставление приборов (без сети и без записи в MySQL)."""
from contextlib import contextmanager
from io import StringIO
from unittest import mock

from django.test import TestCase

from meters.management.commands import sync_kaifa
from meters.models import Device


class FakeResponse:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.text = body, status, str(body)

    def json(self):
        return self._body


def page(rows, total_page=1, total_row=None):
    return FakeResponse({'code': 200, 'data': {'records': {
        'totalPage': total_page, 'totalRow': total_row or len(rows), 'pageData': rows}}})


def row(no, ts='24/11/2026 00:00:00', val=100.5, missing=False):
    return {'deviceNo': no, 'freezeTime': ts,
            'itemValues': [{'magnitudeCode': 'PTCAP', 'rawValue': val, 'missing': missing}]}


class RequestTests(TestCase):
    def setUp(self):
        self.cmd = sync_kaifa.Command(stdout=StringIO(), stderr=StringIO())
        patcher = mock.patch.object(sync_kaifa.time, 'sleep')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_retries_on_network_error_then_succeeds(self):
        session = mock.Mock()
        session.request.side_effect = [Exception('handshake timed out'), page([])]
        body = self.cmd._request(session, 'POST', 'http://x')
        self.assertEqual(body['code'], 200)
        self.assertEqual(session.request.call_count, 2)

    def test_error_code_in_body_is_failure(self):
        # сервер отвечает HTTP 200 и code=500 в теле (так он ведёт себя на pageSize=5000)
        session = mock.Mock()
        session.request.return_value = FakeResponse({'code': 500, 'desc': 'Internal Server Error', 'data': None})
        with self.assertRaises(Exception) as ctx:
            self.cmd._request(session, 'POST', 'http://x')
        self.assertIn('500', str(ctx.exception))
        self.assertEqual(session.request.call_count, sync_kaifa.RETRIES)

    def test_timeouts_are_passed(self):
        session = mock.Mock()
        session.request.return_value = page([])
        self.cmd._request(session, 'POST', 'http://x')
        self.assertEqual(session.request.call_args.kwargs['timeout'], (sync_kaifa.CONNECT_TIMEOUT, sync_kaifa.READ_TIMEOUT))

    def test_page_size_is_safe(self):
        self.assertLessEqual(sync_kaifa.PAGE_SIZE, 1000)


class FetchTests(TestCase):
    def setUp(self):
        self.cmd = sync_kaifa.Command(stdout=StringIO(), stderr=StringIO())
        self.cmd.verbose = False
        self.dev = Device.objects.create(serial_number='089181050938', askue_id='30', api_id='KF', status='active')
        self.other_type = Device.objects.create(serial_number='555', askue_id='22', api_id='SX', status='active')
        self.inactive = Device.objects.create(serial_number='089181050939', askue_id='30', api_id='KF', status='inactive')
        patcher = mock.patch.object(sync_kaifa.time, 'sleep')
        patcher.start()
        self.addCleanup(patcher.stop)

        self.saved = []
        cursor = mock.MagicMock()
        cursor.executemany.side_effect = lambda sql, rows: self.saved.extend(rows)

        @contextmanager
        def cursor_cm():
            yield cursor
        conn = mock.Mock()
        conn.cursor = cursor_cm
        patcher = mock.patch.object(sync_kaifa, 'connection', conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_fetch(self, responses):
        session = mock.Mock()
        session.request.side_effect = responses
        return self.cmd._fetch_and_save(session, 'http://x', '0/1/', '23/11/2026 00:00:00', '24/11/2026 23:59:59')

    def test_saves_only_our_active_kaifa_devices(self):
        saved, errors = self.run_fetch([page([
            row('089181050938', val=2364.987),
            row('555'),                 # другой тип прибора: пропуск
            row('089181050939'),        # неактивный: пропуск
            row('999999'),              # нет в базе: пропуск
            row('089181050938', ts='битая дата'),
            row('089181050938', ts='23/11/2026 00:00:00', missing=True),
        ])])
        self.assertEqual((saved, errors), (1, 0))
        dev_id, dt, direction, val, notes = self.saved[0]
        self.assertEqual((dev_id, direction, notes), (self.dev.id, 'aplus', sync_kaifa.NOTES))
        self.assertEqual((dt.year, dt.month, dt.day, dt.hour), (2026, 11, 24, 0))
        self.assertEqual(val, 2364.987)

    def test_walks_all_pages(self):
        saved, errors = self.run_fetch([
            page([row('089181050938')], total_page=2),
            page([row('089181050938', ts='23/11/2026 00:00:00')], total_page=2),
        ])
        self.assertEqual((saved, errors), (2, 0))

    def test_failed_page_keeps_saved_and_reports_error(self):
        saved, errors = self.run_fetch(
            [page([row('089181050938')], total_page=2)]
            + [FakeResponse({'code': 500, 'desc': 'Internal Server Error', 'data': None})] * sync_kaifa.RETRIES)
        self.assertEqual((saved, errors), (1, 1))

    def test_empty_data_does_not_crash(self):
        saved, errors = self.run_fetch([FakeResponse({'code': 200, 'data': None})])
        self.assertEqual((saved, errors), (0, 0))
