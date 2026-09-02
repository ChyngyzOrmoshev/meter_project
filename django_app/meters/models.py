from django.db import models
from decimal import Decimal

# ---------- МОДЕЛИ ДЛЯ БАЛАНСА ----------
class Region(models.Model):
    name = models.CharField(max_length=100, unique=True)
    head_meter = models.ForeignKey(
        'Device', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='region_head', verbose_name='Головной счётчик района'
    )

    class Meta:
        db_table = 'regions'

    def __str__(self):
        return self.name


class Substation(models.Model):
    name = models.CharField(max_length=100)
    region = models.ForeignKey(Region, on_delete=models.CASCADE, related_name='substations')
    head_meter = models.ForeignKey(
        'Device', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='substation_head', verbose_name='Головной счётчик подстанции'
    )
    voltage_level = models.CharField(max_length=50, blank=True, null=True)  # 110/35/10 кВ
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'substations'
        unique_together = ('region', 'name')

    def __str__(self):
        return f"{self.name} ({self.region.name})"


class Feeder(models.Model):
    name = models.CharField(max_length=100)
    substation = models.ForeignKey(Substation, on_delete=models.CASCADE, related_name='feeders')
    head_meter = models.ForeignKey(
        'Device', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='feeder_head', verbose_name='Головной счётчик фидера'
    )
    voltage_level = models.CharField(max_length=50, blank=True, null=True, verbose_name='Уровень напряжения')
    feeder_type = models.CharField(
        max_length=20,
        choices=[('input', 'Вводной'), ('output', 'Отходящий')],
        default='output',
        verbose_name='Тип фидера'
    )
    direction = models.CharField(
        max_length=20,
        choices=[('receive', 'Приём'), ('give', 'Отдача')],
        default='receive',
        verbose_name='Направление'
    )
    parent_feeder = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='children',
        verbose_name='Родительский фидер (для отходящих)'
    )

    class Meta:
        db_table = 'feeders'
        unique_together = ('substation', 'name')

    def __str__(self):
        return f"{self.name} ({self.substation.name})"


class TransformerSubstation(models.Model):
    name = models.CharField(max_length=100)
    feeder = models.ForeignKey(Feeder, on_delete=models.CASCADE, related_name='tps')
    head_meter = models.ForeignKey(
        'Device', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='tp_head', verbose_name='Головной счётчик ТП'
    )

    class Meta:
        db_table = 'transformer_substations'
        unique_together = ('feeder', 'name')

    def __str__(self):
        return f"{self.name} ({self.feeder.name})"


# ---------- СУЩЕСТВУЮЩИЕ МОДЕЛИ (с изменениями) ----------
class MeterModel(models.Model):
    catalog_code = models.CharField(max_length=100, unique=True)
    model_name = models.CharField(max_length=100)
    digit_capacity = models.CharField(max_length=50)
    phases = models.IntegerField()
    nominal_current = models.CharField(max_length=100, blank=True, null=True)
    nominal_voltage = models.CharField(max_length=100, blank=True, null=True)
    system_type = models.CharField(max_length=100, blank=True, null=True)
    period = models.CharField(max_length=50, blank=True, null=True)
    device_type_id = models.CharField(max_length=50, blank=True, null=True)
    device_type_str = models.CharField(max_length=50, blank=True, null=True)
    manufacturer = models.CharField(max_length=100, blank=True, null=True)  # добавлено ранее

    class Meta:
        db_table = 'meter_models'

    def __str__(self):
        return f"{self.model_name} ({self.catalog_code})"


