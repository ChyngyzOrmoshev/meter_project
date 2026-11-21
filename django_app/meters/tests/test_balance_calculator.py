"""Расчёт баланса подстанции: коэффициенты, показания на дату, периоды, баланс по фидерам."""
from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase

from meters.balance_calculator import (
    calculate_balance_for_substation, calculate_device_consumption, get_coefficient_for_date,
    get_or_create_period, get_reading_for_date,
)
from meters.models import (
    BalanceResult, CalculationPeriod, CoefficientHistory, Device, Feeder, Reading, Region, Substation,
)


class BalanceBase(TestCase):
    def setUp(self):
        self.region = Region.objects.create(name='03 Сузак')
        self.substation = Substation.objects.create(name='ПС-1', region=self.region)

    def device(self, serial, **kwargs):
        return Device.objects.create(serial_number=serial, **kwargs)

    def reading(self, device, day, value, direction='aplus', hour=12):
        return Reading.objects.create(
            device=device, timestamp=datetime(day.year, day.month, day.day, hour),
            reading_value=Decimal(str(value)), direction=direction,
        )


class CoefficientTests(BalanceBase):
    def history(self, device, start, end=None, tt=100, tn=1):
        return CoefficientHistory.objects.create(
            device=device, start_date=start, end_date=end, tt_ratio=Decimal(tt), tn_ratio=Decimal(tn),
        )

    def test_no_device_gives_one(self):
        self.assertEqual(get_coefficient_for_date(None, date(2026, 11, 1)), Decimal(1))

    def test_device_fields_used_without_history(self):
        d = self.device('1', tt_ratio=Decimal(40), tn_ratio=Decimal(2))
        self.assertEqual(get_coefficient_for_date(d, date(2026, 11, 1)), Decimal(80))

    def test_no_ratios_gives_one(self):
        self.assertEqual(get_coefficient_for_date(self.device('1'), date(2026, 11, 1)), Decimal(1))
        half = self.device('2', tt_ratio=Decimal(40))  # ТН не задан
        self.assertEqual(get_coefficient_for_date(half, date(2026, 11, 1)), Decimal(1))

    def test_history_overrides_device_fields(self):
        d = self.device('1', tt_ratio=Decimal(40), tn_ratio=Decimal(1))
        self.history(d, date(2026, 1, 1), tt=200, tn=2)
        self.assertEqual(get_coefficient_for_date(d, date(2026, 11, 1)), Decimal(400))

    def test_date_before_first_history_uses_device_fields(self):
        d = self.device('1', tt_ratio=Decimal(40), tn_ratio=Decimal(1))
        self.history(d, date(2026, 6, 1), tt=200)
        self.assertEqual(get_coefficient_for_date(d, date(2026, 5, 31)), Decimal(40))

    def test_latest_applicable_record_wins(self):
        d = self.device('1')
        self.history(d, date(2026, 1, 1), date(2026, 6, 1), tt=100)
        self.history(d, date(2026, 6, 1), tt=300)
        self.assertEqual(get_coefficient_for_date(d, date(2026, 5, 31)), Decimal(100))
        self.assertEqual(get_coefficient_for_date(d, date(2026, 6, 1)), Decimal(300))
        self.assertEqual(get_coefficient_for_date(d, date(2026, 11, 1)), Decimal(300))

    def test_expired_record_is_not_applied_after_end_date(self):
        # запись действовала до 1 июня; потом коэффициент снова берётся из полей счётчика
        d = self.device('1', tt_ratio=Decimal(40), tn_ratio=Decimal(1))
        self.history(d, date(2026, 1, 1), date(2026, 6, 1), tt=100)
        self.assertEqual(get_coefficient_for_date(d, date(2026, 5, 31)), Decimal(100))
        self.assertEqual(get_coefficient_for_date(d, date(2026, 6, 1)), Decimal(40))
        self.assertEqual(get_coefficient_for_date(d, date(2026, 11, 1)), Decimal(40))


