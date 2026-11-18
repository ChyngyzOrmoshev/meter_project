from django.core.management.base import BaseCommand
from meters import scheduler


class Command(BaseCommand):
    help = 'Run all sync robots in a single process (scheduler)'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true',
                            help='Совместимость с APScheduler (не используется)')
        parser.add_argument('--verbose', action='store_true',
                            help='Совместимость с APScheduler (не используется)')

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('🚀 Scheduler starting...'))
        scheduler.start()