class Device(models.Model):
    STATUS_CHOICES = [
        ('active', 'Активен'),
        ('repair', 'Ремонт'),
        ('inactive', 'Отключен'),
        ('temporary_off', 'Временно отключен'),
    ]
    CATEGORY_CHOICES = [
        ('population', 'Население'),
        ('commercial', 'Коммерческие'),
        ('industrial', 'Промышленные'),
        ('budget', 'Бюджет'),
        ('agriculture', 'Сельхоз'),
        ('other', 'Прочие'),
    ]
    serial_number = models.CharField(max_length=50, unique=True)
    model = models.ForeignKey(MeterModel, on_delete=models.SET_NULL, null=True, blank=True)
    nominal_current = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    phase = models.IntegerField(null=True, blank=True)
    askue_id = models.CharField(max_length=50, blank=True, null=True)
    api_id = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    groups = models.ManyToManyField("DeviceGroup", blank=True, related_name='devices')
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, blank=True, null=True, default=None)
    is_head_meter = models.BooleanField(default=False, help_text='Является головным счётчиком для ТП/фидера/подстанции/района')

    # Поля для привязки к иерархии баланса
    region = models.ForeignKey('Region', on_delete=models.SET_NULL, null=True, blank=True, related_name='devices')
    substation = models.ForeignKey('Substation', on_delete=models.SET_NULL, null=True, blank=True, related_name='devices')
    feeder = models.ForeignKey('Feeder', on_delete=models.SET_NULL, null=True, blank=True, related_name='devices')
    tp = models.ForeignKey('TransformerSubstation', on_delete=models.SET_NULL, null=True, blank=True, related_name='devices')
    registration_source = models.CharField(max_length=50, blank=True, null=True, verbose_name='Источник регистрации')
    tt_ratio = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name='Коэффициент ТТ')
    tn_ratio = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name='Коэффициент ТН')
    installation_date = models.DateField(null=True, blank=True, verbose_name='Дата ввода')
    replacement_date = models.DateField(null=True, blank=True, verbose_name='Дата замены')
    meter_type = models.CharField(
        max_length=20,
        choices=[('active', 'Активный'), ('reactive', 'Реактивный')],
        default='active',
        verbose_name='Тип счётчика'
    )
    direction = models.CharField(
        max_length=20,
        choices=[('aplus', 'A+'), ('aminus', 'A-')],
        default='aplus',
        verbose_name='Направление учёта'
    )

    @property
    def calc_coefficient(self):
        """Расчётный коэффициент = ТТ * ТН (если оба заданы)."""
        if self.tt_ratio and self.tn_ratio:
            return self.tt_ratio * self.tn_ratio
        return Decimal(1)

    class Meta:
        db_table = 'devices'

    def __str__(self):
        return f"{self.serial_number} ({self.model.model_name if self.model else 'No model'})"


class Reading(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE)
    timestamp = models.DateTimeField()
    reading_value = models.DecimalField(max_digits=20, decimal_places=8)
    notes = models.CharField(max_length=200, blank=True, null=True)
    direction = models.CharField(
        max_length=10,
        choices=[('aplus', 'A+'), ('aminus', 'A-')],
        default='aplus',
        verbose_name='Направление учёта'
    )

    class Meta:
        db_table = 'readings'
        unique_together = ('device', 'timestamp', 'direction')  # уникальность по трём полям
        indexes = [
            models.Index(fields=['device', 'timestamp', 'direction'], name='idx_readings_dev_ts_dir'),
            models.Index(fields=['timestamp'], name='idx_readings_ts'),
            models.Index(fields=['notes', 'timestamp'], name='idx_readings_notes_ts'),
        ]


class SyncStatus(models.Model):
    robot_name = models.CharField(max_length=50, unique=True)
    status = models.CharField(max_length=20)
    last_update = models.DateTimeField()
    records_processed = models.IntegerField(default=0)
    error = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'sync_status'

    def __str__(self):
        return f"{self.robot_name} - {self.status}"

# группировка счетчиков

class DeviceGroup(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'device_groups'
        ordering = ['name']

    def __str__(self):
        return self.name

class CompareResult(models.Model):
    STATUS_CHOICES = [
        ('new', 'Новое'),
        ('fixed', 'Исправлено'),
        ('ignored', 'Пропущено'),
        ('in_progress', 'В процессе'),
        ('auto_fixed', 'Автозакрыто'),
    ]

    serial_1c = models.CharField(max_length=50, db_index=True)
    model_1c = models.CharField(max_length=100, blank=True)
    
    device = models.ForeignKey('Device', on_delete=models.SET_NULL, null=True, blank=True)
    model_db = models.CharField(max_length=100, blank=True)
    model_match_type = models.CharField(
        max_length=20,
        choices=[('exact', 'Полное'), ('partial', 'Частичное'), ('none', 'Не совпадает')],
        default='none'
    )
    
    is_online = models.BooleanField(default=False)
    last_reading_date = models.DateTimeField(null=True, blank=True)
    last_reading_value = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    region = models.ForeignKey('Region', on_delete=models.SET_NULL, null=True, blank=True)
    verified = models.BooleanField(default=False, help_text='Отметка о проверке исправления администратором/оператором')

    
    action_type = models.CharField(
        max_length=20,
        choices=[
            ('ok', 'OK'),
            ('fix_model', 'Исправить Тип ПУ'),
            ('fix_serial', 'Исправить номер'),
            ('offline', 'Проверить связь'),
            ('temporary_off', 'Временно отключен'),
            ('not_found', 'Не найден'),
        ],
        default='not_found'
    )
    action_text = models.TextField(blank=True)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='new')
    comment = models.TextField(blank=True)
    fixed_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True)
    fixed_at = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    check_date = models.DateField(auto_now_add=True)
    
    class Meta:
        db_table = 'compare_results'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['serial_1c']),
            models.Index(fields=['status']),
            models.Index(fields=['action_type']),
            models.Index(fields=['check_date']),
        ]
    
    def __str__(self):
        return f"{self.serial_1c} - {self.status}"
        
    upload_region = models.ForeignKey('Region', on_delete=models.SET_NULL, null=True, blank=True, related_name='compare_results', verbose_name='Район загрузки')

