import os
import unittest

os.environ.setdefault("MONGODB_URL", "mongodb://example.com/apk_cloud_launchpad")
os.environ.setdefault("REMOTE_BUILDER_BASE_URL", "https://example.com")

from portal_app import create_app


class FakePortalStore:
    def __init__(self):
        self.users = {}
        self.sessions = {}
        self.next_user_id = 1

    def create_user(self, full_name, email, password):
        email = email.strip().lower()
        if email in self.users:
            raise RuntimeError("duplicate")
        user = {
            "id": f"user_{self.next_user_id}",
            "fullName": full_name,
            "email": email,
            "initials": "".join([part[:1].upper() for part in full_name.split()[:2]]) or "U",
            "password": password,
        }
        self.next_user_id += 1
        self.users[email] = user
        return {key: value for key, value in user.items() if key != "password"}

    def authenticate_user(self, email, password):
        user = self.users.get(email.strip().lower())
        if not user or user["password"] != password:
            return None
        return {key: value for key, value in user.items() if key != "password"}

    def create_session(self, user_id, ttl_days):
        token = f"token_{user_id}"
        self.sessions[token] = user_id
        return token

    def resolve_session(self, raw_token):
        user_id = self.sessions.get(raw_token)
        if not user_id:
            return None
        for user in self.users.values():
            if user["id"] == user_id:
                return {key: value for key, value in user.items() if key != "password"}
        return None

    def delete_session(self, raw_token):
        self.sessions.pop(raw_token, None)


class FakeProxyClient:
    def fetch_jobs(self):
        return type("Result", (), {"payload": [], "status_code": 200})()

    def forward_json_get(self, path):
        return type("Result", (), {"payload": {}, "status_code": 200})()

    def forward_json_multipart(self, path):
        return type("Result", (), {"payload": {"id": "job-1", "state": "QUEUED", "progress": 5}, "status_code": 202})()

    def forward_stream_get(self, path):
        raise AssertionError("stream download not expected in this test")

    def is_builder_healthy(self):
        return True


class FakeLifecycleService:
    def ensure_builder_ready(self):
        return None

    def observe_job_list(self, payload):
        return None

    def observe_snapshot(self, payload):
        return None

    def cancel_pending_stop(self):
        return None


class AuthRoutesTest(unittest.TestCase):
    def setUp(self):
        self.store = FakePortalStore()
        self.client = create_app(
            portal_store=self.store,
            proxy_client=FakeProxyClient(),
            lifecycle_service=FakeLifecycleService(),
        ).test_client()

    def test_dashboard_redirects_to_login_without_session(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))

    def test_register_creates_session_and_redirects_to_dashboard(self):
        response = self.client.post(
            "/register",
            data={
                "fullName": "Portal Admin",
                "email": "admin@example.com",
                "password": "secret123",
                "confirmPassword": "secret123",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers.get("Location"), "/")
        self.assertIn("portal_session=", response.headers.get("Set-Cookie", ""))

    def test_api_requires_login(self):
        response = self.client.get("/api/builds")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Please sign in", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
