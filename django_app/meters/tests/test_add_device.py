"""Регистрация приборов вручную: по одному, списком и из Excel — район по умолчанию ЖПЭС."""
import io

import openpyxl
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase

from meters.models import Device, MeterModel, Region

ADD = '/devices/add/'
UPLOAD = '/devices/upload/'


class AddDeviceRegionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(User.objects.create_user('adm', password='x', is_staff=True, is_superuser=True))
        self.model = MeterModel.objects.create(catalog_code='X1', model_name='SX-1', digit_capacity='6',
                                               phases=1, nominal_current='5(60)А')

    def assertJpes(self, serial):
        dev = Device.objects.get(serial_number=serial)
        self.assertIsNotNone(dev.region)
        self.assertEqual(dev.region.name, 'ЖПЭС')

    def test_single_gets_jpes(self):
        self.client.post(ADD, {'single_submit': '1', 'serial_number': '1001', 'model': self.model.id})
        self.assertJpes('1001')

    def test_group_gets_jpes(self):
        self.client.post(ADD, {'bulk_submit': '1', 'bulk_serials': '2001\n2002\n', 'bulk_model': self.model.id})
        self.assertJpes('2001')
        self.assertJpes('2002')

    def test_group_uses_existing_jpes_region(self):
        jpes = Region.objects.create(name='ЖПЭС')
        self.client.post(ADD, {'bulk_submit': '1', 'bulk_serials': '3001', 'bulk_model': self.model.id})
        self.assertEqual(Device.objects.get(serial_number='3001').region_id, jpes.id)
        self.assertEqual(Region.objects.filter(name='ЖПЭС').count(), 1)

    def test_excel_gets_jpes(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['Серийный', 'Модель', 'Статус', 'Ток', 'Фазы', 'ASKUE', 'API'])
        ws.append(['4001', 'SX-1', 'active', '5(60)А', 1, '22', 'SX'])
        buf = io.BytesIO()
        wb.save(buf)
        f = SimpleUploadedFile('d.xlsx', buf.getvalue())
        self.client.post(UPLOAD, {'excel_file': f})
        self.assertJpes('4001')
