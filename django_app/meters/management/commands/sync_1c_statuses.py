import os
import re
import glob
import json
from datetime import datetime, timedelta
import pandas as pd
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import OuterRef, Subquery
from django.db import transaction
from meters.models import Device, Reading, Region
from meters.hierarchy_sync import RegionHierarchy, aggregate_names, resolve_for_device, clean_name, usable_tp

BASE_1C_PATH = '/app/1c_reports'
EXPORT_DIR = '/app/exports'

# ЖЕСТКИЙ МАППИНГ РАЙОНОВ (Имя из 1С -> Имя в нашей БД)
REGION_MAPPING = {
    'Манас РЭС': '01 Манас',
    'Кок-Арт': '02 Кок-Арт',
    'Сузак ЭС': '03 Сузак',
    'Базар-Коргон РЭС': '04 Базар-Коргон',
    'Ноокен': '05 Ноокен',
    'Майлуу-Суу': '06 Майлуу-Суу',
    'Таш-Комур': '07 Таш-Комур',
    'Аксы РЭС': '08 Аксы',
    'Ала-Бука РЭС': '09 Ала-Бука',
    'Чаткал': '10 Чаткал',
    'Кара-Кол РЭС': '11 Кара-Куль',
    'Токтогул': '12 Токтогул',
    'ЖПЭС': 'ЖПЭС'
}

# Обратный маппинг для склеивания статистики (База -> 1С)
REVERSE_REGION_MAPPING = {v: k for k, v in REGION_MAPPING.items()}

def get_district_key(name):
    """Приводит название района к единому стандарту (имени файла 1С)"""
    if not name: return "Без района"
    # Если пришло имя из базы (например, "01 Манас"), меняем на "Манас РЭС"
    if name in REVERSE_REGION_MAPPING:
        return REVERSE_REGION_MAPPING[name]
    return name

def norm_serial(s):
    if pd.isna(s) or str(s).strip() == '':
        return '0'
    s = str(s).strip()
    try:
        return str(int(float(s.replace(',', '.'))))
    except ValueError:
        digits = re.sub(r'\D', '', s)
        return digits.lstrip('0') or '0'

def map_category(cat_1c):
    if not cat_1c or pd.isna(cat_1c): return None
    cat = str(cat_1c).lower().strip()
    if 'бюджет' in cat: return 'budget'
    if 'пром' in cat: return 'industrial'
    if 'комм' in cat: return 'commercial'
    if 'быт' in cat or 'населен' in cat: return 'population'
    if 'сельхоз' in cat or 'агро' in cat: return 'agriculture'
    if 'проч' in cat: return 'other'
    return 'other'

def get_region_by_district(district_str, db_regions):
    mapped_name = REGION_MAPPING.get(district_str)
    if mapped_name and mapped_name.lower() in db_regions:
        return db_regions[mapped_name.lower()]

    dist_lower = district_str.lower().strip()
    if dist_lower in db_regions: return db_regions[dist_lower]
    clean_dist = dist_lower.replace(' рэс', '').replace(' эc', '').replace(' эк', '').strip()
    if clean_dist in db_regions: return db_regions[clean_dist]
    
    for db_name, region_obj in db_regions.items():
        if clean_dist in db_name or db_name in clean_dist:
            return region_obj
    return None

