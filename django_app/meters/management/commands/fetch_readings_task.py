from django.core.management.base import BaseCommand
from django.utils import timezone
from meters.models import Device, Reading, ReadingFetchTask
from meters.readings_fetcher import get_reading_fetcher
from meters.utils import get_robot_devices
from datetime import timedelta

class Command(BaseCommand):
    help = 'Выполняет фоновый опрос показаний для устройства'

    def add_arguments(self, parser):
        parser.add_argument('task_id', type=int, help='ID задачи из ReadingFetchTask')

    def handle(self, *args, **options):
        task_id = options['task_id']
        try:
            task = ReadingFetchTask.objects.get(id=task_id)
        except ReadingFetchTask.DoesNotExist:
            self.stderr.write(f'Задача {task_id} не найдена')
            return

        if task.status != 'pending':
            self.stdout.write(f'Задача {task_id} уже обработана')
            return

        task.status = 'running'
        task.started_at = timezone.now()
        task.save()

        device = task.device
        serial_number = task.serial_number

        # Определяем робота
        robot_name = None
        for name in ['cEnergo', 'Sanxing_old', 'SunRise', 'Hexing_KUK', 'RiseSun']:
            if device in get_robot_devices(name):
                robot_name = name
                break

        if not robot_name:
            task.status = 'error'
            task.error_message = 'Не найден источник данных для устройства'
            task.completed_at = timezone.now()
            task.save()
            self.stderr.write(f'Ошибка: не найден источник для {serial_number}')
            return

        fetcher = get_reading_fetcher(robot_name)
        if not fetcher:
            task.status = 'error'
            task.error_message = f'Нет функции загрузки для источника {robot_name}'
            task.completed_at = timezone.now()
            task.save()
            return

        end_date = timezone.now()
        start_date = end_date - timedelta(days=7)

        try:
            readings = fetcher(serial_number, start_date, end_date)
            if readings is None:
                task.status = 'error'
                task.error_message = 'Ошибка при запросе данных из источника'
                task.completed_at = timezone.now()
                task.save()
                return

            if not readings:
                task.status = 'completed'
                task.records_loaded = 0
                task.completed_at = timezone.now()
                task.save()
                self.stdout.write(f'Нет данных за последние 7 дней для {serial_number}')
                return

            # Сохраняем показания
            saved = 0
            for item in readings:
                Reading.objects.update_or_create(
                    device=device,
                    timestamp=item['timestamp'],
                    defaults={
                        'reading_value': item['value'],
                        'notes': f'Точечный опрос ({robot_name})'
                    }
                )
                saved += 1

            task.status = 'completed'
            task.records_loaded = saved
            task.completed_at = timezone.now()
            task.save()
            self.stdout.write(f'Успешно загружено {saved} записей для {serial_number}')

        except Exception as e:
            task.status = 'error'
            task.error_message = str(e)
            task.completed_at = timezone.now()
            task.save()
            self.stderr.write(f'Ошибка при опросе {serial_number}: {e}')