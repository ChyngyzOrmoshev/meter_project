"""Синхронизация иерархии (подстанция → фидер → ТП) по данным 1С: ключи и защита от дублей."""
from django.test import TestCase

from meters.hierarchy_sync import (
    SERVICE_FEEDER, SERVICE_SUBSTATION, RegionHierarchy, aggregate_names, feeder_key,
    pick_name, resolve_for_device, tp_key,
)
from meters.models import Feeder, Region, Substation, TransformerSubstation


class KeyTests(TestCase):
    def test_tp_variants_are_same(self):
        keys = {tp_key(v) for v in ['ТП-ТГ 355', 'тп тг355', 'ТГ 355', 'ТП-ТГ-355', 'TП-ТГ 355']}  # последняя — латинская T
        self.assertEqual(len(keys), 1)

    def test_tp_slash_not_merged(self):
        self.assertNotEqual(tp_key('ТП-ТГ 31/1'), tp_key('ТП-ТГ 311'))

    def test_tp_different_numbers(self):
        self.assertNotEqual(tp_key('ТП-АБ 1150'), tp_key('ТП-АБ 1151'))

    def test_tp_empty(self):
        self.assertIsNone(tp_key(''))
        self.assertIsNone(tp_key(None))
        self.assertIsNone(tp_key('ТП'))

    def test_latin_letters_in_tp(self):
        self.assertEqual(tp_key('TП-AБ 12'), tp_key('ТП-АБ 12'))

    def test_feeder_variants_are_same(self):
        keys = {feeder_key(v) for v in [
            'ВЛ-10кВ ф. Насосный', 'ВЛ-10кВ ф.Насосный', 'ВЛ-10 кВ ф Насосный', 'Насосный', 'насосный', 'Ф-Насосный']}
        self.assertEqual(len(keys), 1)

    def test_feeder_voltage_ignored_name_kept(self):
        self.assertEqual(feeder_key('ВЛ-6 кВ Кок-Таш'), feeder_key('ВЛ-10кВ Кок-Таш'))
        self.assertNotEqual(feeder_key('ВЛ-6 кВ Кок-Таш'), feeder_key('ВЛ-6 кВ Кок-Тош'))

    def test_feeder_word_inside_name_is_not_stripped(self):
        # «Фабрика» начинается с «ф», но это часть названия
        self.assertNotEqual(feeder_key('Фабрика'), feeder_key('брика'))

    def test_feeder_digits_matter(self):
        self.assertNotEqual(feeder_key('Фидер 1'), feeder_key('Фидер 2'))

    def test_pick_name_most_common(self):
        self.assertEqual(pick_name(['A', 'B', 'B', ' B ']), 'B')
        self.assertIsNone(pick_name(['', None]))


class HierarchyBase(TestCase):
    def setUp(self):
        self.region = Region.objects.create(name='03 Сузак')


