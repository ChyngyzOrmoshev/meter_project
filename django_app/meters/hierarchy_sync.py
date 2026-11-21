"""Синхронизация иерархии сети (подстанция → фидер → ТП) по выгрузкам 1С.

В выгрузках есть только названия ТП и фидера. Подстанции нет, поэтому новые
фидеры кладутся в служебную подстанцию района «Не определена».

Главное требование: не плодить похожие объекты. Для этого названия сравниваются
не напрямую, а по ключу (`tp_key`, `feeder_key`): регистр, латиница/кириллица,
пробелы, дефисы, префиксы «ТП», «ВЛ», «10 кВ», «ф.» не влияют на сравнение.
Существующий объект района ищется по ключу среди ВСЕХ подстанций и фидеров
района, а не только среди служебных.
"""
import re
from collections import Counter

from .models import Substation, Feeder, TransformerSubstation

SERVICE_SUBSTATION = 'Не определена'
SERVICE_FEEDER = 'Не определен'

# Латинские буквы, внешне неотличимые от кириллических (в выгрузках их путают)
_LAT2CYR = str.maketrans('ABCEHKMOPTXYaceopxy', 'АВСЕНКМОРТХУасеорху')
_SEPARATORS = re.compile(r'[\s\-_.,№#"\'«»()]+')


def _fold(value):
    s = str(value or '').strip().translate(_LAT2CYR).lower().replace('ё', 'е')
    return s


def clean_name(value):
    """Название для показа: без лишних пробелов по краям и дублей внутри."""
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def tp_key(value):
    """Ключ ТП. «ТП-ТГ 355», «тп тг355» и «ТГ 355» дают один ключ.

    Символ «/» и буквы сохраняются: «31/1» и «311» — разные ТП.
    """
    s = _fold(value)
    s = re.sub(r'^тп(?=[\s\-_.№#])', '', s.strip())
    s = _SEPARATORS.sub('', s)
    return s if s and s != 'тп' else None


def feeder_key(value):
    """Ключ фидера. «ВЛ-10кВ ф. Насосный», «ВЛ-10кВ ф.Насосный» и «Насосный» совпадают."""
    s = _fold(value)
    s = re.sub(r'(?<![а-я0-9])(вл|кл)(?![а-я])', ' ', s)
    s = re.sub(r'\d+(?:[.,]\d+)?\s*кв', ' ', s)
    s = re.sub(r'(?<![а-я0-9])ф\.|(?<![а-я0-9])фидер(?![а-я])|(?<![а-я0-9])ф(?=[\s\-])', ' ', s)
    s = _SEPARATORS.sub('', s)
    return s or None


def has_tp_prefix(name):
    """Название начинается на «ТП-» (с учётом латиницы и регистра)."""
    return _fold(name).startswith('тп-')


def usable_tp(value):
    """Из колонки «ТП» берутся только значения, начинающиеся на «ТП-».
    Остальное («АС 341», «ТГ 355» и т. п.) — не ТП, такие строки игнорируются."""
    return clean_name(value) if has_tp_prefix(clean_name(value)) else ''


def substation_key(value):
    """Ключ подстанции: «ПС Западная», «Западная», «подстанция Западная» совпадают."""
    s = _fold(value)
    s = re.sub(r'(?<![а-я0-9])(пс|подстанция)(?![а-я])', ' ', s)
    s = _SEPARATORS.sub('', s)
    return s or None


def pick_name(variants, prefer=None):
    """Основное написание: сначала подходящее под `prefer` (например, «ТП-…»),
    затем самое частое, затем более длинное, затем по алфавиту."""
    counter = Counter(clean_name(v) for v in variants if clean_name(v))
    if not counter:
        return None
    return sorted(counter.items(), key=lambda kv: (
        0 if prefer and prefer(kv[0]) else 1, -kv[1], -len(kv[0]), kv[0]))[0][0]


def _same(a, b):
    """Один и тот же объект: по pk, а у ещё не сохранённых — по идентичности."""
    return a is b or (a is not None and b is not None and a.pk is not None and a.pk == b.pk)