class ReadingForDateTests(BalanceBase):
    def test_latest_reading_of_the_day(self):
        d = self.device('1')
        self.reading(d, date(2026, 11, 1), 100, hour=8)
        self.reading(d, date(2026, 11, 1), 120, hour=20)
        self.assertEqual(get_reading_for_date(d, date(2026, 11, 1)), Decimal(120))

    def test_direction_is_respected(self):
        d = self.device('1')
        self.reading(d, date(2026, 11, 1), 100, 'aplus')
        self.reading(d, date(2026, 11, 1), 7, 'aminus')
        self.assertEqual(get_reading_for_date(d, date(2026, 11, 1), 'aminus'), Decimal(7))
        self.assertEqual(get_reading_for_date(d, date(2026, 11, 1)), Decimal(100))

    def test_no_reading_gives_none(self):
        self.assertIsNone(get_reading_for_date(self.device('1'), date(2026, 11, 1)))

    def test_other_device_and_day_are_ignored(self):
        d, other = self.device('1'), self.device('2')
        self.reading(other, date(2026, 11, 1), 500)
        self.reading(d, date(2026, 11, 2), 600)
        self.assertIsNone(get_reading_for_date(d, date(2026, 11, 1)))


class ConsumptionTests(BalanceBase):
    def test_difference_times_coefficient(self):
        d = self.device('1', tt_ratio=Decimal(10), tn_ratio=Decimal(1))
        self.reading(d, date(2026, 11, 1), 100)
        self.reading(d, date(2026, 11, 11), 130)
        energy, start, end = calculate_device_consumption(d, date(2026, 11, 1), date(2026, 11, 11))
        self.assertEqual((energy, start, end), (Decimal(300), Decimal(100), Decimal(130)))

    def test_missing_edge_reading_gives_none(self):
        d = self.device('1')
        self.reading(d, date(2026, 11, 1), 100)
        self.assertEqual(calculate_device_consumption(d, date(2026, 11, 1), date(2026, 11, 11)), (None, None, None))

    def test_coefficient_taken_on_end_date(self):
        d = self.device('1')
        CoefficientHistory.objects.create(device=d, tt_ratio=Decimal(5), tn_ratio=Decimal(1), start_date=date(2026, 11, 5))
        self.reading(d, date(2026, 11, 1), 0)
        self.reading(d, date(2026, 11, 11), 10)
        self.assertEqual(calculate_device_consumption(d, date(2026, 11, 1), date(2026, 11, 11))[0], Decimal(50))


class PeriodTests(BalanceBase):
    def period(self, kind, year=2026, month=11):
        return get_or_create_period(self.substation, year, month, kind)

    def test_decades(self):
        self.assertEqual((self.period('decade1').start_date, self.period('decade1').end_date), (date(2026, 11, 1), date(2026, 11, 11)))
        self.assertEqual((self.period('decade2').start_date, self.period('decade2').end_date), (date(2026, 11, 11), date(2026, 11, 21)))
        self.assertEqual((self.period('decade3').start_date, self.period('decade3').end_date), (date(2026, 11, 21), date(2026, 12, 1)))

    def test_december_third_decade_ends_next_year(self):
        p = self.period('decade3', month=12)
        self.assertEqual((p.start_date, p.end_date), (date(2026, 12, 21), date(2027, 1, 1)))

    def test_month_ends_on_last_day(self):
        self.assertEqual(self.period('month').end_date, date(2026, 11, 30))
        self.assertEqual(self.period('month', month=12).end_date, date(2026, 12, 31))
        self.assertEqual(self.period('month', year=2028, month=2).end_date, date(2028, 2, 29))

    def test_period_is_created_once(self):
        a, b = self.period('month'), self.period('month')
        self.assertEqual(a.id, b.id)
        self.assertEqual(CalculationPeriod.objects.count(), 1)


