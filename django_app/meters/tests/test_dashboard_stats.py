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
        with mock.patch.object(dashboard_stats, 'compute_period_stats', return_value=({'x': 1}, [], [])) as compute:
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


class CardLinksTests(StatsBase):
    def test_region_links_carry_region_status_and_period(self):
        from meters.models import Region
        r = Region.objects.create(name='01 Манас')
        Device.objects.filter(pk=self.active.pk).update(region=r)
        resp = self.client.get('/region-stats-table/?period=7d')
        self.assertContains(resp, f'/devices/?region={r.pk}&status=active')
        self.assertContains(resp, f'/readings-with/?region={r.pk}&status=active&period=7d')
        self.assertContains(resp, f'/missing-readings/?region={r.pk}&status=active&period=7d')

    def test_producer_links_use_producer_name(self):
        resp = self.client.get('/producer-stats/?period=3d')
        self.assertContains(resp, 'producer_name=Sanxing_old&status=active&period=3d')
        self.assertContains(resp, 'data-producer-card', count=1)

    def test_producer_model_ids_groups_by_display_name(self):
        from meters.views import producer_model_ids
        self.assertEqual(producer_model_ids('Sanxing_old'), [self.active.model_id])
        self.assertEqual(producer_model_ids('Нет такого'), [])

    def test_reports_filter_by_producer_name(self):
        from django.contrib.auth.models import User
        self.client.force_login(User.objects.create_superuser('adm', 'a@a.a', 'x'))
        other = MeterModel.objects.create(
            catalog_code='M2', model_name='M2', digit_capacity=6, phases='1', nominal_current='5',
            nominal_voltage='220', system_type='x', period='1', device_type_id='23', device_type_str='EM')
        em = Device.objects.create(serial_number='ENERGO77', model=other, status='active')
        sx = Device.objects.create(serial_number='SANXFOREIGN', model=self.active.model, status='active')
        Reading.objects.create(device=em, timestamp=timezone.now().replace(microsecond=0),
                               reading_value=Decimal('1'), direction='aplus')
        # missing-readings: ПУ без показаний, поэтому ENERGO77 (с показанием) там не нужен
        cases = {'/devices/': (['ENERGO77'], ['SANXFOREIGN']),
                 '/readings-with/': (['ENERGO77'], ['SANXFOREIGN'])}
        for url, (present, absent) in cases.items():
            body = self.client.get(url + '?producer_name=Energomera&status=active&period=30d').content.decode()
            for serial in present:
                self.assertIn(serial, body, url)
            for serial in absent:
                self.assertNotIn(serial, body, url)
        # missing-readings: SX без показаний виден по своему производителю и не виден по чужому
        body = self.client.get('/missing-readings/?producer_name=Sanxing_old&status=active&period=30d').content.decode()
        self.assertIn('SANXFOREIGN', body)
        body = self.client.get('/missing-readings/?producer_name=Energomera&status=active&period=30d').content.decode()
        self.assertNotIn('SANXFOREIGN', body)

    def test_region_without_id_has_no_report_links(self):
        resp = self.client.get('/region-stats-table/?period=today')
        self.assertNotContains(resp, 'region=None')