class RegionHierarchyTests(HierarchyBase):
    def test_existing_feeder_is_found_by_key_in_any_substation(self):
        ps = Substation.objects.create(region=self.region, name='ПС Западная')
        feeder = Feeder.objects.create(substation=ps, name='Насосный')
        h = RegionHierarchy(self.region, commit=True)
        self.assertEqual(h.get_feeder('ВЛ-10кВ ф. Насосный'), feeder)
        self.assertEqual(Feeder.objects.count(), 1)
        self.assertEqual(Substation.objects.count(), 1)

    def test_existing_tp_found_under_other_feeder(self):
        ps = Substation.objects.create(region=self.region, name='ПС')
        f1 = Feeder.objects.create(substation=ps, name='Ф1')
        tp = TransformerSubstation.objects.create(feeder=f1, name='ТП-ТГ 355')
        h = RegionHierarchy(self.region, commit=True)
        self.assertEqual(h.get_tp('ТГ 355', feeder_raw='Совсем другой фидер'), tp)
        self.assertEqual(TransformerSubstation.objects.count(), 1)
        self.assertEqual(Feeder.objects.count(), 1)

    def test_new_feeder_goes_to_service_substation(self):
        h = RegionHierarchy(self.region, commit=True)
        feeder = h.get_feeder('ВЛ-10кВ ф. Новый')
        self.assertEqual(feeder.substation.name, SERVICE_SUBSTATION)
        self.assertEqual(Substation.objects.filter(region=self.region).count(), 1)

    def test_repeated_calls_create_once(self):
        h = RegionHierarchy(self.region, commit=True)
        a = h.get_feeder('ВЛ-10кВ ф. Новый')
        b = h.get_feeder('ВЛ-10 кВ ф.Новый')
        self.assertEqual(a.pk, b.pk)
        t1 = h.get_tp('ТП-НВ 1', feeder_raw='ВЛ-10кВ ф. Новый')
        t2 = h.get_tp('нв 1', feeder_raw='ВЛ-10кВ ф. Новый')
        self.assertEqual(t1.pk, t2.pk)
        self.assertEqual(Feeder.objects.count(), 1)
        self.assertEqual(TransformerSubstation.objects.count(), 1)

    def test_second_run_creates_nothing(self):
        for _ in range(2):
            h = RegionHierarchy(self.region, commit=True)
            h.get_tp('ТП-НВ 1', feeder_raw='Новый')
        self.assertEqual(Feeder.objects.count(), 1)
        self.assertEqual(TransformerSubstation.objects.count(), 1)
        self.assertEqual(Substation.objects.count(), 1)

    def test_dry_run_writes_nothing(self):
        h = RegionHierarchy(self.region, commit=False)
        h.get_tp('ТП-НВ 1', feeder_raw='Новый')
        self.assertEqual(Feeder.objects.count(), 0)
        self.assertEqual(TransformerSubstation.objects.count(), 0)
        self.assertEqual(Substation.objects.count(), 0)
        self.assertEqual(h.created['tps'], ['ТП-НВ 1'])
        self.assertEqual(h.created['feeders'], ['Новый'])

    def test_tp_without_feeder_goes_under_service_feeder(self):
        h = RegionHierarchy(self.region, commit=True)
        tp = h.get_tp('ТП-НВ 2')
        self.assertEqual(tp.feeder.name, SERVICE_FEEDER)

    def test_other_region_not_mixed(self):
        other = Region.objects.create(name='04 Другой')
        ps = Substation.objects.create(region=other, name='ПС')
        Feeder.objects.create(substation=ps, name='Насосный')
        h = RegionHierarchy(self.region, commit=True)
        feeder = h.get_feeder('Насосный')
        self.assertEqual(feeder.substation.region, self.region)
        self.assertEqual(Feeder.objects.count(), 2)

    def test_existing_duplicates_reported_and_lowest_id_kept(self):
        ps = Substation.objects.create(region=self.region, name='ПС')
        first = Feeder.objects.create(substation=ps, name='ВЛ-10кВ ф. Насосный')
        Feeder.objects.create(substation=ps, name='Насосный')
        h = RegionHierarchy(self.region, commit=True)
        self.assertEqual(h.get_feeder('Насосный'), first)
        self.assertEqual(len(h.duplicates), 1)


class AggregateTests(HierarchyBase):
    def test_main_feeder_is_most_frequent_and_name_is_most_common(self):
        entries = [
            ('ТП-1', 'ВЛ-10кВ ф. Насосный'), ('ТП-1', 'ВЛ-10кВ ф. Насосный'), ('тп 1', 'Насосный'),
            ('ТП-1', 'Другой'),
            ('', 'Насосный'),
        ]
        agg = aggregate_names(entries)
        self.assertEqual(agg['tp_main_feeder'][tp_key('ТП-1')], 'ВЛ-10кВ ф. Насосный')
        self.assertEqual(agg['tp_names'][tp_key('ТП-1')], 'ТП-1')

    def test_resolve_for_device_blank_values(self):
        h = RegionHierarchy(self.region, commit=True)
        agg = aggregate_names([('', '')])
        self.assertEqual(resolve_for_device(h, agg, '', ''), (None, None, None))
        self.assertEqual(Feeder.objects.count(), 0)

    def test_resolve_for_device_creates_full_chain(self):
        entries = [('ТП-НВ 1', 'Новый'), ('ТП-НВ 1', 'Новый')]
        h = RegionHierarchy(self.region, commit=True)
        sub, feeder, tp = resolve_for_device(h, aggregate_names(entries), 'ТП-НВ 1', 'Новый')
        self.assertEqual(sub.name, SERVICE_SUBSTATION)
        self.assertEqual(feeder.substation, sub)
        self.assertEqual(tp.feeder, feeder)

    def test_resolve_feeder_only(self):
        h = RegionHierarchy(self.region, commit=True)
        sub, feeder, tp = resolve_for_device(h, aggregate_names([('', 'Ф')]), '', 'Ф')
        self.assertIsNotNone(feeder)
        self.assertIsNone(tp)


