"""Статистика дашборда: расчёт, кэш и прогрев."""
import io
from datetime import datetime
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from meters import dashboard_stats
from meters.models import Device, MeterModel, Reading


class StatsBase(TestCase):
    def setUp(self):
        cache.clear()
        model = MeterModel.objects.create(
            catalog_code='M1', model_name='M1', digit_capacity=6, phases='1', nominal_current='5',
            nominal_voltage='220', system_type='x', period='1', device_type_id='22', device_type_str='SX')
        self.active = Device.objects.create(serial_number='1', model=model, status='active')
        self.silent = Device.objects.create(serial_number='2', model=model, status='active')
        self.inactive = Device.objects.create(serial_number='3', model=model, status='inactive')
        now = timezone.now().replace(microsecond=0)
        for dev, note in ((self.active, 'Star sync'), (self.active, 'ручной ввод'), (self.inactive, 'ручной ввод')):
            Reading.objects.create(device=dev, timestamp=now, reading_value=Decimal('1'),
                                   direction='aplus' if note == 'Star sync' else 'aminus', notes=note)


class DashboardStatsTests(StatsBase):
    def test_counts(self):
        data = dashboard_stats.compute_period_stats('today')[0]
        self.assertEqual(data['total_devices'], 2)      # inactive не считается
        self.assertEqual(data['active_today'], 1)
        self.assertEqual(data['active_percent'], 50.0)
        self.assertEqual(data['manual_today'], 1)       # ручное только по активным, авто-сбор не в счёт

    def test_unknown_period_falls_back_to_today(self):
        self.assertEqual(dashboard_stats.compute_period_stats('bad')[0]['period'], 'today')

    def test_get_uses_cache(self):
        dashboard_stats.get_dashboard_stats('today')
        with mock.patch.object(dashboard_stats, 'compute_period_stats') as compute:
            dashboard_stats.get_dashboard_stats('today')
        compute.assert_not_called()

    def test_refresh_recomputes(self):
        dashboard_stats.get_dashboard_stats('today')
        with mock.patch.object(dashboard_stats, 'compute_period_stats', return_value=({'x': 1}, [])) as compute:
            self.assertEqual(dashboard_stats.get_dashboard_stats('today', refresh=True), {'x': 1})
        compute.assert_called_once()

    def test_warm_command_fills_cache_for_all_periods(self):
        call_command('warm_dashboard_cache', stdout=io.StringIO())
        self.assertIsNotNone(cache.get(dashboard_stats.cache_key('today')))
        self.assertIsNone(cache.get(dashboard_stats.cache_key('30d')))
        call_command('warm_dashboard_cache', '--all', stdout=io.StringIO())
        for period in dashboard_stats.PERIODS:
            self.assertIsNotNone(cache.get(dashboard_stats.cache_key(period)))
            self.assertIsNotNone(cache.get(dashboard_stats.region_cache_key(period)))

    def test_view_reads_cache_without_computing(self):
        from django.contrib.auth.models import User
        user = User.objects.create_superuser('admin', 'a@a.a', 'x')
        self.client.force_login(user)
        dashboard_stats.get_dashboard_stats('today')
        with mock.patch.object(dashboard_stats, 'compute_period_stats') as compute:
            resp = self.client.get('/dashboard/')
        self.assertEqual(resp.status_code, 200)
        compute.assert_not_called()


class RegionStatsTests(StatsBase):
    """Статистика по районам: те же данные, что и в setUp родителя."""

    def setUp(self):
        super().setUp()
        from meters.models import Region
        self.r1 = Region.objects.create(name='01 Манас')
        self.r2 = Region.objects.create(name='02 Кок-Арт')
        Device.objects.filter(pk__in=[self.active.pk, self.silent.pk]).update(region=self.r1)
        Device.objects.filter(pk=self.inactive.pk).update(region=self.r2)
        extra = Device.objects.create(serial_number='4', model=self.active.model, status='active', region=self.r2)
        Reading.objects.create(device=extra, timestamp=timezone.now().replace(microsecond=0),
                               reading_value=Decimal('1'), direction='aplus', notes='Star sync')

    def test_regions(self):
        stats = dashboard_stats.compute_period_stats('today')[1]
        self.assertEqual([s['region_name'] for s in stats], ['01 Манас', '02 Кок-Арт'])
        manas, kok = stats
        self.assertEqual((manas['total'], manas['active'], manas['offline'], manas['percent']), (2, 1, 1, 50.0))
        # inactive-устройство района не считается
        self.assertEqual((kok['total'], kok['active'], kok['offline'], kok['percent']), (1, 1, 0, 100.0))

    def test_device_without_region_goes_first_as_no_region(self):
        Device.objects.create(serial_number='5', model=self.active.model, status='active')
        stats = dashboard_stats.compute_period_stats('today')[1]
        self.assertEqual(stats[0]['region_name'], 'Без района')

    def test_view_returns_cards_from_cache(self):
        dashboard_stats.get_region_stats('today')
        with mock.patch.object(dashboard_stats, 'compute_period_stats') as compute:
            resp = self.client.get('/region-stats-table/?period=today')
        self.assertEqual(resp.status_code, 200)
        compute.assert_not_called()
        self.assertContains(resp, 'data-region-card', count=2)
        self.assertContains(resp, '01 Манас')


class ProducerStatsViewTests(StatsBase):
    def test_producers_from_cache_without_region(self):
        dashboard_stats.get_dashboard_stats('today')
        with mock.patch.object(dashboard_stats, 'compute_period_stats') as compute:
            resp = self.client.get('/producer-stats/?period=today')
        self.assertEqual(resp.status_code, 200)
        compute.assert_not_called()
        self.assertContains(resp, 'Sanxing')  # производитель типа SX
        self.assertContains(resp, '50,0%')    # 1 из 2 активных

    def test_producer_row_numbers(self):
        stats = dashboard_stats.compute_period_stats('today')[0]['producer_stats']
        self.assertEqual(len(stats), 1)
        self.assertEqual((stats[0]['total'], stats[0]['active'], stats[0]['percent']), (2, 1, 50.0))