class SubstationBalanceTests(BalanceBase):
    def setUp(self):
        super().setUp()
        self.input_meter = self.device('IN', tt_ratio=Decimal(1), tn_ratio=Decimal(1))
        self.out_meter = self.device('OUT', tt_ratio=Decimal(1), tn_ratio=Decimal(1))
        Feeder.objects.create(name='Ввод', substation=self.substation, head_meter=self.input_meter, feeder_type='input')
        Feeder.objects.create(name='Отход', substation=self.substation, head_meter=self.out_meter, feeder_type='output')

    def fill(self, meter, start, end, direction='aplus'):
        # показания на границах трёх декад: 1, 11, 21 ноября и 1 декабря
        for day, value in zip((date(2026, 11, 1), date(2026, 11, 11), date(2026, 11, 21), date(2026, 12, 1)), (start, end[0], end[1], end[2])):
            self.reading(meter, day, value, direction)

    def test_imbalance_per_decade_and_month(self):
        self.fill(self.input_meter, 1000, (1100, 1250, 1450))   # 100 + 150 + 200 = 450
        self.fill(self.out_meter, 500, (590, 730, 910))          # 90 + 140 + 180 = 410
        result = calculate_balance_for_substation(self.substation, 2026, 11)
        self.assertEqual(result['decade1']['imbalance'], Decimal(10))
        self.assertEqual(result['decade2']['imbalance'], Decimal(10))
        self.assertEqual(result['decade3']['imbalance'], Decimal(20))
        self.assertEqual((result['month']['input'], result['month']['output'], result['month']['imbalance']), (450, 410, 40))
        self.assertAlmostEqual(float(result['month']['percent']), 40 / 450 * 100, places=3)

    def test_results_are_stored_and_reused_without_recalculation(self):
        self.fill(self.input_meter, 1000, (1100, 1250, 1450))
        self.fill(self.out_meter, 500, (590, 730, 910))
        calculate_balance_for_substation(self.substation, 2026, 11)
        self.assertEqual(BalanceResult.objects.count(), 4)  # 3 декады + месяц
        Reading.objects.filter(device=self.input_meter).update(reading_value=0)  # данные «испортились»
        cached = calculate_balance_for_substation(self.substation, 2026, 11)
        self.assertEqual(cached['decade1']['input'], Decimal(100))
        forced = calculate_balance_for_substation(self.substation, 2026, 11, force_recalc=True)
        self.assertEqual(forced['decade1']['input'], Decimal(0))

    def test_returned_energy_aminus_is_subtracted(self):
        self.fill(self.input_meter, 1000, (1100, 1250, 1450))
        self.fill(self.input_meter, 0, (10, 20, 30), direction='aminus')   # отдача 10 + 10 + 10
        result = calculate_balance_for_substation(self.substation, 2026, 11)
        self.assertEqual(result['decade1']['input'], Decimal(90))
        self.assertEqual(result['month']['input'], Decimal(420))

    def test_no_data_gives_zero_without_division_error(self):
        result = calculate_balance_for_substation(self.substation, 2026, 11)
        self.assertEqual((result['month']['input'], result['month']['percent']), (0, 0))

    def test_several_output_feeders_are_summed(self):
        second = self.device('OUT2', tt_ratio=Decimal(1), tn_ratio=Decimal(1))
        Feeder.objects.create(name='Отход 2', substation=self.substation, head_meter=second, feeder_type='output')
        self.fill(self.out_meter, 500, (590, 730, 910))
        self.fill(second, 0, (10, 20, 30))
        result = calculate_balance_for_substation(self.substation, 2026, 11)
        self.assertEqual(result['decade1']['output'], Decimal(100))


class BalanceReportAllViewTests(BalanceBase):
    """Данные диаграмм уходят в json_script списками: строка вместо массива ломала JS (`.map is not a function`)."""

    def test_chart_data_are_lists_and_render_as_json_arrays(self):
        from django.contrib.auth.models import User
        self.client.force_login(User.objects.create_superuser('bal', 'b@b.b', 'x'))
        resp = self.client.get('/balance/report/?year=2026&month=11')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['chart_labels'], ['ПС-1'])
        self.assertIsInstance(resp.context['chart_imbalance'], list)
        body = resp.content.decode()
        self.assertIn('<script id="chartImbalance" type="application/json">[', body)
        self.assertIn('<script id="chartLabels" type="application/json">["', body)
