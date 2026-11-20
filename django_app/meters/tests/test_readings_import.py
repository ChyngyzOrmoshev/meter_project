"""Импорт показаний: ручной ввод, загрузка Excel, фоновый импорт (run_import_background)."""
import io
from datetime import datetime
from unittest import mock

import openpyxl
from django.contrib.auth.models import User
from django.contrib.sessions.backends.db import SessionStore
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase

from meters.models import Device, Reading
from meters.views_readings import run_import_background

BULK = '/bulk-readings/'
LEGACY = '/import-readings-excel/'


def xlsx(rows, header=('Серийный', 'Дата', 'Значение', 'Заметка')):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(header))
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return SimpleUploadedFile('r.xlsx', buf.getvalue(),
                              content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


class ImportBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('admin', password='x', is_staff=True)
        self.client = Client()
        self.client.force_login(self.admin)
        self.device = Device.objects.create(serial_number='61002510')
        self.long = Device.objects.create(serial_number='086365005946')

    def reading(self, device=None, **filters):
        return Reading.objects.filter(device=device or self.device, **filters)


class ManualInputTests(ImportBase):
    def post(self, text, **extra):
        data = {'manual_submit': '1', 'date': '2026-11-01', 'readings_text': text}
        data.update(extra)
        return self.client.post(BULK, data)

    def test_creates_readings_at_midnight(self):
        self.post('61002510 1234.5\n086365005946 77')
        r = self.reading().get()
        self.assertEqual((float(r.reading_value), r.direction, r.timestamp), (1234.5, 'aplus', datetime(2026, 11, 1)))
        self.assertEqual(float(self.reading(self.long).get().reading_value), 77.0)

    def test_direction_and_notes(self):
        self.post('61002510 10', direction='aminus', notes='ручной ввод')
        r = self.reading().get()
        self.assertEqual((r.direction, r.notes), ('aminus', 'ручной ввод'))

    def test_repeat_updates_instead_of_duplicating(self):
        self.post('61002510 10')
        self.post('61002510 20')
        self.assertEqual(self.reading().count(), 1)
        self.assertEqual(float(self.reading().get().reading_value), 20.0)

    def test_bad_lines_do_not_stop_import(self):
        self.post('61002510\n99999999 5\n61002510 abc\n086365005946 9')
        self.assertEqual(self.reading().count(), 0)
        self.assertEqual(self.reading(self.long).count(), 1)

    def test_unique_suffix_finds_device(self):
        self.post('86365005946 15')
        self.assertEqual(self.reading(self.long).count(), 1)

    def test_requires_login_and_permission(self):
        self.assertEqual(Client().post(BULK, {'manual_submit': '1', 'readings_text': '61002510 1'}).status_code, 302)
        plain = User.objects.create_user('plain', password='x')
        c = Client()
        c.force_login(plain)
        c.post(BULK, {'manual_submit': '1', 'date': '2026-11-01', 'readings_text': '61002510 1'})
        self.assertEqual(Reading.objects.count(), 0)


class BackgroundImportTests(ImportBase):
    PARAMS = {'col_serial': 'sn', 'col_timestamp': 'ts', 'col_value': 'val', 'col_notes': 'note',
              'date_format': 'auto', 'col_direction': None, 'default_direction': 'aplus'}

    def run_import(self, rows, **params):
        session = SessionStore()
        session['import_data'] = rows
        session['import_params'] = {**self.PARAMS, **params}
        session.save()
        run_import_background(session.session_key)
        stored = SessionStore(session_key=session.session_key)
        return stored.get('import_result'), stored

    def row(self, sn='61002510', ts='2026-11-01', val=100, note='', **extra):
        return {'sn': sn, 'ts': ts, 'val': val, 'note': note, **extra}

    def test_creates_and_updates(self):
        result, stored = self.run_import([self.row(val=100)])
        self.assertEqual((result['success'], result['updated']), (1, 0))
        result, _ = self.run_import([self.row(val=150)])
        self.assertEqual((result['success'], result['updated']), (0, 1))
        self.assertEqual(self.reading().count(), 1)
        self.assertEqual(float(self.reading().get().reading_value), 150.0)

    def test_session_data_is_cleared_after_import(self):
        _, stored = self.run_import([self.row()])
        self.assertNotIn('import_data', stored)
        self.assertNotIn('import_params', stored)

    def test_direction_column_per_row(self):
        # регрессия: раньше row/direction использовались вне цикла (NameError), импорт не работал вовсе
        rows = [self.row(val=1, d='A+'), self.row(val=2, d='a-'), self.row(val=3, d='aminus'), self.row(val=4, d='мусор')]
        for i, r in enumerate(rows):
            r['ts'] = f'2026-11-0{i + 1}'
        result, _ = self.run_import(rows, col_direction='d')
        self.assertEqual(result['success'], 4)
        by_value = {float(r.reading_value): r.direction for r in self.reading()}
        self.assertEqual(by_value, {1.0: 'aplus', 2.0: 'aminus', 3.0: 'aminus', 4.0: 'aplus'})

    def test_default_direction_when_column_empty(self):
        self.run_import([self.row(d='')], col_direction='d', default_direction='aminus')
        self.assertEqual(self.reading().get().direction, 'aminus')

    def test_same_timestamp_different_direction_are_separate_readings(self):
        self.run_import([self.row(val=1, d='a+'), self.row(val=2, d='a-')], col_direction='d')
        self.assertEqual(self.reading().count(), 2)

    def test_date_formats_auto(self):
        formats = {
            '01.11.2026': datetime(2026, 11, 1),
            '01.11.2026 10:30:00': datetime(2026, 11, 1, 10, 30),
            '2026-11-02': datetime(2026, 11, 2),
            '2026-11-03 08:15:00': datetime(2026, 11, 3, 8, 15),
            '2026-11-04T09:00:00': datetime(2026, 11, 4, 9, 0),
            '2026-11-05T09:00:00Z': datetime(2026, 11, 5, 9, 0),
            '2026-11-06T09:00:00+06:00': datetime(2026, 11, 6, 9, 0),
            '11/07/2026': datetime(2026, 11, 7),
        }
        result, _ = self.run_import([self.row(ts=ts, val=i) for i, ts in enumerate(formats)])
        self.assertEqual((result['success'], result['error_count']), (len(formats), 0), result)
        self.assertEqual({r.timestamp for r in self.reading()}, set(formats.values()))

    def test_explicit_date_format(self):
        result, _ = self.run_import([self.row(ts='07-11-2026')], date_format='%d-%m-%Y')
        self.assertEqual(result['success'], 1)
        self.assertEqual(self.reading().get().timestamp, datetime(2026, 11, 7))

    def test_bad_date_is_reported_and_other_rows_still_saved(self):
        result, _ = self.run_import([self.row(ts='вчера'), self.row(ts='2026-11-01')])
        self.assertEqual((result['success'], result['error_count']), (1, 1))
        self.assertIn('вчера', result['errors'][0])

    def test_value_problems(self):
        rows = [self.row(val='abc'), self.row(val=''), self.row(val=None), self.row(val='12,5'), self.row(val=7)]
        for i, r in enumerate(rows):
            r['ts'] = f'2026-11-0{i + 1}'
        result, _ = self.run_import(rows)
        self.assertEqual(result['success'], 1)              # только val=7
        self.assertEqual(result['error_count'], 2)          # 'abc' и '12,5' (запятая не поддерживается)
        self.assertEqual(result['empty_count'], 2)          # '' и None

    def test_unknown_and_empty_serials(self):
        result, _ = self.run_import([self.row(sn='99999999'), self.row(sn=''), self.row(sn=None), self.row()])
        self.assertEqual((result['success'], result['skipped_count'], result['skipped']), (1, 1, ['99999999']))

    def test_serial_matched_by_unique_suffix(self):
        self.run_import([self.row(sn='86365005946')])
        self.assertEqual(self.reading(self.long).count(), 1)

    def test_notes_saved(self):
        self.run_import([self.row(note='  ручное  ')])
        self.assertEqual(self.reading().get().notes, 'ручное')

    def test_result_lists_are_truncated_to_ten_but_counted(self):
        rows = [self.row(sn=f'X{i}') for i in range(15)]
        result, _ = self.run_import(rows)
        self.assertEqual((len(result['skipped']), result['skipped_count']), (10, 15))

    def test_nothing_to_import_leaves_no_result(self):
        result, _ = self.run_import([])
        self.assertIsNone(result)

    def test_unknown_session_does_not_crash(self):
        run_import_background('несуществующая-сессия')


class ExcelUploadTests(ImportBase):
    def upload(self, rows, url=BULK, **extra):
        data = {'excel_file': xlsx(rows), 'excel_submit': '1'}
        data.update(extra)
        return self.client.post(url, data)

    def test_upload_with_text_dates_shows_preview(self):
        r = self.upload([('61002510', '2026-11-01', 100, 'x')])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['total_rows'], 1)
        self.assertEqual(r.context['columns'], ['Серийный', 'Дата', 'Значение', 'Заметка'])

    def test_upload_with_real_excel_dates_and_numbers(self):
        # даты в Excel приходят как datetime/Timestamp, серийники как числа: данные должны пережить сессию
        r = self.upload([(61002510, datetime(2026, 11, 1, 10, 0), 100.5, None)])
        self.assertEqual(r.status_code, 200, 'сохранение сессии с датой из Excel')

    def test_empty_file(self):
        r = self.upload([])
        self.assertEqual(r.status_code, 302)

    def test_not_an_excel_file(self):
        r = self.client.post(BULK, {'excel_file': SimpleUploadedFile('a.xlsx', b'not excel'), 'excel_submit': '1'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Reading.objects.count(), 0)


class ConfirmImportTests(ImportBase):
    """Полный путь: загрузка файла, подтверждение, фоновый импорт (поток выполняется синхронно)."""

    def setUp(self):
        super().setUp()

        class InlineThread:
            def __init__(self, target, args=(), **kwargs):
                self.target, self.args = target, args
                self.daemon = False

            def start(self):
                self.target(*self.args)

        patcher = mock.patch('meters.views_readings.threading.Thread', InlineThread)
        patcher.start()
        self.addCleanup(patcher.stop)

    def confirm(self, **extra):
        data = {'confirm_import': '1', 'col_serial': 'Серийный', 'col_timestamp': 'Дата',
                'col_value': 'Значение', 'col_notes': 'Заметка'}
        data.update(extra)
        return self.client.post(BULK, data)

    def test_upload_confirm_creates_readings(self):
        self.client.post(BULK, {'excel_file': xlsx([('61002510', '2026-11-01', 100, 'файл')]), 'excel_submit': '1'})
        r = self.confirm()
        self.assertEqual(r.status_code, 302)
        reading = Reading.objects.get()
        self.assertEqual((float(reading.reading_value), reading.notes), (100.0, 'файл'))
        self.assertEqual(self.client.session['import_result']['success'], 1)

    def test_numeric_serial_and_real_dates_full_cycle(self):
        self.client.post(BULK, {'excel_file': xlsx([('61002510', datetime(2026, 11, 1), 100, '')]), 'excel_submit': '1'})
        self.confirm()
        self.assertEqual(Reading.objects.count(), 1)

    def test_clear_result(self):
        self.client.post(BULK, {'excel_file': xlsx([('61002510', '2026-11-01', 100, '')]), 'excel_submit': '1'})
        self.confirm()
        self.client.get(BULK, {'clear_result': '1'})
        self.assertNotIn('import_result', self.client.session)


class LegacyImportViewTests(ImportBase):
    def upload(self, rows):
        return self.client.post(LEGACY, {'excel_file': xlsx(rows)})

    def confirm(self, **extra):
        data = {'confirm': '1', 'col_serial': 'Серийный', 'col_timestamp': 'Дата', 'col_value': 'Значение'}
        data.update(extra)
        return self.client.post(LEGACY, data)

    def test_full_cycle_creates_reading_as_aplus(self):
        # регрессия: раньше direction не определялся (NameError на любой валидной строке)
        self.upload([('61002510', '2026-11-01', 100, '')])
        r = self.confirm()
        self.assertEqual(r.status_code, 302)
        reading = Reading.objects.get()
        self.assertEqual((reading.direction, float(reading.reading_value)), ('aplus', 100.0))

    def test_confirm_requires_columns(self):
        self.upload([('61002510', '2026-11-01', 100, '')])
        self.confirm(col_value='')
        self.assertEqual(Reading.objects.count(), 0)

    def test_confirm_without_uploaded_data(self):
        self.confirm()
        self.assertEqual(Reading.objects.count(), 0)

    def test_errors_do_not_stop_valid_rows(self):
        self.upload([('', '2026-11-01', 1, ''), ('99999999', '2026-11-01', 2, ''),
                     ('61002510', 'вчера', 3, ''), ('61002510', '2026-11-01', 'abc', ''),
                     ('61002510', '2026-11-02', 4, '')])
        self.confirm()
        self.assertEqual([float(r.reading_value) for r in Reading.objects.all()], [4.0])
