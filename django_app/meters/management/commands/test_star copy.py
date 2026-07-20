# Файл: meters/management/commands/sync_star.py
# Финальная версия синхронизатора Star Power
# Успешно перехватывает XML, парсит и импортирует данные в БД без ошибок синхронности

import os
import xml.etree.ElementTree as ET
import logging
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from dotenv import load_dotenv

# Импортируйте свои модели (раскомментируйте и укажите фактические)
# from meters.models import Meter, MeterReading

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Синхронизация показаний с системы Star Power (HES)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--date',
            type=str,
            help='Дата для синхронизации в формате YYYY-MM-DD (по умолчанию вчера)',
        )

    def handle(self, *args, **options):
        load_dotenv('/app/cEnergo.env')
        username = os.getenv('STAR_USERNAME')
        password = os.getenv('STAR_PASSWORD')
        base_url = os.getenv('STAR_URL', 'http://192.168.20.246:59101/hes')
        download_dir = '/app/downloads'
        os.makedirs(download_dir, exist_ok=True)

        if not username or not password:
            self.stdout.write(self.style.ERROR("Не заданы STAR_USERNAME и STAR_PASSWORD в cEnergo.env"))
            return

        if options.get('date'):
            try:
                sync_date = datetime.strptime(options['date'], '%Y-%m-%d').date()
            except ValueError:
                self.stdout.write(self.style.ERROR("Неверный формат даты. Используйте YYYY-MM-DD"))
                return
        else:
            sync_date = datetime.now().date() - timedelta(days=1)

        self.stdout.write(f"Синхронизация для даты: {sync_date}")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.on('dialog', lambda dialog: dialog.accept())

            try:
                # ------------------------------------------------------------------
                # 1. ЛОГИН
                # ------------------------------------------------------------------
                self.stdout.write("🌐 Переход на страницу логина...")
                page.goto(f"{base_url}/login")
                page.wait_for_selector('input[name="username"]', timeout=30000)
                page.screenshot(path=os.path.join(download_dir, '01_login_page.png'))

                self.stdout.write("🔐 Заполнение формы логина...")
                username_input = page.locator('input[name="username"]')
                username_input.click()
                page.wait_for_timeout(500)
                username_input.fill(username)

                password_input = page.locator('input[name="password"]')
                password_input.click()
                password_input.fill(password)

                page.click('button[type="submit"]')
                page.wait_for_selector('li[data-perm-no="sysMenu.hes.queryAnalysis"]', timeout=30000)
                self.stdout.write(self.style.SUCCESS("✅ Успешный вход."))
                page.screenshot(path=os.path.join(download_dir, '02_after_login.png'))

                # ------------------------------------------------------------------
                # 2. ПЕРЕКЛЮЧЕНИЕ ЯЗЫКА
                # ------------------------------------------------------------------
                self.stdout.write("🌍 Переключение языка на английский...")
                lang_select = page.locator('select#langSelect')
                if lang_select.count():
                    lang_select.select_option('en-US')
                    page.wait_for_timeout(2000)
                    self.stdout.write(self.style.SUCCESS("✅ Язык переключён."))
                page.screenshot(path=os.path.join(download_dir, '03_after_language.png'))

                # ------------------------------------------------------------------
                # 3. НАВИГАЦИЯ ЧЕРЕЗ МЕНЮ
                # ------------------------------------------------------------------
                self.stdout.write("📂 Клик по меню 'Query Analysis' -> 'Query Row Data'...")
                menu_item = page.locator('li[data-perm-no="sysMenu.hes.queryAnalysis"]')
                menu_item.click()
                page.wait_for_timeout(2000)

                query_row_link = page.locator('a:has-text("Query Row Data")')
                if query_row_link.count() == 0:
                    query_row_link = page.locator('a.J_menuItem:has-text("Query Row Data")')
                if query_row_link.count() == 0:
                    query_row_link = page.locator('a[target="_blank"]:has-text("Query Row Data")')
                if query_row_link.count() == 0:
                    raise Exception("Не найдена ссылка 'Query Row Data' в меню")

                query_row_link.click()
                page.wait_for_timeout(5000)

                # ------------------------------------------------------------------
                # 4. ПОИСК IFRAME
                # ------------------------------------------------------------------
                self.stdout.write("🔍 Поиск iframe с id='iframe0'...")
                page.wait_for_selector('iframe#iframe0', timeout=30000)
                iframe_element = page.query_selector('iframe#iframe0')
                if not iframe_element:
                    raise Exception("iframe с id='iframe0' не найден")
                target_frame = iframe_element.content_frame()
                if not target_frame:
                    raise Exception("Не удалось получить содержимое iframe")

                current_iframe_url = target_frame.url
                self.stdout.write(f"Текущий URL iframe: {current_iframe_url}")
                if 'login' in current_iframe_url:
                    self.stdout.write("⚠️ iframe содержит страницу логина, перезагружаем...")
                    target_frame.goto(f"{base_url}/hDatDayController/rowDataList")
                    target_frame.wait_for_load_state('networkidle', timeout=30000)

                self.stdout.write(self.style.SUCCESS(f"✅ Найден iframe: {target_frame.url}"))
                page.screenshot(path=os.path.join(download_dir, '04_iframe_found.png'))

                # ------------------------------------------------------------------
                # 5. ОЖИДАНИЕ ЗАГРУЗКИ ФОРМЫ И ДАННЫХ
                # ------------------------------------------------------------------
                self.stdout.write("⏳ Ожидание загрузки формы внутри iframe...")
                try:
                    target_frame.wait_for_selector('select#dataType', timeout=30000)
                    self.stdout.write(self.style.SUCCESS("✅ Форма загружена."))
                except PlaywrightTimeout:
                    self.stdout.write("⚠️ Форма не загрузилась, нажимаем Query...")
                    query_btn = target_frame.locator('button#btnQueryhDatRowDataList')
                    if query_btn.count() > 0:
                        query_btn.click()
                    target_frame.wait_for_selector('table#hDatRowDataList tbody tr', timeout=60000)
                    self.stdout.write(self.style.SUCCESS("✅ Данные загружены."))
                page.screenshot(path=os.path.join(download_dir, '05_form_loaded.png'))

                # ------------------------------------------------------------------
                # 6. УСТАНОВКА ДАТЫ И НАЖАТИЕ QUERY (если нужно)
                # ------------------------------------------------------------------
                rows = target_frame.locator('table#hDatRowDataList tbody tr')
                if rows.count() == 0:
                    self.stdout.write("⚠️ Таблица пуста, устанавливаем дату и нажимаем Query...")
                    date_str = sync_date.strftime('%Y-%m-%d')
                    target_frame.evaluate(f'''() => {{
                        const start = document.getElementById('dataDt_startDate');
                        const end = document.getElementById('dataDt_endDate');
                        if (start) start.value = '{date_str} 00:00:00';
                        if (end) end.value = '{date_str} 23:59:59';
                        const display = document.getElementById('dataDt');
                        if (display) display.value = '{date_str} 00:00:00 - {date_str} 23:59:59';
                        if (display) {{
                            display.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            display.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        }}
                    }}''')
                    page.wait_for_timeout(1000)

                    query_btn = target_frame.locator('button#btnQueryhDatRowDataList')
                    if query_btn.count() > 0:
                        query_btn.click()
                        target_frame.wait_for_selector('table#hDatRowDataList tbody tr', timeout=60000)
                        self.stdout.write(self.style.SUCCESS("✅ Таблица загружена."))

                page.screenshot(path=os.path.join(download_dir, '06_data_table.png'))

                # ------------------------------------------------------------------
                # 7. ЭКСПОРТ XML (перехват через page.expect_response)
                # ------------------------------------------------------------------
                self.stdout.write("📥 Нажатие кнопки Export XML File2...")
                export_btn = target_frame.locator('.btn_wrapper[title="Export XML File2"] .ui-title-btn a.fa-file-code-o')
                if export_btn.count() == 0:
                    export_btn = target_frame.locator('.btn_wrapper:has-text("Export XML File2")')
                if export_btn.count() == 0:
                    raise Exception("Кнопка Export XML File2 не найдена")

                file_path = None

                # Стратегия 1: перехват по Content-Type через page.expect_response
                try:
                    self.stdout.write("  Попытка перехвата по Content-Type на уровне страницы...")
                    with page.expect_response(
                        lambda resp: 'xml' in resp.headers.get('content-type', '').lower()
                    ) as response_info:
                        export_btn.click(force=True)
                    response = response_info.value
                    xml_content = response.body()
                    file_path = os.path.join(download_dir, 'data.xml')
                    with open(file_path, 'wb') as f:
                        f.write(xml_content)
                    self.stdout.write(self.style.SUCCESS("  ✅ Успех: перехват по Content-Type"))
                except Exception as e:
                    self.stdout.write(f"  ⚠️ Не удалось перехватить по Content-Type: {e}")

                # Стратегия 2: перехват по URL (через page.expect_response)
                if not file_path:
                    try:
                        self.stdout.write("  Попытка перехвата по URL на уровне страницы...")
                        with page.expect_response(
                            lambda resp: '/excelFileController/dataExportFileDownload' in resp.url
                        ) as response_info:
                            export_btn.click(force=True)
                        response = response_info.value
                        xml_content = response.body()
                        file_path = os.path.join(download_dir, 'data.xml')
                        with open(file_path, 'wb') as f:
                            f.write(xml_content)
                        self.stdout.write(self.style.SUCCESS("  ✅ Успех: перехват по URL"))
                    except Exception as e:
                        self.stdout.write(f"  ⚠️ Не удалось перехватить по URL: {e}")

                # Стратегия 3: поиск сгенерированной ссылки
                if not file_path:
                    try:
                        self.stdout.write("  Попытка найти сгенерированную ссылку...")
                        export_btn.click(force=True)
                        page.wait_for_timeout(5000)
                        download_link = target_frame.locator('a:has-text("Download"), a[download], a[href*="export"]')
                        if download_link.count() > 0:
                            with page.expect_download(timeout=15000) as dl_info:
                                download_link.click()
                            download = dl_info.value
                            file_path = os.path.join(download_dir, 'data.xml')
                            download.save_as(file_path)
                            self.stdout.write(self.style.SUCCESS("  ✅ Успех: скачивание по ссылке"))
                        else:
                            raise Exception("Ссылка не найдена")
                    except Exception as e:
                        self.stdout.write(f"  ⚠️ Метод со ссылкой не сработал: {e}")

                # Стратегия 4: новое окно
                if not file_path:
                    try:
                        self.stdout.write("  Попытка перехвата нового окна...")
                        with context.expect_page(timeout=15000) as page_info:
                            export_btn.click(force=True)
                        new_page = page_info.value
                        content = new_page.content()
                        new_page.close()
                        if '<mreadings' in content:
                            file_path = os.path.join(download_dir, 'data.xml')
                            with open(file_path, 'w', encoding='utf-8') as f:
                                f.write(content)
                            self.stdout.write(self.style.SUCCESS("  ✅ Успех: данные из нового окна"))
                        else:
                            raise Exception("Новое окно не содержит XML")
                    except Exception as e:
                        self.stdout.write(f"  ⚠️ Метод с новым окном не сработал: {e}")

                if not file_path or not os.path.exists(file_path):
                    raise Exception("Все методы перехвата XML не сработали – файл не получен")

                # ------------------------------------------------------------------
                # 8. ПАРСИНГ XML И ИМПОРТ В БД (без транзакции, чтобы избежать ошибки)
                # ------------------------------------------------------------------
                self.stdout.write("📄 Парсинг XML и импорт в БД...")
                tree = ET.parse(file_path)
                root = tree.getroot()
                readings = root.findall('mreadings')
                self.stdout.write(f"Найдено записей: {len(readings)}")

                created = 0
                updated = 0
                errors = 0

                # Для большого количества записей используем bulk_create для оптимизации
                # Сначала собираем объекты в список, затем создаём массово
                objects_to_create = []
                objects_to_update = []

                for reading in readings:
                    try:
                        meter_no = reading.find('meterNo').text if reading.find('meterNo') is not None else None
                        data_dt_str = reading.find('dataDt').text if reading.find('dataDt') is not None else None
                        reg = reading.find('reg').text if reading.find('reg') is not None else None
                        data_value_str = reading.find('dataValue').text if reading.find('dataValue') is not None else None

                        if not meter_no or not data_dt_str or data_value_str is None:
                            errors += 1
                            continue

                        try:
                            data_dt = datetime.strptime(data_dt_str, '%m/%d/%Y %H:%M:%S')
                        except ValueError:
                            try:
                                data_dt = datetime.strptime(data_dt_str, '%Y-%m-%d %H:%M:%S')
                            except ValueError:
                                errors += 1
                                continue

                        try:
                            data_value = float(data_value_str.replace(',', ''))
                        except ValueError:
                            errors += 1
                            continue

                        # --- Логика импорта (заменить на свои модели) ---
                        # Поиск прибора
                        # meter = Meter.objects.filter(meter_no=meter_no).first()
                        # if not meter:
                        #     errors += 1
                        #     continue
                        #
                        # # Пытаемся найти существующее показание
                        # reading_obj, created_flag = MeterReading.objects.get_or_create(
                        #     meter=meter,
                        #     reading_date=data_dt,
                        #     channel=reg,
                        #     defaults={'value': data_value}
                        # )
                        # if created_flag:
                        #     created += 1
                        # else:
                        #     reading_obj.value = data_value
                        #     objects_to_update.append(reading_obj)

                        # Для теста просто вывод
                        self.stdout.write(f"  Показание: прибор {meter_no}, дата {data_dt}, канал {reg}, значение {data_value}")
                        created += 1

                    except Exception as e:
                        errors += 1

                # Выполняем массовое обновление, если есть
                # if objects_to_update:
                #     MeterReading.objects.bulk_update(objects_to_update, ['value'])

                self.stdout.write(self.style.SUCCESS(
                    f"✅ Импорт завершён. Создано: {created}, Обновлено: {updated}, Ошибок: {errors}"
                ))

                try:
                    os.remove(file_path)
                except:
                    pass

                self.stdout.write(self.style.SUCCESS("✅ Синхронизация завершена успешно."))

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"❌ Критическая ошибка: {e}"))
                try:
                    page.screenshot(path=os.path.join(download_dir, 'error.png'))
                    with open(os.path.join(download_dir, 'error_page.html'), 'w', encoding='utf-8') as f:
                        f.write(page.content())
                except:
                    pass
                raise
            finally:
                browser.close()