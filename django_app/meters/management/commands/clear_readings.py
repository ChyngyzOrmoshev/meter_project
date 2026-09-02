from django.core.management.base import BaseCommand
from meters.models import Reading, SyncStatus
from django.db import transaction

class Command(BaseCommand):
    help = 'Очищает все показания (Reading) и сбрасывает статусы синхронизаторов'

    def add_arguments(self, parser):
        parser.add_argument(
            '--keep-status',
            action='store_true',
            help='Не сбрасывать статусы синхронизаторов (только удалить показания)',
        )
        parser.add_argument(
            '--robot',
            type=str,
            help='Удалить показания только для конкретного робота (по имени в SyncStatus)',
        )

    def handle(self, *args, **options):
        with transaction.atomic():
            qs = Reading.objects.all()
            if options.get('robot'):
                # Ищем устройства, которые имеют показания с notes, содержащим имя робота
                robot_name = options['robot']
                qs = qs.filter(notes__icontains=robot_name)
                self.stdout.write(f'🔍 Удаление показаний для робота: {robot_name}')

            count = qs.count()
            qs.delete()
            self.stdout.write(self.style.SUCCESS(f'✅ Удалено показаний: {count}'))

            if not options.get('keep_status'):
                # Сбрасываем статусы всех или конкретного робота
                if options.get('robot'):
                    SyncStatus.objects.filter(robot_name=robot_name).update(
                        status='idle', records_processed=0, error=None
                    )
                    self.stdout.write(f'🔄 Сброшен статус для {robot_name}')
                else:
                    SyncStatus.objects.all().update(
                        status='idle', records_processed=0, error=None
                    )
                    self.stdout.write('🔄 Сброшены статусы всех синхронизаторов')