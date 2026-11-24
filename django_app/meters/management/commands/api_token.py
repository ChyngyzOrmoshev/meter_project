from django.core.management.base import BaseCommand, CommandError

from meters.api_v1 import create_token
from meters.models import ApiToken


class Command(BaseCommand):
    help = 'Токены внешнего API: create <имя> | list | revoke <id>'

    def add_arguments(self, parser):
        parser.add_argument('action', choices=['create', 'list', 'revoke'])
        parser.add_argument('value', nargs='?', help='имя (create) или id токена (revoke)')

    def handle(self, *args, **opts):
        action, value = opts['action'], opts['value']
        if action == 'create':
            if not value:
                raise CommandError('Укажите, кому выдаётся токен: api_token create "Название"')
            token, key = create_token(value)
            self.stdout.write(f'Токен #{token.id} для «{token.name}». Сохраните ключ, больше он показан не будет:')
            self.stdout.write(key)
        elif action == 'list':
            for t in ApiToken.objects.order_by('id'):
                state = 'действует' if t.is_active else 'отозван'
                used = t.last_used_at.strftime('%Y-%m-%d %H:%M') if t.last_used_at else 'не использовался'
                self.stdout.write(f'#{t.id} {t.name} [{t.prefix}…] {state}, последний запрос: {used}')
        else:
            updated = ApiToken.objects.filter(id=value).update(is_active=False) if value and value.isdigit() else 0
            if not updated:
                raise CommandError('Токен не найден')
            self.stdout.write('Токен отозван')
