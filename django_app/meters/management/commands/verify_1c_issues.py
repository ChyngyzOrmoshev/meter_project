import os
import re
import glob
import json
import pandas as pd
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from meters.models import CompareResult, Device, Reading
from meters.views import compare_models 

BASE_1C_PATH = '/app/1c_reports'
EXPORT_DIR = '/app/exports'

def norm_serial(s):
    if pd.isna(s) or str(s).strip() == '': return '0'
    s = str(s).strip()
    try: return str(int(float(s.replace(',', '.'))))
    except ValueError:
        return re.sub(r'\D', '', s).lstrip('0') or '0'

class Command(BaseCommand):
    help = 'Автопроверка заявок по выгрузке 1С'

    def handle(self, *args, **options):
        result_path = os.path.join(EXPORT_DIR, 'verify_1c_result.json')
        
        if not os.path.exists(BASE_1C_PATH):
            with open(result_path, 'w') as f:
                json.dump({'status': 'error', 'message': 'Папка 1С не найдена'}, f)
            return

        folders = [f for f in os.listdir(BASE_1C_PATH) if re.match(r'^\d{4}-\d{2}-\d{2}$', f)]
        if not folders: 
            with open(result_path, 'w') as f:
                json.dump({'status': 'error', 'message': 'Нет папок с датами'}, f)
            return
            
        latest_folder = sorted(folders)[-1]
        files = glob.glob(os.path.join(BASE_1C_PATH, latest_folder, '*.xlsx'))

        # 1. Читаем актуальную 1С
        current_1c_data = {} 
        for file_path in files:
            try:
                df = pd.read_excel(file_path, engine='calamine', dtype=str)
                col_serial = next((col for col in df.columns if 'заводской' in str(col).lower() or 'завод' in str(col).lower()), None)
                col_model = next((col for col in df.columns if 'счетчик' in str(col).lower() or 'тип пу' in str(col).lower()), None)
                if not col_serial: continue

                for _, row in df.iterrows():
                    raw_sn = str(row[col_serial]).strip()
                    if not raw_sn or 'количество' in raw_sn.lower(): continue
                    norm_sn = norm_serial(raw_sn)
                    if norm_sn == '0': continue
                    model_1c = str(row[col_model]).strip() if col_model and not pd.isna(row[col_model]) else ''
                    current_1c_data[norm_sn] = {'model': model_1c, 'raw': raw_sn}
            except Exception:
                pass

        # 2. Берем заявки на проверку
        issues = CompareResult.objects.filter(status='fixed', verified=False)
        total_issues = issues.count()
        verified_count = 0
        seven_days_ago = timezone.now() - timedelta(days=7)

        for issue in issues:
            norm_1c = norm_serial(issue.serial_1c)
            
            # Сценарий А: Не найден, но теперь есть в 1С и в базе
            if issue.action_type == 'not_found':
                if norm_1c in current_1c_data:
                    device = Device.objects.filter(serial_number__endswith=norm_1c[-8:]).first()
                    if device:
                        issue.verified = True
                        issue.save()
                        verified_count += 1
                        continue

            # Сценарий Б: Исправление типа ПУ
            elif issue.action_type == 'fix_model':
                if norm_1c in current_1c_data:
                    current_model_1c = current_1c_data[norm_1c]['model']
                    if compare_models(current_model_1c, issue.model_db) == 'exact':
                        issue.verified = True
                        issue.save()
                        verified_count += 1
                        continue

            # Сценарий В: Оффлайн (появились показания)
            elif issue.action_type == 'offline' and issue.device:
                last_reading = Reading.objects.filter(device=issue.device).order_by('-timestamp').first()
                if last_reading and last_reading.timestamp >= seven_days_ago:
                    issue.verified = True
                    issue.save()
                    verified_count += 1
                    continue

        # 3. Сохраняем результат
        from datetime import datetime
        with open(result_path, 'w', encoding='utf-8') as f:
            json.dump({
                'status': 'success', 
                'total': total_issues, 
                'verified': verified_count,
                'folder_date': latest_folder,
                'timestamp': datetime.now().strftime('%d.%m.%Y %H:%M:%S')
            }, f, ensure_ascii=False)