from locust import HttpUser, task, between

class WebsiteUser(HttpUser):
    wait_time = between(1, 5)

    @task(3)
    def load_main_page(self):
        self.client.get("/")

    @task(2)
    def load_devices(self):
        self.client.get("/Device_Registry")

    @task(2)
    def load_readings(self):
        self.client.get("/Readings")

    @task(1)
    def login(self):
        # Пример POST-запроса на авторизацию
        self.client.post("/", data={"username": "user", "password": "123456"})