class ReadingFetchTask(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Ожидание'),
        ('running', 'Выполняется'),
        ('completed', 'Завершено'),
        ('error', 'Ошибка'),
    ]
    device = models.ForeignKey('Device', on_delete=models.CASCADE)
    serial_number = models.CharField(max_length=50, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    records_loaded = models.IntegerField(default=0)
    error_message = models.TextField(blank=True)
    # Результат можно хранить в виде JSON, но достаточно просто записать показания в таблицу Reading

    class Meta:
        db_table = 'reading_fetch_tasks'
        ordering = ['-created_at']

class MeterConnection(models.Model):
    """Привязка счётчика к объекту (ПС, фидер, ТП) с ролью."""
    device = models.ForeignKey('Device', on_delete=models.CASCADE, verbose_name='Счётчик')
    connected_object_type = models.CharField(
        max_length=20,
        choices=[
            ('substation', 'Подстанция'),
            ('feeder', 'Фидер'),
            ('tp', 'ТП'),
        ],
        verbose_name='Тип объекта'
    )
    connected_object_id = models.PositiveIntegerField(verbose_name='ID объекта')
    role = models.CharField(max_length=50, blank=True, null=True, verbose_name='Роль (main/backup)')

    class Meta:
        db_table = 'meter_connections'
        indexes = [
            models.Index(fields=['connected_object_type', 'connected_object_id']),
        ]

    def __str__(self):
        return f"{self.device.serial_number} → {self.connected_object_type} #{self.connected_object_id}"


class CoefficientHistory(models.Model):
    """История изменения коэффициентов ТТ/ТН для счётчика."""
    device = models.ForeignKey('Device', on_delete=models.CASCADE, verbose_name='Счётчик')
    tt_ratio = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='ТТ')
    tn_ratio = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='ТН')
    start_date = models.DateField(verbose_name='Дата начала действия')
    end_date = models.DateField(null=True, blank=True, verbose_name='Дата окончания действия')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'coefficient_history'
        ordering = ['-start_date']

    def __str__(self):
        return f"{self.device.serial_number} {self.start_date}–{self.end_date or 'н.в.'}"


class CalculationPeriod(models.Model):
    """Расчётный период (декада или месяц)."""
    PERIOD_TYPES = [
        ('decade1', 'Декада 1'),
        ('decade2', 'декада 2'),
        ('decade3', 'декада 3'),
        ('month', 'Месяц'),
    ]
    substation = models.ForeignKey('Substation', on_delete=models.CASCADE, verbose_name='ПС')
    year = models.IntegerField(verbose_name='Год')
    month = models.IntegerField(verbose_name='Месяц')
    period_type = models.CharField(max_length=10, choices=PERIOD_TYPES, verbose_name='Тип периода')
    start_date = models.DateField(verbose_name='Дата начала')
    end_date = models.DateField(verbose_name='Дата окончания')

    class Meta:
        db_table = 'calculation_periods'
        unique_together = ('substation', 'year', 'month', 'period_type')
        ordering = ['-year', '-month', 'period_type']

    def __str__(self):
        return f"{self.substation.name} {self.month}.{self.year} {self.get_period_type_display()}"


class BalanceResult(models.Model):
    """Сохранённый результат баланса по ПС за период."""
    substation = models.ForeignKey('Substation', on_delete=models.CASCADE, verbose_name='ПС')
    period = models.ForeignKey('CalculationPeriod', on_delete=models.CASCADE, verbose_name='Период')
    input_energy = models.DecimalField(max_digits=20, decimal_places=3, default=0, verbose_name='Поступление, кВт·ч')
    output_energy = models.DecimalField(max_digits=20, decimal_places=3, default=0, verbose_name='Отпуск, кВт·ч')
    imbalance_kwh = models.DecimalField(max_digits=20, decimal_places=3, default=0, verbose_name='Небаланс, кВт·ч')
    imbalance_percent = models.DecimalField(max_digits=6, decimal_places=2, default=0, verbose_name='Небаланс, %')
    calculated_at = models.DateTimeField(auto_now_add=True, verbose_name='Дата расчёта')

    class Meta:
        db_table = 'balance_results'
        ordering = ['-period__year', '-period__month']

    def __str__(self):
        return f"{self.substation.name} {self.period.month}.{self.period.year} — небаланс {self.imbalance_kwh:.3f} кВт·ч"