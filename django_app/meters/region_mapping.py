"""Единый маппинг районов «имя из выгрузки 1С -> имя района в нашей БД» и проверка папки выгрузки 1С."""
import os
import re
import time
from datetime import date

# Название файла выгрузки (без .xlsx) -> Region.name
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
    'ЖПЭС': 'ЖПЭС',
}
REVERSE_REGION_MAPPING = {v: k for k, v in REGION_MAPPING.items()}

# Районы, для которых 1С файла не присылает
NO_FILE_REGIONS = {'ЖПЭС'}

# В выгрузках встречаются латинские буквы вместо кириллических («Сузак ЭC»): приводим к кириллице
_LOOKALIKE = str.maketrans('ABCEHKMOPTXYaceopxy', 'АВСЕНКМОРТХУасеорху')


def norm_name(name):
    return re.sub(r'\s+', ' ', str(name or '').translate(_LOOKALIKE).strip()).casefold()


_BY_NORM = {norm_name(k): k for k in REGION_MAPPING}


def canonical_1c_name(name):
    """Имя района 1С как в REGION_MAPPING (с учётом регистра и латинских двойников); неизвестное возвращается как есть."""
    return _BY_NORM.get(norm_name(name), str(name or '').strip())


def is_mapped(name):
    return norm_name(name) in _BY_NORM


def db_region_name(name_1c):
    """Имя района в нашей БД по имени из 1С; для неизвестного None."""
    return REGION_MAPPING.get(canonical_1c_name(name_1c))


def display_name(name):
    """Единое название района для таблиц: имя из БД (из 1С или из БД приходит одно и то же)."""
    canon = canonical_1c_name(name)
    if canon in REGION_MAPPING:
        return REGION_MAPPING[canon]
    return str(name or '').strip() or 'Без района'


def expected_1c_files():
    """Имена районов 1С, для которых ждём файл (12 штук)."""
    return [k for k in REGION_MAPPING if k not in NO_FILE_REGIONS]


def check_folder(base_path, folder, db_region_names, settle_sec=0, now=None):
    """
    Проверка папки выгрузки 1С перед сверкой. Возвращает (ok, problems).
    Проблемы: нет папки, нет файла района, файл не из маппинга, дубли, пустой файл,
    район из маппинга отсутствует в нашей БД, файл ещё записывается (изменён менее settle_sec назад).
    """
    problems = []
    path = os.path.join(base_path, folder)
    if not os.path.isdir(path):
        return False, [f'папки {folder} нет в 1c_reports']

    now_ts = now if now is not None else time.time()
    found = {}
    for fn in sorted(os.listdir(path)):
        low = fn.lower()
        if fn.startswith('~$'):
            problems.append(f'файл открыт в Excel: {fn}')
            continue
        if not low.endswith('.xlsx'):
            continue
        name = fn[:-5].strip()
        full = os.path.join(path, fn)
        if not is_mapped(name):
            problems.append(f'район «{name}» нет в маппинге')
            continue
        canon = canonical_1c_name(name)
        if canon in found:
            problems.append(f'два файла для района «{canon}»')
            continue
        found[canon] = full
        try:
            st = os.stat(full)
        except OSError as e:
            problems.append(f'не читается {fn}: {e}')
            continue
        if st.st_size == 0:
            problems.append(f'пустой файл: {fn}')
        elif settle_sec and now_ts - st.st_mtime < settle_sec:
            problems.append(f'файл ещё записывается: {fn}')

    missing = [n for n in expected_1c_files() if n not in found]
    if missing:
        problems.append(f'нет файлов ({len(missing)} из {len(expected_1c_files())}): ' + ', '.join(missing))

    db_names = {str(n).lower() for n in db_region_names}
    for canon in found:
        target = REGION_MAPPING[canon]
        if target.lower() not in db_names:
            problems.append(f'района «{target}» из маппинга нет в нашей базе')
    return (not problems), problems


def today_folder():
    return date.today().strftime('%Y-%m-%d')
