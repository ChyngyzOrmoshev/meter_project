"""Настройки для автотестов: временная SQLite в памяти, боевая MySQL не затрагивается.

Запуск: docker exec meters_django python manage.py test meters --settings=config.settings_test
"""
from .settings import *  # noqa: F401,F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}
