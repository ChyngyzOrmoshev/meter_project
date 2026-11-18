"""
Планировщик синхронизаций.

ВСЕ быстрые роботы идут ЦЕПОЧКОЙ раз в 30 минут — строго последовательно.
Пока один не закончит, следующий не начнётся.
Playwright-роботы — каждый в своём слоте, чтобы не мешали друг другу:
    Star        03:00 / 15:00
    Hexing POP  03:30 / 15:30
    Sanxing_new_100A  04:00 / 16:00
    Sanxing_new_5A    04:30 / 16:30
"""
import logging
import time

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from django.core.management import call_command
from django.db import close_old_connections
from django.utils import timezone

# Отключаем спам от APScheduler о пропущенных тиках (skipped)
logging.getLogger('apscheduler.executors.default').setLevel(logging.WARNING)
logging.getLogger('apscheduler.scheduler').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def run_command(*args, **kwargs):
    close_old_connections()
    label = ' '.join(str(a) for a in args) if args else 'unknown'
    started = time.time()
    logger.info(f'▶️ {label} — start at {timezone.now()}')
    try:
        call_command(*args, **kwargs)
        logger.info(f'✅ {label} — done in {time.time() - started:.1f}s')
    except Exception as e:
        logger.exception(f'❌ {label} — failed after {time.time() - started:.1f}s: {e}')


# Порядок важен: сначала лёгкие, потом тяжёлые
CHAIN = [
    'sync_risesun_api',  # 3 сек
    'sync_inhemeter',
    'sync_mssql',        # 45 сек (cEnergo)
    'sync_hexing',       # 12 сек (Hexing KUK)
    'sync_sanrise',      # 90 сек (SunRise)
    'sync_sanxing',      # 30–60 сек (Sanxing_old, теперь bulk)
]


def run_chain():
    logger.info('=' * 60)
    logger.info('🔁 Chain START')
    t0 = time.time()
    for cmd in CHAIN:
        run_command(cmd, '--once')
    logger.info(f'🔁 Chain DONE in {time.time() - t0:.1f}s')
    logger.info('=' * 60)


def start():
    sched = BlockingScheduler(timezone='Asia/Bishkek')

    # Цепочка — раз в 30 минут, без наложений
    sched.add_job(
        run_chain,
        IntervalTrigger(minutes=30),
        id='fast_chain',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=900,
    )

    # Автозакрытие заявок — каждый час в :10 (между цепочками)
    sched.add_job(
        run_command,
        CronTrigger(minute='10'),
        args=['auto_close_issues'],
        id='auto_close_issues',
        max_instances=1,
        coalesce=True,
    )

    # Проверка оплаты долгов (раз в час в :40 минут)
    sched.add_job(
        run_command,
        IntervalTrigger(minutes=10),
        # CronTrigger(seconds=30),
        args=['check_debts_auto_on'],
        id='check_debts_auto_on',
        max_instances=1,
        coalesce=True,
    )

    # Playwright-роботы — каждый в своём слоте, чтобы не мешали друг другу
    # (раньше все 4 стартовали в 03:00 / 15:00 одновременно и роняли Sanxing_new_100A)
    PLAYWRIGHT_JOBS = [
        # (команда,               часы,        минута)
        ('sync_star',            '3,15',      '0'),   # 03:00 / 15:00
        ('sync_hxn',             '3,15',      '30'),  # 03:30 / 15:30
        ('sync_website',         '4,16',      '0'),   # 04:00 / 16:00
        ('sync_website_period1', '4,16',      '30'),  # 04:30 / 16:30
    ]
    for cmd, hour, minute in PLAYWRIGHT_JOBS:
        sched.add_job(
            run_command,
            CronTrigger(hour=hour, minute=minute),
            args=[cmd, '--once', '--verbose'],
            id=cmd,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=1800,
        )


    # Воркер обработки очереди реле (каждые 5 секунд)
    sched.add_job(
        run_command,
        IntervalTrigger(seconds=60),        # Ждем 5 секунд
        args=['process_relay_tasks', '--once'],  # ОБЯЗАТЕЛЬНО передаем --once!
        id='process_relay_tasks',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    
    logger.info('🚀 Scheduler started with %d jobs', len(sched.get_jobs()))
    for job in sched.get_jobs():
        logger.info(f'   • {job.id:25s} → {job.trigger}')
    sched.start()

    