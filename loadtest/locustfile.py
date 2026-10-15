"""Surge ~20x normal across all four tiers.  locust -f loadtest/locustfile.py --headless --host http://<ingress>
Point each tier at its Service via ROUTES (env) or edit below."""
import os

from locust import HttpUser, LoadTestShape, between, task

BASE = {"C1": os.getenv("C1_URL", "http://c1-emergency.lifeboat-demo"),
        "C2": os.getenv("C2_URL", "http://c2-portal.lifeboat-demo")}


class Citizen(HttpUser):
    wait_time = between(0.5, 1.5)

    @task(3)
    def emergency(self):
        self.client.get(BASE["C1"] + "/", name="C1")

    @task(2)
    def portal(self):
        self.client.get(BASE["C2"] + "/", name="C2")


class Surge(LoadTestShape):
    """2 min normal (10 users) -> ramp to 20x (200 users) -> hold 5 min -> back."""
    stages = [(120, 10), (180, 200), (480, 200), (600, 10)]

    def tick(self):
        t = self.get_run_time()
        for end, users in self.stages:
            if t < end:
                return users, 50
        return None
