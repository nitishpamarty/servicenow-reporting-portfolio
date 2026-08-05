import os
import threading
import time

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


class SnowClient:
    def __init__(self, min_interval=0.05):
        base_url = os.environ["SERVICENOW_URL"].rstrip("/")
        user = os.environ["SERVICENOW_USER"]
        password = os.environ["SERVICENOW_PASSWORD"]

        self.base = base_url
        self.session = requests.Session()
        self.session.auth = (user, password)
        self.session.headers.update(
            {"Content-Type": "application/json", "Accept": "application/json"}
        )
        self.min_interval = min_interval
        self._last_call = 0.0
        self._lock = threading.Lock()

    def _throttle(self):
        with self._lock:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_call = time.monotonic()

    def _request(self, method, path, **kwargs):
        url = f"{self.base}{path}"
        last_exc = None
        resp = None
        for attempt in range(6):
            self._throttle()
            try:
                resp = self.session.request(method, url, timeout=30, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(min(2**attempt, 20))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(min(2**attempt, 20))
                continue
            return resp
        if resp is not None:
            return resp
        raise last_exc

    def get(self, table, params=None, fields=None, limit=None, query=None):
        p = dict(params or {})
        if fields:
            p["sysparm_fields"] = fields
        if limit:
            p["sysparm_limit"] = limit
        if query:
            p["sysparm_query"] = query
        resp = self._request("GET", f"/api/now/table/{table}", params=p)
        resp.raise_for_status()
        return resp.json()["result"]

    def post(self, table, body):
        resp = self._request("POST", f"/api/now/table/{table}", json=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"POST {table} failed [{resp.status_code}]: {resp.text[:500]} body={body}")
        return resp.json()["result"]

    def patch(self, table, sys_id, body):
        resp = self._request("PATCH", f"/api/now/table/{table}/{sys_id}", json=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"PATCH {table}/{sys_id} failed [{resp.status_code}]: {resp.text[:500]}")
        return resp.json()["result"]
