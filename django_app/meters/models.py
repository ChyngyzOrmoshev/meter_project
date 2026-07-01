from django.db import models

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

    class Meta:
        db_table = 'meter_models'
        # опционально: уникальность по паре (model_name, nominal_current), но catalog_code уже уникален

class Device(models.Model):
    serial_number = models.CharField(max_length=50, unique=True)
    model = models.ForeignKey(MeterModel, on_delete=models.SET_NULL, null=True, blank=True)
    nominal_current = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=20, default='active')
    phase = models.IntegerField(null=True, blank=True)
    askue_id = models.CharField(max_length=50, blank=True, null=True)
    api_id = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'devices'

class Reading(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE)
    timestamp = models.DateTimeField()
    reading_value = models.DecimalField(max_digits=20, decimal_places=8)
    notes = models.CharField(max_length=200, blank=True, null=True)

    class Meta:
        db_table = 'readings'
        unique_together = ('device', 'timestamp')
        indexes = [
            models.Index(fields=['device', 'timestamp']),
            models.Index(fields=['timestamp']),
            models.Index(fields=['notes', 'timestamp']),
        ]

class SyncStatus(models.Model):
    robot_name = models.CharField(max_length=50, unique=True)
    status = models.CharField(max_length=20)
    last_update = models.DateTimeField()
    records_processed = models.IntegerField(default=0)
    error = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'sync_status'

class Meta:
    db_table = 'readings'
    unique_together = ('device', 'timestamp')
    indexes = [
        models.Index(fields=['timestamp'], name='idx_readings_ts'),
        models.Index(fields=['device', 'timestamp'], name='idx_readings_dev_ts'),
        models.Index(fields=['notes', 'timestamp'], name='idx_readings_notes_ts'),
    ]