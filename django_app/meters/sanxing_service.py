import os
import time
import requests
import ddddocr
from dotenv import load_dotenv
import logging

logger = logging.getLogger(__name__)

class SanxingService:
    def __init__(self):
        load_dotenv('/app/cEnergo.env')
        self.base_url = os.getenv('SANXING_OLD_URL', 'http://192.168.144.71:8080/epower').rstrip('/')
        self.username = os.getenv('SANXING_OLD_USERNAME')
        self.password = os.getenv('SANXING_OLD_PASSWORD')
        self.session = requests.Session()
        self.ocr = ddddocr.DdddOcr(show_ad=False)
        
        # Настройки заголовков, чтобы сервер думал, что мы браузер
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': f"{self.base_url}/login?locale=en_US"
        }

    def _login(self):
        """Чистый HTTP-логин без Playwright"""
        print("🌐 Логин в Sanxing (HTTP)...")
        
        for attempt in range(1, 6):
            try:
                # 1. Получаем картинку капчи
                captcha_url = f"{self.base_url}/captcha/image"
                # Очищаем старые куки перед новой попыткой
                self.session.cookies.clear()
                
                resp_captcha = self.session.get(captcha_url, headers=self.headers, timeout=10)
                
                # 2. Распознаем капчу
                captcha_text = self.ocr.classification(resp_captcha.content)
                print(f"🤖 Попытка {attempt}/5. Капча: {captcha_text}")
                
                # 3. Отправляем форму логина
                login_url = f"{self.base_url}/login/login"
                payload = {
                    'username': self.username,
                    'password': self.password,
                    'vc': captcha_text
                }
                
                resp_login = self.session.post(login_url, data=payload, headers=self.headers, timeout=10)
                
                # Если в ответе есть ошибка о неверной капче
                if 'validateCodeWrong' in resp_login.text or 'captcha' in resp_login.text.lower():
                    print("⚠️ Неверная капча, пробуем снова...")
                    continue
                
                # Если сервер вернул редирект или успешный вход
                if 'modifypwd' in resp_login.url or 'point2point' in resp_login.url or resp_login.status_code == 200:
                    print("✅ Успешный вход в Sanxing!")
                    return True
                    
            except Exception as e:
                print(f"❌ Сбой при логине: {e}")
                time.sleep(2)
                
        raise Exception("Не удалось войти в Sanxing за 5 попыток.")

    def set_relay(self, serial, action):
        """Отправляет команду на реле через requests"""
        # Если сессии нет, логинимся
        if not self.session.cookies.get('JSESSIONID'):
            self._login()
            
        switch_url = f"{self.base_url}/realtimereading/index/switch"
        
        item_code = '0046000060030AFF01' if action == 'off' else '0046000060030AFF02'
        
        payload = {
            'msno': serial,
            'cmd': '0',
            'item': item_code,
            'optLog': ''
        }
        
        req_headers = {
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': f"{self.base_url}/point2point/index"
        }
        
        # Отправляем
        resp = self.session.post(switch_url, data=payload, headers=req_headers, timeout=20)
        
        # Если сессия протухла (редирект на логин)
        if 'login' in resp.url.lower() or resp.status_code == 401:
            print("🔄 Сессия протухла. Перелогиниваемся...")
            self._login()
            resp = self.session.post(switch_url, data=payload, headers=req_headers, timeout=20)
            
        try:
            res_json = resp.json()
            if res_json.get('errorCode') == 0:
                return 'success'
            else:
                raise Exception(f"Ошибка Sanxing: {res_json.get('errorMsg')}")
        except ValueError:
            raise Exception(f"Сервер вернул не JSON (возможно, страницу логина): {resp.text[:100]}")

    def get_status(self, serial):
        return 'unknown'