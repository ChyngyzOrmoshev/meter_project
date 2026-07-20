from django.db import models

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
    serial_number = models.CharField(max_length=50, unique=True)
    model = models.ForeignKey(MeterModel, on_delete=models.SET_NULL, null=True, blank=True)
    nominal_current = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=20, default='active')
    phase = models.IntegerField(null=True, blank=True)
    askue_id = models.CharField(max_length=50, blank=True, null=True)
    api_id = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Поля для привязки к иерархии баланса
    region = models.ForeignKey('Region', on_delete=models.SET_NULL, null=True, blank=True, related_name='devices')
    substation = models.ForeignKey('Substation', on_delete=models.SET_NULL, null=True, blank=True, related_name='devices')
    feeder = models.ForeignKey('Feeder', on_delete=models.SET_NULL, null=True, blank=True, related_name='devices')
    tp = models.ForeignKey('TransformerSubstation', on_delete=models.SET_NULL, null=True, blank=True, related_name='devices')

    class Meta:
        db_table = 'devices'

    def __str__(self):
        return f"{self.serial_number} ({self.model.model_name if self.model else 'No model'})"


class Reading(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE)
    timestamp = models.DateTimeField()
    reading_value = models.DecimalField(max_digits=20, decimal_places=8)
    notes = models.CharField(max_length=200, blank=True, null=True)

    class Meta:
        db_table = 'readings'
        unique_together = ('device', 'timestamp')
        indexes = [
            models.Index(fields=['device', 'timestamp'], name='idx_readings_dev_ts'),
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