class TpPrefixAndSubstationTests(HierarchyBase):
    def test_only_tp_prefixed_values_are_taken(self):
        agg = aggregate_names([('АС 341', 'Ф'), ('ТГ 355', 'Ф'), ('ТП-ТГ 355', 'Ф')])
        self.assertEqual(list(agg['tp_names'].values()), ['ТП-ТГ 355'])

    def test_non_prefixed_tp_is_ignored_for_device(self):
        h = RegionHierarchy(self.region, commit=True)
        sub, feeder, tp = resolve_for_device(h, aggregate_names([('АС 341', 'Ф1')]), 'АС 341', 'Ф1')
        self.assertIsNone(tp)
        self.assertIsNotNone(feeder)  # фидер берётся независимо от ТП
        self.assertEqual(TransformerSubstation.objects.count(), 0)

    def test_latin_tp_prefix_accepted(self):
        from meters.hierarchy_sync import usable_tp
        self.assertEqual(usable_tp('TП-AБ 12'), 'TП-AБ 12')
        self.assertEqual(usable_tp('КТП-1'), '')
        self.assertEqual(usable_tp('ТП 12'), '')

    def test_substation_key_variants(self):
        from meters.hierarchy_sync import substation_key
        self.assertEqual(substation_key('ПС Западная'), substation_key('подстанция западная'))
        self.assertEqual(substation_key('Западная'), substation_key('ПС-Западная'))
        self.assertNotEqual(substation_key('Западная'), substation_key('Восточная'))

    def test_existing_substation_found_by_key(self):
        ps = Substation.objects.create(region=self.region, name='ПС Западная')
        h = RegionHierarchy(self.region, commit=True)
        sub, feeder, tp = resolve_for_device(h, aggregate_names([('', 'Новый', 'Западная')]), '', 'Новый', 'Западная')
        self.assertEqual(sub, ps)
        self.assertEqual(feeder.substation, ps)
        self.assertEqual(Substation.objects.count(), 1)

    def test_new_substation_created_once_and_no_service_one(self):
        entries = [('ТП-1', 'Ф1', 'ПС Новая'), ('ТП-2', 'Ф2', 'Новая')]
        agg = aggregate_names(entries)
        h = RegionHierarchy(self.region, commit=True)
        for tp, f, s in entries:
            resolve_for_device(h, agg, tp, f, s)
        self.assertEqual(list(Substation.objects.values_list('name', flat=True)), ['ПС Новая'])
        self.assertEqual(Feeder.objects.filter(substation__name='ПС Новая').count(), 2)

    def test_row_substation_beats_feeder_substation(self):
        old = Substation.objects.create(region=self.region, name='Старая')
        Feeder.objects.create(substation=old, name='Ф1')
        Substation.objects.create(region=self.region, name='Новая')
        h = RegionHierarchy(self.region, commit=True)
        sub, feeder, _ = resolve_for_device(h, aggregate_names([('', 'Ф1', 'Новая')]), '', 'Ф1', 'Новая')
        self.assertEqual(sub.name, 'Новая')
        self.assertEqual(feeder.substation, old)  # существующий фидер не переносим

    def test_no_substation_in_row_uses_service(self):
        h = RegionHierarchy(self.region, commit=True)
        sub, feeder, _ = resolve_for_device(h, aggregate_names([('', 'Ф1')]), '', 'Ф1')
        self.assertEqual(sub.name, SERVICE_SUBSTATION)
