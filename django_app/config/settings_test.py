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

# Быстрый хешер: PBKDF2 с миллионом итераций замедляет каждый тест, создающий пользователя
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

# Тесты не должны писать в боевой файловый кэш
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