class Command(BaseCommand):
    help = 'Сверка с 1С (с обновлением района и категории)'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--date', type=str)

    def handle(self, *args, **options):
        if not os.path.exists(BASE_1C_PATH): return
        os.makedirs(EXPORT_DIR, exist_ok=True)

        if options.get('date'):
            latest_folder = options['date']
        else:
            folders = [f for f in os.listdir(BASE_1C_PATH) if re.match(r'^\d{4}-\d{2}-\d{2}$', f)]
            if not folders: return
            latest_folder = sorted(folders)[-1]

        self.stdout.write(f"📁 Читаем папку: {latest_folder}")
        db_regions = {r.name.lower(): r for r in Region.objects.all()}

        files = glob.glob(os.path.join(BASE_1C_PATH, latest_folder, '*.xlsx'))
        active_1c_serials = {} 
        district_entries = {}  # район 1С -> [(ТП, фидер)] по всем строкам
        district_stats = {}

        def init_district(name):
            # Используем get_district_key для склейки
            key = get_district_key(name)
            if key not in district_stats:
                district_stats[key] = {'total_1c': 0, 'new': 0, 'missing': 0, 'deactivated': 0, 'activated': 0, 'updated': 0}
            return key

        # 1. Читаем 1С
        for file_path in files:
            filename = os.path.basename(file_path)
            if filename.startswith('~$'):  # временный файл открытого Excel
                continue
            district = filename.replace('.xlsx', '').strip()
            dist_key = init_district(district)
            
            try:
                df = pd.read_excel(file_path, engine='calamine', dtype=str)
                col_serial = next((col for col in df.columns if 'заводской' in str(col).lower() or 'завод' in str(col).lower()), None)
                col_model = next((col for col in df.columns if 'счетчик' in str(col).lower() or 'тип пу' in str(col).lower()), None)
                col_category = next((col for col in df.columns if 'категория' in str(col).lower()), None)
                col_tp = next((col for col in df.columns if str(col).strip().lower() == 'тп'), None)
                col_feeder = next((col for col in df.columns if 'фидер' in str(col).lower()), None)
                col_sub = next((col for col in df.columns if 'подстанц' in str(col).lower() or str(col).strip().lower() == 'пс'), None)

                if not col_serial: continue

                for _, row in df.iterrows():
                    raw_sn = str(row[col_serial]).strip()
                    if not raw_sn or 'количество' in raw_sn.lower(): continue
                    norm_sn = norm_serial(raw_sn)
                    if norm_sn == '0': continue
                    
                    model_1c = str(row[col_model]).strip() if col_model and not pd.isna(row[col_model]) else ''
                    cat_1c = str(row[col_category]).strip() if col_category and not pd.isna(row[col_category]) else ''
                    tp_1c = clean_name(row[col_tp]) if col_tp and not pd.isna(row[col_tp]) else ''
                    feeder_1c = clean_name(row[col_feeder]) if col_feeder and not pd.isna(row[col_feeder]) else ''
                    sub_1c = clean_name(row[col_sub]) if col_sub and not pd.isna(row[col_sub]) else ''
                    district_entries.setdefault(district, []).append((tp_1c, feeder_1c, sub_1c))
                    
                    if norm_sn not in active_1c_serials:
                        district_stats[dist_key]['total_1c'] += 1
                    
                    if norm_sn not in active_1c_serials or (not active_1c_serials[norm_sn]['model'] and model_1c):
                        active_1c_serials[norm_sn] = {
                            'raw': raw_sn, 'district': district, 'model': model_1c, 'category': cat_1c,
                            'tp': tp_1c, 'feeder': feeder_1c, 'sub': sub_1c,
                        }
            except Exception as e:
                pass

        self.stdout.write(f"✅ Всего в 1С: {len(active_1c_serials)}")

        # 2. Готовим БД
        last_ts_subquery = Reading.objects.filter(device=OuterRef('pk')).order_by('-timestamp').values('timestamp')[:1]
        last_val_subquery = Reading.objects.filter(device=OuterRef('pk')).order_by('-timestamp').values('reading_value')[:1]
        
        our_devices = list(Device.objects.filter(status__in=['active', 'inactive']).select_related('model', 'region', 'tp', 'feeder', 'substation').annotate(
            last_ts=Subquery(last_ts_subquery), last_val=Subquery(last_val_subquery)
        ))
        
        devices_map, devices_map_norm, devices_map_last8 = {}, {}, {}
        for d in our_devices:
            raw_sn = str(d.serial_number).strip()
            devices_map[raw_sn] = d
            norm = norm_serial(raw_sn)
            devices_map_norm.setdefault(norm, []).append(d)
            if len(norm) >= 8: devices_map_last8.setdefault(norm[-8:], []).append(d)

        apply_changes = bool(options.get('apply'))
        agg_by_district = {dist: aggregate_names(entries) for dist, entries in district_entries.items()}
        hierarchies = {}  # id района -> RegionHierarchy
        hierarchy_rows = []

        def hierarchy_for(region):
            if region.id not in hierarchies:
                hierarchies[region.id] = RegionHierarchy(region, commit=apply_changes)
            return hierarchies[region.id]

        new_meters, missing_in_1c, to_activate, to_deactivate = [], [], [], []
        to_update_info = [] 
        matched_device_ids = set() 

        # 3. Сверка
        for norm_1c, info in active_1c_serials.items():
            found_devices = []
            raw_1c = info['raw']
            if raw_1c in devices_map: found_devices = [devices_map[raw_1c]]
            elif norm_1c in devices_map_norm: found_devices = devices_map_norm[norm_1c]
            elif len(norm_1c) >= 8 and norm_1c[-8:] in devices_map_last8: found_devices = devices_map_last8[norm_1c[-8:]]
            elif len(norm_1c) < 8 and norm_1c.zfill(8) in devices_map_last8: found_devices = devices_map_last8[norm_1c.zfill(8)]
            
            target_region = get_region_by_district(info['district'], db_regions)
            target_category = map_category(info['category'])
            dist_key = get_district_key(info['district'])

            if found_devices:
                for d in found_devices:
                    matched_device_ids.add(d.id)
                    
                    if d.status == 'inactive':
                        to_activate.append(d)
                        district_stats[dist_key]['activated'] += 1
                    
                    old_region_name = d.region.name if d.region else '—'
                    old_category_name = d.get_category_display() if d.category else '—'

                    needs_update = False
                    if target_region and d.region != target_region:
                        d.region = target_region
                        needs_update = True
                    if target_category and d.category != target_category:
                        d.category = target_category
                        needs_update = True
                    
                    # Иерархия: 1С главнее, пустое значение в 1С ничего не стирает
                    region_for_tree = target_region or d.region
                    old_tree = (d.substation.name if d.substation else '—',
                                d.feeder.name if d.feeder else '—',
                                d.tp.name if d.tp else '—')
                    tree_changed = False
                    if region_for_tree and (usable_tp(info['tp']) or info['feeder'] or info['sub']):
                        sub, feeder, tp = resolve_for_device(
                            hierarchy_for(region_for_tree), agg_by_district[info['district']],
                            info['tp'], info['feeder'], info['sub'])
                        if feeder is not None and (d.feeder_id != feeder.pk or d.feeder_id is None):
                            d.feeder = feeder
                            tree_changed = True
                        if sub is not None and (d.substation_id != sub.pk or d.substation_id is None):
                            d.substation = sub
                            tree_changed = True
                        if tp is not None and (d.tp_id != tp.pk or d.tp_id is None):
                            d.tp = tp
                            tree_changed = True
                    if tree_changed:
                        needs_update = True
                        hierarchy_rows.append({
                            'serial': d.serial_number, 'district_1c': info['district'],
                            'old_substation': old_tree[0], 'new_substation': d.substation.name if d.substation else '—',
                            'old_feeder': old_tree[1], 'new_feeder': d.feeder.name if d.feeder else '—',
                            'old_tp': old_tree[2], 'new_tp': d.tp.name if d.tp else '—',
                        })

                    if needs_update:
                        to_update_info.append({
                            'device': d, 'serial': d.serial_number,
                            'old_region': old_region_name, 'new_region': d.region.name if d.region else '—',
                            'old_category': old_category_name, 'new_category': d.get_category_display(),
                            'district_1c': info['district']
                        })
                        district_stats[dist_key]['updated'] += 1
            else:
                new_meters.append({'serial_1c': raw_1c, 'norm_1c': norm_1c, 'district_1c': info['district'], 'model_1c': info['model'], 'category_1c': info['category']})
                district_stats[dist_key]['new'] += 1

        # 4. Ищем пропавших
        for d in our_devices:
            if d.id not in matched_device_ids and d.status == 'active':
                is_online, days_since, last_date_str = False, 9999, ''
                if d.last_ts:
                    last_date_str = d.last_ts.strftime('%Y-%m-%d %H:%M:%S')
                    days_since = (timezone.now() - d.last_ts).days
                    if days_since < 7: is_online = True

                loc_parts = []
                if d.region: loc_parts.append(d.region.name)
                if d.tp: loc_parts.append(f"ТП: {d.tp.name}")
                
                missing_in_1c.append({
                    'serial_db': d.serial_number, 'norm_db': norm_serial(d.serial_number),
                    'model_db': d.model.model_name if d.model else '', 'location_db': " / ".join(loc_parts) if loc_parts else "—",
                    'is_online': 'Да' if is_online else 'Нет', 'days_since_last': days_since,
                    'last_reading_date': last_date_str, 'last_reading_val': f"{float(d.last_val):.3f}" if d.last_val else ''
                })
                
                # Определяем ключ по базе, чтобы приплюсовать к правильному району
                region_name_db = d.region.name if d.region else "Без района"
                dist_key = init_district(region_name_db) 
                
                district_stats[dist_key]['missing'] += 1
                
                if days_since > 30: 
                    to_deactivate.append(d)
                    district_stats[dist_key]['deactivated'] += 1

        # 5. Сохраняем CSV
        if new_meters: pd.DataFrame(new_meters).to_csv(os.path.join(EXPORT_DIR, 'new_meters.csv'), index=False, sep=';', encoding='utf-8-sig')
        if missing_in_1c:
            df_m = pd.DataFrame(missing_in_1c).sort_values(by=['days_since_last'], ascending=False)
            df_m.to_csv(os.path.join(EXPORT_DIR, 'missing_in_1c.csv'), index=False, sep=';', encoding='utf-8-sig')
        
        if to_update_info:
            updated_rows = [{
                'serial': item['serial'], 'old_region': item['old_region'],
                'new_region': item['new_region'], 'old_category': item['old_category'],
                'new_category': item['new_category'], 'district_1c': item['district_1c']
            } for item in to_update_info]
            pd.DataFrame(updated_rows).to_csv(os.path.join(EXPORT_DIR, 'updated_meters.csv'), index=False, sep=';', encoding='utf-8-sig')

        if hierarchy_rows:
            pd.DataFrame(hierarchy_rows).to_csv(os.path.join(EXPORT_DIR, 'updated_hierarchy.csv'), index=False, sep=';', encoding='utf-8-sig')
        created_rows = [{'region': h.region.name, 'level': level, 'name': name}
                        for h in hierarchies.values()
                        for level, names in (('substation', h.created['substations']), ('feeder', h.created['feeders']), ('tp', h.created['tps']))
                        for name in names]
        duplicates_rows = [{'region': h.region.name, 'level': lvl, 'kept': kept, 'duplicate': dup}
                           for h in hierarchies.values() for lvl, kept, dup in h.duplicates]
        if created_rows:
            pd.DataFrame(created_rows).to_csv(os.path.join(EXPORT_DIR, 'hierarchy_created.csv'), index=False, sep=';', encoding='utf-8-sig')
        if duplicates_rows:
            pd.DataFrame(duplicates_rows).to_csv(os.path.join(EXPORT_DIR, 'hierarchy_duplicates.csv'), index=False, sep=';', encoding='utf-8-sig')

        # 6. Применяем изменения
        deactivated_count, activated_count, updated_count = 0, 0, 0
        
        if options.get('apply'):
            if to_update_info:
                for item in to_update_info:
                    item['device'].save(update_fields=['region', 'category', 'substation', 'feeder', 'tp'])
                    updated_count += 1
            if to_deactivate:
                for d in to_deactivate:
                    d.status = 'inactive'; d.save(update_fields=['status']); deactivated_count += 1
            if to_activate:
                for d in to_activate:
                    d.status = 'active'; d.save(update_fields=['status']); activated_count += 1

        # 7. Сохраняем JSON
        result_data = {
            'status': 'success', 'total_1c': len(active_1c_serials),
            'new_count': len(new_meters), 'missing_count': len(missing_in_1c),
            'candidates_count': len(to_deactivate), 
            'deactivated_count': deactivated_count,
            'activated_count': activated_count, 'updated_count': updated_count,
            'folder_date': latest_folder, 'districts': district_stats,
            'hierarchy_updated': len(hierarchy_rows),
            'hierarchy_created': {'substations': sum(len(h.created['substations']) for h in hierarchies.values()),
                                  'feeders': sum(len(h.created['feeders']) for h in hierarchies.values()),
                                  'tps': sum(len(h.created['tps']) for h in hierarchies.values())},
            'hierarchy_duplicates': len(duplicates_rows),
        }
        with open(os.path.join(EXPORT_DIR, 'sync_1c_result.json'), 'w', encoding='utf-8') as f:
            json.dump(result_data, f, ensure_ascii=False)