class RegionHierarchy:
    """Справочники одного района: поиск по ключу и (при commit) создание."""

    def __init__(self, region, commit=False):
        self.region = region
        self.commit = commit
        self.created = {'substations': [], 'feeders': [], 'tps': []}
        self._service_sub = None
        self._subs = {}      # ключ -> Substation
        self._feeders = {}   # ключ -> [Feeder, ...] (в том числе ещё не сохранённые); похожие фидеры разных подстанций — разные объекты
        self._tps = {}       # ключ -> TransformerSubstation
        self._duplicates = []  # существующие объекты с одним ключом (только отчёт)
        self._load()

    def _load(self):
        subs = list(Substation.objects.filter(region=self.region).order_by('id'))
        for s in subs:
            if s.name == SERVICE_SUBSTATION:
                self._service_sub = s
                continue
            key = substation_key(s.name)
            if key and key not in self._subs:
                self._subs[key] = s
            elif key:
                self._duplicates.append(('substation', self._subs[key].name, s.name))
        feeders = Feeder.objects.filter(substation__region=self.region).select_related('substation').order_by('id')
        for f in feeders:
            key = feeder_key(f.name)
            if not key:
                continue
            same = self._feeders.setdefault(key, [])
            twin = next((x for x in same if _same(x.substation, f.substation)), None)
            if twin is not None:
                self._duplicates.append(('feeder', twin.name, f.name))
            else:
                same.append(f)
        tps = TransformerSubstation.objects.filter(feeder__substation__region=self.region).select_related('feeder').order_by('id')
        for t in tps:
            key = tp_key(t.name)
            if not key:
                continue
            if key in self._tps:
                self._duplicates.append(('tp', self._tps[key].name, t.name))
            else:
                self._tps[key] = t

    @property
    def duplicates(self):
        return list(self._duplicates)

    def service_substation(self):
        if self._service_sub is None:
            sub = Substation(region=self.region, name=SERVICE_SUBSTATION)
            if self.commit:
                sub, _ = Substation.objects.get_or_create(region=self.region, name=SERVICE_SUBSTATION)
            self._service_sub = sub
            self.created['substations'].append(SERVICE_SUBSTATION)
        return self._service_sub

    def get_substation(self, raw_name, display_name=None):
        """Подстанция района по названию из 1С; нет — создаётся."""
        key = substation_key(raw_name)
        if not key:
            return None
        if key not in self._subs:
            name = display_name or clean_name(raw_name)
            sub = Substation(region=self.region, name=name)
            if self.commit:
                sub, _ = Substation.objects.get_or_create(region=self.region, name=name)
            self.created['substations'].append(name)
            self._subs[key] = sub
        return self._subs[key]

    def _new_feeder(self, name, substation=None):
        sub = substation or self.service_substation()
        feeder = Feeder(name=name, substation=sub)
        if self.commit:
            feeder, _ = Feeder.objects.get_or_create(substation=sub, name=name)
        self.created['feeders'].append(name)
        return feeder

    def get_feeder(self, raw_name, display_name=None, substation=None):
        """Фидер района по названию из 1С.

        Фидер определяется парой «название + подстанция»: «Насосный» двух разных
        подстанций — два фидера. Без указанной подстанции подходит единственный
        фидер с таким названием; если их несколько — однозначно выбрать нельзя
        (None), решает вызывающий (по ТП). Нет совсем — создаётся в `substation`
        или, если она не указана, в служебной.
        """
        key = feeder_key(raw_name)
        if not key:
            return None
        same = self._feeders.setdefault(key, [])
        if substation is not None:
            for f in same:
                if _same(f.substation, substation):
                    return f
        elif len(same) == 1:
            return same[0]
        elif len(same) > 1:
            return None
        feeder = self._new_feeder(display_name or clean_name(raw_name), substation)
        same.append(feeder)
        return feeder

    def service_feeder(self):
        return self.get_feeder(SERVICE_FEEDER)

    def get_tp(self, raw_name, feeder_raw=None, display_name=None, feeder_display=None, feeder_substation=None):
        """ТП района по названию из 1С.

        Новая ТП создаётся под фидером из `feeder_raw` (основной фидер этой ТП
        в выгрузке), а если фидера нет — под служебным фидером.
        """
        key = tp_key(raw_name)
        if not key:
            return None
        if key not in self._tps:
            feeder = self.get_feeder(feeder_raw, feeder_display, feeder_substation) if feeder_raw else None
            feeder = feeder or self.service_feeder()
            name = display_name or clean_name(raw_name)
            tp = TransformerSubstation(name=name, feeder=feeder)
            if self.commit:
                tp, _ = TransformerSubstation.objects.get_or_create(feeder=feeder, name=name)
            self.created['tps'].append(name)
            self._tps[key] = tp
        return self._tps[key]


