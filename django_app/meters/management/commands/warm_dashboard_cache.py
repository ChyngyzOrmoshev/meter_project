"""Пересчёт статистики дашборда в общий кэш (чтобы страница открывалась мгновенно)."""
import time

from django.core.management.base import BaseCommand

from meters.dashboard_stats import PERIODS, store_period_stats


class Command(BaseCommand):
    help = 'Пересчитывает статистику дашборда и районов: по умолчанию «сегодня», с --all ещё 3d/7d/30d'

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true')

    def handle(self, *args, **options):
        periods = list(PERIODS) if options['all'] else ['today']
        for period in periods:
            started = time.time()
            store_period_stats(period)
            self.stdout.write(f'{period}: {time.time() - started:.1f}с')
