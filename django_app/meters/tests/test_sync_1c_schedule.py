"""Сверка с 1С по расписанию: проверка папки за сегодня (12 файлов, маппинг районов), единые названия районов, SyncStatus."""
import json
import os
import tempfile
import time
from io import StringIO
from unittest import mock

import openpyxl
from django.core.management import call_command
from django.test import TestCase

from meters.management.commands import sync_1c_statuses as cmd
from datetime import datetime, timedelta

from meters.models import Device, Reading, Region, SyncStatus
from meters.region_mapping import (REGION_MAPPING, canonical_1c_name, check_folder, display_name,
                                   expected_1c_files, today_folder)

DB_NAMES = list(REGION_MAPPING.values())


def make_xlsx(path, serials=()):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['Заводской номер', 'Счетчик', 'Категория'])
    for sn in serials:
        ws.append([sn, 'ST34', 'Быт'])
    wb.save(path)


class FolderBase(TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.export = tempfile.mkdtemp()
        self.folder = today_folder()
        os.makedirs(os.path.join(self.base, self.folder))

    def put(self, name, serials=(), age=3600, folder=None):
        path = os.path.join(self.base, folder or self.folder, name + '.xlsx')
        make_xlsx(path, serials)
        old = time.time() - age
        os.utime(path, (old, old))
        return path

    def put_all(self, skip=(), latin_c=True):
        for name in expected_1c_files():
            if name in skip:
                continue
            self.put('Сузак ЭC' if latin_c and name == 'Сузак ЭС' else name)

    def check(self, **kw):
        return check_folder(self.base, self.folder, DB_NAMES, **kw)


class NameTests(TestCase):
    def test_latin_double_is_the_same_district(self):
        self.assertEqual(canonical_1c_name('Сузак ЭC'), 'Сузак ЭС')
        self.assertEqual(display_name('Сузак ЭC'), '03 Сузак')
        self.assertEqual(display_name('03 Сузак'), '03 Сузак')
        self.assertEqual(display_name('сузак  эс'), '03 Сузак')

    def test_unknown_and_empty(self):
        self.assertEqual(display_name('Новый район'), 'Новый район')
        self.assertEqual(display_name(''), 'Без района')

    def test_twelve_files_expected(self):
        self.assertEqual(len(expected_1c_files()), 12)
        self.assertNotIn('ЖПЭС', expected_1c_files())


class CheckFolderTests(FolderBase):
    def test_complete_folder_is_ok(self):
        self.put_all()
        self.assertEqual(self.check(), (True, []))

    def test_missing_folder(self):
        ok, problems = check_folder(self.base, '2000-01-01', DB_NAMES)
        self.assertFalse(ok)
        self.assertIn('нет в 1c_reports', problems[0])

    def test_missing_file(self):
        self.put_all(skip=('Чаткал',))
        ok, problems = self.check()
        self.assertFalse(ok)
        self.assertIn('Чаткал', ' '.join(problems))

    def test_file_not_in_mapping(self):
        self.put_all()
        self.put('Новый район')
        ok, problems = self.check()
        self.assertFalse(ok)
        self.assertIn('Новый район', ' '.join(problems))

    def test_duplicate_district_files(self):
        self.put_all()
        self.put('Сузак ЭС')   # кириллица рядом с файлом с латинской C
        ok, problems = self.check()
        self.assertFalse(ok)
        self.assertIn('два файла', ' '.join(problems))

    def test_empty_and_open_files(self):
        self.put_all()
        open(os.path.join(self.base, self.folder, 'Манас РЭС.xlsx'), 'w').close()
        open(os.path.join(self.base, self.folder, '~$Ноокен.xlsx'), 'w').close()
        ok, problems = self.check()
        text = ' '.join(problems)
        self.assertFalse(ok)
        self.assertIn('пустой файл', text)
        self.assertIn('открыт в Excel', text)

    def test_file_still_being_written(self):
        self.put_all()
        self.put('Токтогул', age=10)
        ok, problems = self.check(settle_sec=300)
        self.assertFalse(ok)
        self.assertIn('ещё записывается', ' '.join(problems))
        self.assertTrue(self.check(settle_sec=0)[0])

    def test_mapped_region_missing_in_db(self):
        self.put_all()
        names = [n for n in DB_NAMES if n != '12 Токтогул']
        ok, problems = check_folder(self.base, self.folder, names)
        self.assertFalse(ok)
        self.assertIn('12 Токтогул', ' '.join(problems))


class ScheduledRunTests(FolderBase):
    def setUp(self):
        super().setUp()
        for name in DB_NAMES:
            Region.objects.create(name=name)
        self.dev = Device.objects.create(serial_number='61002510', status='active',
                                         region=Region.objects.get(name='05 Ноокен'))
        for patcher in (mock.patch.object(cmd, 'BASE_1C_PATH', self.base), mock.patch.object(cmd, 'EXPORT_DIR', self.export)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cmd(self, *args):
        out = StringIO()
        call_command('sync_1c_statuses', *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def result(self):
        with open(os.path.join(self.export, 'sync_1c_result.json'), encoding='utf-8') as f:
            return json.load(f)

    def test_incomplete_folder_is_not_run(self):
        self.put_all(skip=('Чаткал', 'Токтогул'))
        out = self.run_cmd('--scheduled')
        self.assertIn('не запущена', out)
        self.assertFalse(os.path.exists(os.path.join(self.export, 'sync_1c_result.json')))
        st = SyncStatus.objects.get(robot_name='Sync1C')
        self.assertEqual(st.status, 'waiting')
        self.assertIn('Чаткал', st.error)

    def test_waiting_keeps_previous_apply_numbers(self):
        old = SyncStatus.objects.create(robot_name='Sync1C', status='success', last_update='2026-10-22 10:00:00',
                                        records_processed=3556)
        self.put_all(skip=('Чаткал',))
        self.run_cmd('--scheduled')
        st = SyncStatus.objects.get(pk=old.pk)
        self.assertEqual((st.status, st.records_processed), ('waiting', 3556))
        self.assertEqual(str(st.last_update)[:10], '2026-10-22')

    def test_complete_folder_runs_once_and_districts_are_unique(self):
        self.put_all()
        self.put('Ноокен', serials=['61002510', '99999999'])   # перезаписываем: один свой прибор и один новый
        self.run_cmd('--scheduled')
        res = self.result()
        self.assertTrue(res['applied'])
        self.assertEqual(res['folder_date'], self.folder)
        self.assertEqual(res['new_count'], 1)
        names = list(res['districts'])
        self.assertEqual(len(names), len(set(names)))
        self.assertIn('03 Сузак', names)
        self.assertNotIn('Сузак ЭC', names)
        st = SyncStatus.objects.get(robot_name='Sync1C')
        self.assertEqual(st.status, 'success')
        self.assertIsNone(st.error)
        # второй опрос в тот же день ничего не делает
        mtime = os.path.getmtime(os.path.join(self.export, 'sync_1c_result.json'))
        out = self.run_cmd('--scheduled')
        self.assertIn('уже применена', out)
        self.assertEqual(os.path.getmtime(os.path.join(self.export, 'sync_1c_result.json')), mtime)

    def stale_device(self):
        """Прибор, которого нет в 1С и по которому нет показаний больше 30 дней: кандидат на отключение."""
        dev = Device.objects.create(serial_number='70000001', status='active', region=Region.objects.get(name='06 Майлуу-Суу'))
        Reading.objects.create(device=dev, timestamp=datetime.now() - timedelta(days=90), reading_value=1)
        return dev

    def test_scheduled_run_applies_deactivation_within_limit(self):
        self.put_all()
        dev = self.stale_device()
        self.run_cmd('--scheduled')
        dev.refresh_from_db()
        self.assertEqual(dev.status, 'inactive')
        # и ещё прибор из setUp: его нет в файлах и показаний у него нет совсем
        self.assertEqual(self.result()['deactivated_count'], 2)

    def test_scheduled_run_blocked_when_too_many_deactivations(self):
        self.put_all()
        dev = self.stale_device()
        with mock.patch.object(cmd, 'MAX_AUTO_DEACTIVATE', 0):
            out = self.run_cmd('--scheduled')
            self.assertIn('примените вручную', out)
            res = self.result()
            self.assertTrue(res['blocked'])
            self.assertFalse(res['applied'])
            dev.refresh_from_db()
            self.assertEqual(dev.status, 'active')
            st = SyncStatus.objects.get(robot_name='Sync1C')
            self.assertEqual(st.status, 'waiting')
            self.assertIn('вручную', st.error)
            # повторный опрос в тот же день не пересчитывает заново
            mtime = os.path.getmtime(os.path.join(self.export, 'sync_1c_result.json'))
            self.assertIn('уже применена', self.run_cmd('--scheduled'))
            self.assertEqual(os.path.getmtime(os.path.join(self.export, 'sync_1c_result.json')), mtime)

    def test_manual_apply_after_block_marks_day_as_done(self):
        self.put_all()
        dev = self.stale_device()
        with mock.patch.object(cmd, 'MAX_AUTO_DEACTIVATE', 0):
            self.run_cmd('--scheduled')
        self.run_cmd('--apply')   # человек применил вручную
        dev.refresh_from_db()
        self.assertEqual(dev.status, 'inactive')
        self.assertEqual(SyncStatus.objects.get(robot_name='Sync1C').status, 'success')

    def test_unreadable_file_blocks_apply(self):
        self.put_all()
        with open(os.path.join(self.base, self.folder, 'Манас РЭС.xlsx'), 'wb') as f:
            f.write(b'not an excel file')
        old = time.time() - 3600
        os.utime(os.path.join(self.base, self.folder, 'Манас РЭС.xlsx'), (old, old))
        self.run_cmd('--scheduled')
        self.assertFalse(os.path.exists(os.path.join(self.export, 'sync_1c_result.json')))
        self.assertEqual(SyncStatus.objects.get(robot_name='Sync1C').status, 'waiting')

    def test_manual_run_uses_latest_complete_folder(self):
        yesterday = '2000-01-01'
        os.makedirs(os.path.join(self.base, yesterday))
        for name in expected_1c_files():
            self.put(name, folder=yesterday)
        self.put('Манас РЭС')   # сегодняшняя папка неполная
        self.run_cmd()
        self.assertEqual(self.result()['folder_date'], yesterday)
        self.assertFalse(self.result()['applied'])
        self.assertFalse(SyncStatus.objects.filter(robot_name='Sync1C').exists())   # без применения статус не трогаем
