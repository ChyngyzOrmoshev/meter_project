from django.core.management.base import BaseCommand
from django.contrib.auth.models import User, Group

class Command(BaseCommand):
    help = 'Создание инженеров АСКУЭ'

    def handle(self, *args, **options):
        group_name = 'Инженер АСКУЭ'
        group, created = Group.objects.get_or_create(name=group_name)
        
        if created:
            self.stdout.write(self.style.SUCCESS(f'Группа "{group_name}" создана.'))
            
        users_data = [
            ("begmatova", "begmatova2026", "Бегматова Айгуль", "Кок-Арт РЭС"),
            ("askarov", "askarov2026", "Аскаров Руслан", "Сузак ЭС"),
            ("kazatova", "kazatova2026", "Казатова Таттыбубу", "Базар-Коргон РЭС"),
            ("kochkorbaikyzy", "kochkorbaikyzy2026", "Кочкорбай кызы Зыягул", "Ноокен РЭС"),
            ("kimsanova", "kimsanova2026", "Кимсанова Роза", "Майлуу-Суу РЭС"),
            ("tajibaev", "tajibaev2026", "Тажибаев Нургазы", "Таш-Комур РЭС"),
            ("kalmatova", "kalmatova2026", "Калматова Айгул", "Аксы РЭС"),
            ("inamov", "inamov2026", "Инамов Надыржан", "Ала-Бука РЭС"),
            ("akmoldoev", "akmoldoev2026", "Акмолдоев Элдияр", "Чаткал РЭС"),
            ("sydykova", "sydykova2026", "Сыдыкова Назгуль", "Кара-Куль РЭС"),
            ("arykbaeva", "arykbaeva2026", "Арыкбаева Саламат", "Токтогул РЭС"),
            ("kalybek", "kalybek2026", "Калыбек уулу Азамат", "Манас ЭС"),
        ]

        for username, password, full_name, region in users_data:
            user, created = User.objects.get_or_create(username=username)
            if created:
                # Разбиваем ФИО на Имя и Фамилию для красоты
                parts = full_name.split()
                user.last_name = parts[0] if parts else ''
                user.first_name = ' '.join(parts[1:]) if len(parts) > 1 else ''
                
                user.set_password(password)
                user.is_active = True
                user.save()
                
                # Добавляем в группу
                user.groups.add(group)
                self.stdout.write(self.style.SUCCESS(f'✅ Создан: {username} ({full_name})'))
            else:
                self.stdout.write(self.style.WARNING(f'ℹ️ Уже существует: {username}'))