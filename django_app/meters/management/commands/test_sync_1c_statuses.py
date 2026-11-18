import os
import re
import glob
from datetime import datetime, timedelta
import pandas as pd
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import OuterRef, Subquery
from meters.models import Device, Reading

BASE_1C_PATH = '/app/1c_reports'
EXPORT_DIR = '/app/exports'

def norm_serial(s):
    if pd.isna(s) or str(s).strip() == '':
        return '0'
    s = str(s).strip()
    try:
        num = float(s.replace(',', '.'))
        return str(int(num))
    except ValueError:
        pass
    digits = re.sub(r'\D', '', s)
    return digits.lstrip('0') or '0'

class Command(BaseCommand):
    help = 'Тестовая сверка статусов с 1С'

    def handle(self, *args, **options):
        if not os.path.exists(BASE_1C_PATH):
            self.stderr.write(self.style.ERROR(f"❌ Папка {BASE_1C_PATH} не найдена."))
            return

        os.makedirs(EXPORT_DIR, exist_ok=True)

        folders = [f for f in os.listdir(BASE_1C_PATH) if re.match(r'^\d{4}-\d{2}-\d{2}$', f)]
        latest_folder = sorted(folders)[-1]
        self.stdout.write(f"📁 Читаем папку: {latest_folder}")
        
        files = glob.glob(os.path.join(BASE_1C_PATH, latest_folder, '*.xlsx'))
        active_1c_serials = {} 

        for file_path in files:
            filename = os.path.basename(file_path)
            district = filename.replace('.xlsx', '').strip()
            try:
                df = pd.read_excel(file_path, engine='calamine', dtype=str)
                
                col_serial = next((col for col in df.columns if 'заводской' in str(col).lower() or 'завод' in str(col).lower()), None)
                col_model = next((col for col in df.columns if 'счетчик' in str(col).lower() or 'тип пу' in str(col).lower()), None)
                col_category = next((col for col in df.columns if 'категория' in str(col).lower()), None)

                if not col_serial: continue

                # ОТЛАДКА ДЛЯ МАНАСА
                if 'Манас' in filename:
                    self.stdout.write(self.style.WARNING(f"DEBUG: Файл {filename} прочитан. Колонка: '{col_serial}'"))
                    sample = df[col_serial].dropna().astype(str).head(3).tolist()
                    self.stdout.write(self.style.WARNING(f"DEBUG: Первые 3 значения: {sample}"))

                for _, row in df.iterrows():
                    raw_sn = str(row[col_serial]).strip()
                    if not raw_sn or 'количество' in raw_sn.lower():
                        continue
                    
                    norm_sn = norm_serial(raw_sn)
                    if norm_sn == '0': continue
                    
                    if '84151000191' in norm_sn or '84151000191' in raw_sn:
                        print(f"🔍 [1С] raw='{raw_sn}' -> norm='{norm_sn}' (район: {district})")
                    
                    model_1c = str(row[col_model]).strip() if col_model and not pd.isna(row[col_model]) else ''
                    cat_1c = str(row[col_category]).strip() if col_category and not pd.isna(row[col_category]) else ''
                    
                    if norm_sn not in active_1c_serials or (not active_1c_serials[norm_sn]['model'] and model_1c):
                        active_1c_serials[norm_sn] = {
                            'raw': raw_sn,
                            'district': district,
                            'model': model_1c,
                            'category': cat_1c
                        }
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"  ❌ Ошибка {filename}: {e}"))

        self.stdout.write(f"✅ Всего уникальных счетчиков в 1С: {len(active_1c_serials)}")


        # 3. Готовим справочники из НАШЕЙ базы
        seven_days_ago = timezone.now() - timedelta(days=7)
        last_ts_subquery = Reading.objects.filter(device=OuterRef('pk')).order_by('-timestamp').values('timestamp')[:1]
        last_val_subquery = Reading.objects.filter(device=OuterRef('pk')).order_by('-timestamp').values('reading_value')[:1]
        
        our_devices = list(Device.objects.filter(status='active').select_related('model', 'region', 'tp', 'feeder').annotate(
            last_ts=Subquery(last_ts_subquery),
            last_val=Subquery(last_val_subquery)
        ))
        
        devices_map = {}         
        devices_map_norm = {}    
        devices_map_last8 = {}   

        for d in our_devices:
            raw_sn = str(d.serial_number).strip()
            devices_map[raw_sn] = d
            
            norm = norm_serial(raw_sn)
            
            # ОТЛАДКА ДЛЯ БАЗЫ
            if '84151000191' in norm or '84151000191' in raw_sn:
                print(f"🔍 [БД] raw='{raw_sn}' -> norm='{norm}' (ID: {d.id})")
            
            if norm not in devices_map_norm:
                devices_map_norm[norm] = []
            devices_map_norm[norm].append(d)
            
            if len(norm) >= 8:
                suffix = norm[-8:]
                if suffix not in devices_map_last8:
                    devices_map_last8[suffix] = []
                devices_map_last8[suffix].append(d)

        new_meters = []
        missing_in_1c = []
        matched_log = [] 
        matched_device_ids = set() 

        # 4. Ищем совпадения
        for norm_1c, info in active_1c_serials.items():
            found_devices_dict = {} 
            raw_1c = info['raw']
            
            # 1. Точное совпадение
            if raw_1c in devices_map:
                d = devices_map[raw_1c]
                found_devices_dict[d.id] = (d, 'exact')
                
            # 2. Нормализованное совпадение (срезает ведущие нули)
            if norm_1c in devices_map_norm:
                for d in devices_map_norm[norm_1c]:
                    if d.id not in found_devices_dict:
                        found_devices_dict[d.id] = (d, 'norm')
            
            # 3. По последним 8 цифрам (если в 1С длиннее)
            if len(norm_1c) >= 8:
                for d in devices_map_last8.get(norm_1c[-8:], []):
                    if d.id not in found_devices_dict:
                        found_devices_dict[d.id] = (d, 'last8')
            # 4. По последним 8 цифрам с добавлением нулей (если короче)
            elif len(norm_1c) < 8:
                for d in devices_map_last8.get(norm_1c.zfill(8), []):
                    if d.id not in found_devices_dict:
                        found_devices_dict[d.id] = (d, 'last8_zfill')
            
            if found_devices_dict:
                for d_id, (d, match_type) in found_devices_dict.items():
                    matched_device_ids.add(d_id)
                    matched_log.append({
                        'serial_1c': raw_1c,
                        'norm_1c': norm_1c,
                        'serial_db': d.serial_number,
                        'match_type': match_type,
                        'district_1c': info['district'],
                        'model_1c': info['model']
                    })
            else:
                new_meters.append({
                    'serial_1c': raw_1c,
                    'norm_1c': norm_1c,
                    'district_1c': info['district'],
                    'model_1c': info['model'],
                    'category_1c': info['category']
                })

        # 5. Ищем тех, кто есть у нас, но не найден в 1С
        for d in our_devices:
            if d.id not in matched_device_ids:
                is_online = False
                last_date_str = ''
                last_val_str = ''
                
                if d.last_ts:
                    last_date_str = d.last_ts.strftime('%Y-%m-%d %H:%M:%S')
                    if d.last_ts >= seven_days_ago:
                        is_online = True
                if d.last_val:
                    last_val_str = f"{float(d.last_val):.3f}"

                loc_parts = []
                if d.region: loc_parts.append(d.region.name)
                if d.tp: loc_parts.append(f"ТП: {d.tp.name}")
                if d.feeder: loc_parts.append(f"Ф: {d.feeder.name}")
                location = " / ".join(loc_parts) if loc_parts else "—"

                missing_in_1c.append({
                    'serial_db': d.serial_number,
                    'norm_db': norm_serial(d.serial_number),
                    'model_db': d.model.model_name if d.model else '',
                    'location_db': location,
                    'is_online': 'Да' if is_online else 'Нет',
                    'last_reading_date': last_date_str,
                    'last_reading_val': last_val_str
                })

        # 6. Сохраняем результаты
        if new_meters:
            pd.DataFrame(new_meters).to_csv(os.path.join(EXPORT_DIR, 'new_meters.csv'), index=False, sep=';', encoding='utf-8-sig')
            self.stdout.write(self.style.WARNING(f"⚠️ Новых счетчиков: {len(new_meters)}"))

        if missing_in_1c:
            df_missing = pd.DataFrame(missing_in_1c)
            df_missing = df_missing.sort_values(by=['is_online', 'last_reading_date'], ascending=[False, False])
            df_missing.to_csv(os.path.join(EXPORT_DIR, 'missing_in_1c.csv'), index=False, sep=';', encoding='utf-8-sig')
            self.stdout.write(self.style.WARNING(f"⚠️ Пропали из 1С: {len(missing_in_1c)}"))

        if matched_log:
            pd.DataFrame(matched_log).to_csv(os.path.join(EXPORT_DIR, 'matched.csv'), index=False, sep=';', encoding='utf-8-sig')
            self.stdout.write(self.style.SUCCESS(f"✅ Найдено совпадений: {len(matched_log)} (см. matched.csv)"))

        self.stdout.write("\n🏁 Тест завершен. База данных не изменялась.")