def aggregate_names(entries):
    """По строкам 1С находит основной фидер (и его подстанцию) каждой ТП и типичные написания.

    entries — итерируемое (tp_raw, feeder_raw[, substation_raw]). Возвращает словарь:
    tp_main_feeder: ключ ТП → сырое название основного фидера,
    tp_main_sub: ключ ТП → название подстанции этого фидера по строкам этой ТП (если указана),
    feeder_main_sub: ключ фидера → подстанция, только если в выгрузке она одна
        (похожий фидер у разных подстанций сюда не попадает: его решает ТП),
    tp_names / feeder_names / sub_names: ключ → самое частое написание.
    """
    tp_variants, feeder_variants, sub_variants = {}, {}, {}
    tp_feeders, tp_feeder_subs, feeder_subs = {}, {}, {}
    for entry in entries:
        tp_raw, feeder_raw = usable_tp(entry[0]), entry[1]
        sub_raw = entry[2] if len(entry) > 2 else ''
        tk, fk, sk = tp_key(tp_raw), feeder_key(feeder_raw), substation_key(sub_raw)
        if sk:
            sub_variants.setdefault(sk, []).append(sub_raw)
            if fk:
                feeder_subs.setdefault(fk, Counter())[sk] += 1
        if tk:
            tp_variants.setdefault(tk, []).append(tp_raw)
        if fk:
            feeder_variants.setdefault(fk, []).append(feeder_raw)
        if tk and fk:
            tp_feeders.setdefault(tk, Counter())[fk] += 1
            if sk:
                tp_feeder_subs.setdefault((tk, fk), Counter())[sk] += 1
    feeder_names = {k: pick_name(v) for k, v in feeder_variants.items()}
    sub_names = {k: pick_name(v) for k, v in sub_variants.items()}
    main_feeder, main_sub = {}, {}
    for tk, counter in tp_feeders.items():
        fk = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        main_feeder[tk] = feeder_names[fk]
        subs = tp_feeder_subs.get((tk, fk))
        if subs:
            main_sub[tk] = sub_names[sorted(subs.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]]
    return {
        'tp_names': {k: pick_name(v, prefer=has_tp_prefix) for k, v in tp_variants.items()},
        'feeder_names': feeder_names,
        'tp_main_feeder': main_feeder,
        'tp_main_sub': main_sub,
        'sub_names': sub_names,
        'feeder_main_sub': {fk: sub_names[next(iter(c))] for fk, c in feeder_subs.items() if len(c) == 1},
    }


def resolve_for_device(hier, agg, tp_raw, feeder_raw, substation_raw=''):
    """(substation, feeder, tp) для строки 1С.

    Правила:
    * нет допустимой ТП («ТП-…») — (None, None, None): фидер и подстанция
      ставятся прибору только вместе с ТП;
    * есть ТП, но в строке нет ни фидера, ни подстанции — прибор получает
      только ТП;
    * фидер — это пара «название + подстанция»: подстанция из строки главнее;
      если её нет, подстанцию подсказывает ТП этой строки (основной фидер
      ТП по выгрузке района), затем единственная подстанция этого фидера;
    * похожий фидер у разных подстанций без подсказки не выбирается (None).
    """
    tp_raw = usable_tp(tp_raw)
    fk, tk, sk = feeder_key(feeder_raw), tp_key(tp_raw), substation_key(substation_raw)
    if not tk:
        return None, None, None
    sub_names = agg.get('sub_names', {})
    named_sub = hier.get_substation(substation_raw, sub_names.get(sk)) if sk else None
    main_feeder = agg['tp_main_feeder'].get(tk)
    main_fk = feeder_key(main_feeder) if main_feeder else None
    tp_sub_name = agg.get('tp_main_sub', {}).get(tk)

    def home_sub(feeder_k, use_row=True):
        """Подстанция фидера: из строки, иначе по ТП, иначе единственная по выгрузке."""
        if use_row and named_sub is not None:
            return named_sub
        if tp_sub_name and feeder_k == main_fk:
            return hier.get_substation(tp_sub_name)
        only = agg.get('feeder_main_sub', {}).get(feeder_k)
        return hier.get_substation(only) if only else None

    feeder = None
    if fk:
        feeder = hier.get_feeder(feeder_raw, agg['feeder_names'].get(fk), home_sub(fk))
    tp = hier.get_tp(
        tp_raw,
        feeder_raw=main_feeder,
        display_name=agg['tp_names'].get(tk),
        feeder_display=main_feeder,
        feeder_substation=home_sub(main_fk, use_row=False) if main_fk else None,
    )
    substation = named_sub if named_sub is not None else (feeder.substation if feeder is not None else None)
    return substation, feeder, tp