class ReportsViaCacheTests(StatsBase):
    """Отчёты «с показаниями / без показаний» и «приборы по типам» берут период из кэша прогрева."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import User
        self.client.force_login(User.objects.create_superuser('adm2', 'a@a.a', 'x'))

    def test_no_reading_ids_exclude_devices_with_readings(self):
        ids = dashboard_stats.get_no_reading_ids('today')
        self.assertIn(self.silent.pk, ids)
        self.assertNotIn(self.active.pk, ids)

    def test_no_reading_cache_expires_on_new_day(self):
        dashboard_stats.get_no_reading_ids('today')
        payload = cache.get(dashboard_stats.no_reading_cache_key('today'))
        from datetime import timedelta
        payload['date'] = timezone.now().date() - timedelta(days=1)
        cache.set(dashboard_stats.no_reading_cache_key('today'), payload, 60)
        # новый прибор без показаний должен попасть в список после пересчёта
        new = Device.objects.create(serial_number='NEWDEV', model=self.active.model, status='active')
        self.assertIn(new.pk, dashboard_stats.get_no_reading_ids('today'))

    def test_readings_with_shows_only_devices_with_readings(self):
        body = self.client.get('/readings-with/?period=today&status=active').content.decode()
        self.assertIn(self.active.serial_number, body)
        self.assertNotIn('>2</strong>', body)

    def test_missing_readings_shows_only_devices_without_readings(self):
        resp = self.client.get('/missing-readings/?period=today&status=active')
        serials = [d.serial_number for d in resp.context['devices']]
        self.assertEqual(serials, ['2'])
        self.assertTrue(all(hasattr(d, 'last_reading_date') and hasattr(d, 'has_recent_reading') for d in resp.context['devices']))

    def test_readings_with_context_has_flags_and_window_range(self):
        resp = self.client.get('/readings-with/?period=today&status=active')
        devices = resp.context['devices']
        self.assertEqual([d.serial_number for d in devices], ['1'])
        self.assertTrue(devices[0].has_recent_reading)
        self.assertIsNotNone(devices[0].last_reading_date)

    def test_online_filter_still_works(self):
        resp = self.client.get('/missing-readings/?period=today&status=active&online=no')
        self.assertEqual([d.serial_number for d in resp.context['devices']], ['2'])
        resp = self.client.get('/missing-readings/?period=today&status=active&online=yes')
        self.assertEqual(list(resp.context['devices']), [])

    def test_devices_page_has_recent_flag_per_page(self):
        resp = self.client.get('/devices/')
        flags = {d.serial_number: d.has_recent_reading for d in resp.context['devices']}
        self.assertEqual(flags, {'1': True, '2': False, '3': True})

    def test_device_types_report_counts_online(self):
        resp = self.client.get('/reports/device-types/?period=today')
        self.assertEqual(resp.status_code, 200)

    def test_window_page_range(self):
        from meters.report_helpers import window_page_range
        self.assertEqual(window_page_range(1, 1), [1])
        self.assertEqual(window_page_range(1, 5), [1, 2, 3, 4, 5])
        self.assertEqual(window_page_range(500, 1000), [1, 497, 498, 499, 500, 501, 502, 503, 1000])
        self.assertEqual(window_page_range(1000, 1000), [1, 997, 998, 999, 1000])


class CategoryCardsTests(StatsBase):
    """Карточки категорий: проценты, «без категории» = «Прочие», линия за 7 дней."""

    def setUp(self):
        super().setUp()
        Device.objects.filter(pk=self.active.pk).update(category='population')
        Device.objects.filter(pk=self.silent.pk).update(category=None)

    def test_category_stats_cover_all_six_categories_in_order(self):
        stats = dashboard_stats.compute_period_stats('today')[0]['category_stats']
        self.assertEqual([c['code'] for c in stats],
                         ['population', 'commercial', 'industrial', 'budget', 'agriculture', 'other'])
        by = {c['code']: c for c in stats}
        self.assertEqual((by['population']['total'], by['population']['active'], by['population']['percent']), (1, 1, 100))
        # прибор без категории считается в «Прочих» и без показаний
        self.assertEqual((by['other']['total'], by['other']['active'], by['other']['percent']), (1, 0, 0))
        self.assertEqual(by['commercial']['total'], 0)
        self.assertEqual(by['commercial']['percent'], 0)

    def test_unknown_category_goes_to_other(self):
        self.assertEqual(dashboard_stats.category_code('weird'), 'other')
        self.assertEqual(dashboard_stats.category_code(None), 'other')
        self.assertEqual(dashboard_stats.category_code('budget'), 'budget')

    def test_history_is_seven_days_with_today_last(self):
        self.assertEqual(dashboard_stats.HISTORY_DAYS, 7)
        stats = dashboard_stats.compute_period_stats('today')[0]['category_stats']
        series, days = dashboard_stats.get_category_history(stats)
        self.assertEqual(len(days), 7)
        self.assertEqual(days[-1], timezone.now().date())
        self.assertEqual(series['population'][-1], 100)   # сегодня есть показание
        self.assertEqual(series['population'][0], 0)      # неделю назад показаний не было
        self.assertTrue(all(len(v) == 7 for v in series.values()))

    def test_history_uses_cached_past_days(self):
        stats = dashboard_stats.compute_period_stats('today')[0]['category_stats']
        dashboard_stats.get_category_history(stats)
        with mock.patch.object(dashboard_stats, '_day_category_counts') as day:
            dashboard_stats.get_category_history(stats)
        day.assert_not_called()

    def test_sparkline_geometry(self):
        spark = dashboard_stats.build_sparkline([50, 60, 70])
        self.assertEqual(spark['line'].count(' '), 2)
        first_y = float(spark['line'].split()[0].split(',')[1])
        last_y = float(spark['line'].split()[-1].split(',')[1])
        self.assertGreater(first_y, last_y)  # рост рисуется вверх (y меньше)
        flat = dashboard_stats.build_sparkline([90, 90, 90])
        ys = {p.split(',')[1] for p in flat['line'].split()}
        self.assertEqual(len(ys), 1)          # ровная линия остаётся ровной
        self.assertIsNone(dashboard_stats.build_sparkline([]))

    def test_cards_view_renders_six_cards(self):
        resp = self.client.get('/category-cards/?period=today')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertEqual(body.count('data-category='), 6)
        self.assertIn('Население', body)
        self.assertIn('Сельхоз', body)
        self.assertIn('7 дн.', body)

    def test_warm_command_fills_history_cache(self):
        call_command('warm_dashboard_cache', stdout=io.StringIO())
        today = timezone.now().date()
        self.assertIsNotNone(cache.get(dashboard_stats.day_cache_key(today)))
