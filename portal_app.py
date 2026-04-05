from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, Optional
from urllib.parse import quote, urlparse

import requests
from flask import Flask, Response, g, jsonify, redirect, render_template, request, stream_with_context, url_for
from pymongo import ASCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "src" / "main" / "resources" / "public"
TEMPLATES_DIR = BASE_DIR / "templates"
BUILDER_TOKEN_HEADER = "X-Builder-Token"
COOKIE_NAME_DEFAULT = "portal_session"
ACTIVE_JOB_STATES = {"QUEUED", "PREPARING", "BUILDING"}
TERMINAL_JOB_STATES = {"COMPLETED", "FAILED"}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class BuilderUnavailableError(RuntimeError):
    pass


class AuthenticationRequiredError(RuntimeError):
    pass


class PortalStoreError(RuntimeError):
    pass


class RateLimitExceededError(RuntimeError):
    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass
class JsonProxyResult:
    payload: Any
    status_code: int


@dataclass
class HerokuWebConfig:
    service_root: Path
    port: int
    mongo_url: str
    session_cookie_name: str
    session_ttl_days: int
    remote_builder_base_url: str
    remote_builder_health_path: str
    remote_builder_token: Optional[str]
    github_access_token: Optional[str]
    github_codespace_name: Optional[str]
    github_api_base_url: str
    github_api_version: str
    builder_request_timeout_seconds: int
    codespace_start_timeout_seconds: int
    codespace_wake_cooldown_seconds: int
    codespace_idle_shutdown_seconds: int
    codespace_auto_stop_enabled: bool
    api_rate_limit_max_requests: int
    api_rate_limit_window_seconds: int

    @classmethod
    def from_environment(cls, service_root: Path) -> "HerokuWebConfig":
        return cls(
            service_root=service_root.resolve(),
            port=parse_positive_int("PORT", parse_positive_int("WEB_PORT", 8080)),
            mongo_url=require_env("MONGODB_URL"),
            session_cookie_name=first_non_blank(os.getenv("SESSION_COOKIE_NAME")) or COOKIE_NAME_DEFAULT,
            session_ttl_days=parse_positive_int("SESSION_TTL_DAYS", 30),
            remote_builder_base_url=require_env("REMOTE_BUILDER_BASE_URL").rstrip("/"),
            remote_builder_health_path=normalize_path(first_non_blank(os.getenv("REMOTE_BUILDER_HEALTH_PATH")) or "/health"),
            remote_builder_token=first_non_blank(os.getenv("REMOTE_BUILDER_TOKEN")),
            github_access_token=first_non_blank(os.getenv("GITHUB_ACCESS_TOKEN"), os.getenv("GITHUB_TOKEN")),
            github_codespace_name=first_non_blank(os.getenv("GITHUB_CODESPACE_NAME")),
            github_api_base_url=first_non_blank(os.getenv("GITHUB_API_BASE_URL")) or "https://api.github.com",
            github_api_version=first_non_blank(os.getenv("GITHUB_API_VERSION")) or "2022-11-28",
            builder_request_timeout_seconds=parse_positive_int("BUILDER_REQUEST_TIMEOUT_SECONDS", 900),
            codespace_start_timeout_seconds=parse_positive_int("CODESPACE_START_TIMEOUT_SECONDS", 180),
            codespace_wake_cooldown_seconds=parse_non_negative_int("CODESPACE_WAKE_COOLDOWN_SECONDS", 15),
            codespace_idle_shutdown_seconds=parse_non_negative_int("CODESPACE_IDLE_SHUTDOWN_SECONDS", 90),
            codespace_auto_stop_enabled=parse_bool_env("CODESPACE_AUTO_STOP_ENABLED", True),
            api_rate_limit_max_requests=parse_positive_int("API_RATE_LIMIT_MAX_REQUESTS", 60),
            api_rate_limit_window_seconds=parse_positive_int("API_RATE_LIMIT_WINDOW_SECONDS", 60),
        )

    def wake_enabled(self) -> bool:
        return bool(self.github_access_token and self.github_codespace_name)

    def auto_stop_enabled(self) -> bool:
        return self.wake_enabled() and self.codespace_auto_stop_enabled


class ApiRateLimiter:
    def __init__(self, max_requests: int, window_seconds: int, now_millis=None) -> None:
        self.max_requests = max_requests
        self.window_millis = window_seconds * 1000
        self.now_millis = now_millis or (lambda: int(time.time() * 1000))
        self._buckets: Dict[str, Deque[int]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = self.now_millis()
        with self._lock:
            bucket = self._buckets[key]
            while bucket and now - bucket[0] >= self.window_millis:
                bucket.popleft()

            if len(bucket) >= self.max_requests:
                oldest = bucket[0]
                retry_after_seconds = max(1, (self.window_millis - (now - oldest) + 999) // 1000)
                raise RateLimitExceededError(
                    "Rate limit exceed ho gaya. Thodi der baad dobara try karo.",
                    int(retry_after_seconds),
                )

            bucket.append(now)


class MongoPortalStore:
    def __init__(self, mongo_url: str) -> None:
        self.client = MongoClient(mongo_url, connect=False)
        self.db = self.client.get_default_database("apk_cloud_launchpad")
        self.users = self.db["users"]
        self.sessions = self.db["sessions"]
        self._indexes_ready = False
        self._lock = threading.Lock()

    def create_user(self, full_name: str, email: str, password: str) -> Dict[str, str]:
        self._ensure_indexes()
        normalized_email = normalize_email(email)
        now = utcnow()
        document = {
            "_id": f"user_{secrets.token_hex(12)}",
            "fullName": full_name,
            "email": normalized_email,
            "emailLower": normalized_email.lower(),
            "passwordHash": generate_password_hash(password, method="scrypt"),
            "createdAt": now,
            "updatedAt": now,
        }

        try:
            self.users.insert_one(document)
        except DuplicateKeyError as error:
            raise PortalStoreError("Is email se account pehle se bana hua hai.") from error
        except PyMongoError as error:
            raise PortalStoreError("MongoDB me account save nahi ho paya.") from error

        return self._public_user(document)

    def authenticate_user(self, email: str, password: str) -> Optional[Dict[str, str]]:
        self._ensure_indexes()
        try:
            document = self.users.find_one({"emailLower": normalize_email(email).lower()})
        except PyMongoError as error:
            raise PortalStoreError("MongoDB se user lookup nahi ho paya.") from error

        if not document or not check_password_hash(document["passwordHash"], password):
            return None

        try:
            self.users.update_one({"_id": document["_id"]}, {"$set": {"updatedAt": utcnow()}})
        except PyMongoError:
            pass

        return self._public_user(document)

    def create_session(self, user_id: str, ttl_days: int) -> str:
        self._ensure_indexes()
        raw_token = secrets.token_urlsafe(32)
        now = utcnow()
        session_document = {
            "_id": f"session_{secrets.token_hex(12)}",
            "userId": user_id,
            "tokenHash": hash_session_token(raw_token),
            "createdAt": now,
            "lastSeenAt": now,
            "expiresAt": now + timedelta(days=max(1, ttl_days)),
        }

        try:
            self.sessions.insert_one(session_document)
        except PyMongoError as error:
            raise PortalStoreError("User session create nahi ho paayi.") from error

        return raw_token

    def resolve_session(self, raw_token: str) -> Optional[Dict[str, str]]:
        self._ensure_indexes()
        token_hash = hash_session_token(raw_token)
        now = utcnow()

        try:
            session_document = self.sessions.find_one({"tokenHash": token_hash, "expiresAt": {"$gt": now}})
        except PyMongoError as error:
            raise PortalStoreError("MongoDB se session read nahi ho paayi.") from error

        if not session_document:
            return None

        try:
            user_document = self.users.find_one({"_id": session_document["userId"]})
        except PyMongoError as error:
            raise PortalStoreError("MongoDB se logged-in user load nahi ho paya.") from error

        if not user_document:
            self.delete_session(raw_token)
            return None

        last_seen_at = session_document.get("lastSeenAt")
        if not isinstance(last_seen_at, datetime) or now - last_seen_at >= timedelta(minutes=10):
            try:
                self.sessions.update_one({"_id": session_document["_id"]}, {"$set": {"lastSeenAt": now}})
            except PyMongoError:
                pass

        return self._public_user(user_document)

    def delete_session(self, raw_token: str) -> None:
        self._ensure_indexes()
        try:
            self.sessions.delete_many({"tokenHash": hash_session_token(raw_token)})
        except PyMongoError as error:
            raise PortalStoreError("User session delete nahi ho paayi.") from error

    def _ensure_indexes(self) -> None:
        if self._indexes_ready:
            return

        with self._lock:
            if self._indexes_ready:
                return
            try:
                self.users.create_index([("emailLower", ASCENDING)], unique=True, name="uniq_users_email_lower")
                self.sessions.create_index([("tokenHash", ASCENDING)], unique=True, name="uniq_sessions_token_hash")
                self.sessions.create_index([("expiresAt", ASCENDING)], expireAfterSeconds=0, name="ttl_sessions_expires_at")
            except PyMongoError as error:
                raise PortalStoreError("MongoDB indexes create nahi ho paaye.") from error
            self._indexes_ready = True

    @staticmethod
    def _public_user(document: Dict[str, Any]) -> Dict[str, str]:
        full_name = str(document.get("fullName") or "").strip()
        return {
            "id": str(document.get("_id") or ""),
            "fullName": full_name,
            "email": str(document.get("email") or "").strip(),
            "initials": initials_from_name(full_name),
        }


class BuilderProxyClient:
    def __init__(self, config: HerokuWebConfig) -> None:
        self.config = config
        self.session = requests.Session()

    def is_builder_healthy(self) -> bool:
        try:
            response = self.session.get(
                self._builder_url(self.config.remote_builder_health_path),
                headers=self._builder_headers({"Accept": "application/json"}),
                timeout=20,
            )
            response.close()
            return 200 <= response.status_code < 300
        except requests.RequestException:
            return False

    def fetch_jobs(self) -> JsonProxyResult:
        return self.forward_json_get("/api/builds")

    def forward_json_get(self, path: str) -> JsonProxyResult:
        return self._execute_json("GET", path, headers={"Accept": "application/json"})

    def forward_json_multipart(self, path: str) -> JsonProxyResult:
        data = []
        for name in request.form:
            for value in request.form.getlist(name):
                data.append((name, value))

        files = []
        for storage in request.files.getlist("iconFile"):
            if storage and storage.filename:
                files.append(
                    (
                        "iconFile",
                        (storage.filename, storage.stream, storage.mimetype or "application/octet-stream"),
                    )
                )

        return self._execute_json("POST", path, headers={"Accept": "application/json"}, data=data, files=files)

    def forward_stream_get(self, path: str) -> requests.Response:
        return self._execute("GET", path, headers={"Accept": "*/*"}, stream=True)

    def _execute_json(self, method: str, path: str, headers=None, data=None, files=None) -> JsonProxyResult:
        response = self._execute(method, path, headers=headers, data=data, files=files, stream=False)
        try:
            if response.content:
                payload = response.json()
            else:
                payload = {}
        except ValueError:
            payload = {"message": response.text[:500] or "Builder se valid JSON response nahi aayi."}
        finally:
            response.close()

        return JsonProxyResult(payload=payload, status_code=response.status_code)

    def _execute(self, method: str, path: str, headers=None, data=None, files=None, stream: bool = True) -> requests.Response:
        try:
            return self.session.request(
                method=method,
                url=self._builder_url(path),
                headers=self._builder_headers(headers),
                timeout=self.config.builder_request_timeout_seconds,
                data=data,
                files=files,
                stream=stream,
            )
        except requests.RequestException as error:
            raise BuilderUnavailableError(
                "Remote builder se connect nahi ho paya. API URL aur Codespaces port forwarding check karo."
            ) from error

    def _builder_headers(self, extra_headers=None) -> Dict[str, str]:
        headers = dict(extra_headers or {})
        if self.config.remote_builder_token:
            headers[BUILDER_TOKEN_HEADER] = self.config.remote_builder_token
        return headers

    def _builder_url(self, path: str) -> str:
        normalized = path if path.startswith("/") else f"/{path}"
        return f"{self.config.remote_builder_base_url}{normalized}"


class CodespaceLifecycleService:
    def __init__(self, config: HerokuWebConfig, proxy_client: BuilderProxyClient) -> None:
        self.config = config
        self.proxy_client = proxy_client
        self.session = requests.Session()
        self._last_wake_attempt_at = 0.0
        self._stop_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def ensure_builder_ready(self) -> None:
        self.cancel_pending_stop()
        if self.proxy_client.is_builder_healthy():
            return

        with self._lock:
            if self.proxy_client.is_builder_healthy():
                return

            if not self.config.wake_enabled():
                raise BuilderUnavailableError(
                    "Builder abhi reachable nahi hai. Optional auto-wake ke liye GITHUB_ACCESS_TOKEN aur GITHUB_CODESPACE_NAME set karo."
                )

            self._maybe_start_codespace()
            self._wait_for_builder()

    def observe_job_list(self, payload: Any) -> None:
        if payload_has_active_jobs(payload):
            self.cancel_pending_stop()
            return
        self.schedule_idle_stop()

    def observe_snapshot(self, payload: Any) -> None:
        state = snapshot_state(payload)
        if state in ACTIVE_JOB_STATES:
            self.cancel_pending_stop()
            return

        if state in TERMINAL_JOB_STATES:
            try:
                jobs_result = self.proxy_client.fetch_jobs()
            except BuilderUnavailableError:
                return
            self.observe_job_list(jobs_result.payload)

    def schedule_idle_stop(self) -> None:
        if not self.config.auto_stop_enabled():
            return

        delay = self.config.codespace_idle_shutdown_seconds
        with self._lock:
            if self._stop_timer is not None:
                return

            timer = threading.Timer(delay, self._stop_codespace_if_idle)
            timer.daemon = True
            self._stop_timer = timer
            timer.start()

    def cancel_pending_stop(self) -> None:
        with self._lock:
            if self._stop_timer is not None:
                self._stop_timer.cancel()
                self._stop_timer = None

    def _maybe_start_codespace(self) -> None:
        now = time.time()
        if now - self._last_wake_attempt_at < self.config.codespace_wake_cooldown_seconds:
            return

        response = self._github_codespace_request("start")
        if not 200 <= response.status_code < 300:
            raise BuilderUnavailableError(
                f"GitHub Codespace start request fail ho gaya ({response.status_code}). Response: {response.text[:300]}"
            )

        self._last_wake_attempt_at = now

    def _wait_for_builder(self) -> None:
        deadline = time.time() + self.config.codespace_start_timeout_seconds
        while time.time() < deadline:
            if self.proxy_client.is_builder_healthy():
                return
            time.sleep(3)

        raise BuilderUnavailableError(
            f"Codespace wake request bhej di, lekin builder {self.config.codespace_start_timeout_seconds} seconds ke andar ready nahi hua."
        )

    def _stop_codespace_if_idle(self) -> None:
        with self._lock:
            self._stop_timer = None

        try:
            jobs_result = self.proxy_client.fetch_jobs()
        except BuilderUnavailableError:
            return

        if payload_has_active_jobs(jobs_result.payload):
            return

        if not self.config.auto_stop_enabled():
            return

        try:
            response = self._github_codespace_request("stop")
        except BuilderUnavailableError:
            return

        if not 200 <= response.status_code < 300:
            print(f"Codespace stop request failed: {response.status_code} {response.text[:240]}")

    def _github_codespace_request(self, action: str) -> requests.Response:
        if not self.config.github_codespace_name:
            raise BuilderUnavailableError("GITHUB_CODESPACE_NAME missing hai.")
        if not self.config.github_access_token:
            raise BuilderUnavailableError("GITHUB_ACCESS_TOKEN missing hai.")

        url = f"{self.config.github_api_base_url}/user/codespaces/{quote(self.config.github_codespace_name, safe='')}/{action}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.config.github_access_token}",
            "X-GitHub-Api-Version": self.config.github_api_version,
            "User-Agent": "apk-cloud-launchpad",
        }

        try:
            return self.session.post(url, headers=headers, timeout=30)
        except requests.RequestException as error:
            raise BuilderUnavailableError(f"GitHub Codespace {action} request fail ho gayi.") from error


def create_app(
    config: Optional[HerokuWebConfig] = None,
    portal_store: Optional[MongoPortalStore] = None,
    proxy_client: Optional[BuilderProxyClient] = None,
    lifecycle_service: Optional[CodespaceLifecycleService] = None,
) -> Flask:
    load_env_file(BASE_DIR / ".env")
    config = config or HerokuWebConfig.from_environment(BASE_DIR)
    portal_store = portal_store or MongoPortalStore(config.mongo_url)
    proxy_client = proxy_client or BuilderProxyClient(config)
    lifecycle_service = lifecycle_service or CodespaceLifecycleService(config, proxy_client)
    rate_limiter = ApiRateLimiter(config.api_rate_limit_max_requests, config.api_rate_limit_window_seconds)

    app = Flask(__name__, static_folder=str(PUBLIC_DIR), static_url_path="", template_folder=str(TEMPLATES_DIR))
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[assignment]

    @app.before_request
    def load_request_context() -> Optional[Response]:
        g.current_user = None
        g.clear_auth_cookie = False

        if request.endpoint == "static":
            return None

        session_token = request.cookies.get(config.session_cookie_name)
        if session_token:
            user = portal_store.resolve_session(session_token)
            if user is None:
                g.clear_auth_cookie = True
            else:
                g.current_user = user

        if request.path.startswith("/api/"):
            rate_limiter.check(resolve_client_key())

        return None

    @app.after_request
    def apply_security_headers(response: Response) -> Response:
        if getattr(g, "clear_auth_cookie", False):
            response.delete_cookie(config.session_cookie_name, path="/")

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Origin-Agent-Cluster"] = "?1"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob: https:; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        )

        if request.path.startswith("/api/") or response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.context_processor
    def inject_current_user():
        return {"current_user": g.get("current_user")}

    @app.errorhandler(AuthenticationRequiredError)
    def handle_auth_required(error: AuthenticationRequiredError):
        if request.path.startswith("/api/"):
            return jsonify({"message": str(error)}), 401
        return redirect(url_for("login", next=request.path))

    @app.errorhandler(PortalStoreError)
    def handle_portal_store_error(error: PortalStoreError):
        if request.path.startswith("/api/"):
            return jsonify({"message": str(error)}), 503
        return render_template("error.html", message=str(error), title="Database Error"), 503

    @app.errorhandler(BuilderUnavailableError)
    def handle_builder_unavailable(error: BuilderUnavailableError):
        return jsonify({"message": str(error)}), 503

    @app.errorhandler(RateLimitExceededError)
    def handle_rate_limit(error: RateLimitExceededError):
        response = jsonify({"message": str(error)})
        response.status_code = 429
        response.headers["Retry-After"] = str(error.retry_after_seconds)
        return response

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        if isinstance(error, HTTPException):
            return error

        if request.path.startswith("/api/"):
            return jsonify({"message": str(error) or "Unexpected server error"}), 500

        return render_template("error.html", message=str(error) or "Unexpected server error", title="Unexpected Error"), 500

    @app.get("/")
    def dashboard():
        user = require_authenticated_user()
        return render_template(
            "dashboard.html",
            title="Build Dashboard",
            user=user,
            codespace_enabled=config.wake_enabled(),
        )

    @app.get("/index.html")
    def legacy_index():
        return redirect(url_for("dashboard"), code=302)

    @app.get("/login")
    def login():
        if g.get("current_user"):
            return redirect(url_for("dashboard"))
        return render_template("login.html", title="Login", form_values={}, error=None, next_target=safe_next_target(request.args.get("next")))

    @app.post("/login")
    def login_submit():
        if g.get("current_user"):
            return redirect(url_for("dashboard"))

        form_values = {"email": request.form.get("email", "").strip()}
        next_target = safe_next_target(request.form.get("next"))
        password = request.form.get("password", "")
        user = portal_store.authenticate_user(form_values["email"], password)
        if user is None:
            return render_template(
                "login.html",
                title="Login",
                form_values=form_values,
                error="Email ya password sahi nahi hai.",
                next_target=next_target,
            ), 400

        response = redirect(next_target or url_for("dashboard"))
        issue_session_cookie(response, config, portal_store.create_session(user["id"], config.session_ttl_days))
        return response

    @app.get("/register")
    def register():
        if g.get("current_user"):
            return redirect(url_for("dashboard"))
        return render_template("register.html", title="Register", form_values={}, error=None, next_target=safe_next_target(request.args.get("next")))

    @app.post("/register")
    def register_submit():
        if g.get("current_user"):
            return redirect(url_for("dashboard"))

        full_name = request.form.get("fullName", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirmPassword", "")
        next_target = safe_next_target(request.form.get("next"))
        form_values = {"fullName": full_name, "email": email}

        validation_error = validate_registration_form(full_name, email, password, confirm_password)
        if validation_error:
            return render_template(
                "register.html",
                title="Register",
                form_values=form_values,
                error=validation_error,
                next_target=next_target,
            ), 400

        try:
            user = portal_store.create_user(full_name, email, password)
        except PortalStoreError as error:
            if "pehle se" in str(error):
                return render_template(
                    "register.html",
                    title="Register",
                    form_values=form_values,
                    error=str(error),
                    next_target=next_target,
                ), 400
            raise

        response = redirect(next_target or url_for("dashboard"))
        issue_session_cookie(response, config, portal_store.create_session(user["id"], config.session_ttl_days))
        return response

    @app.post("/logout")
    def logout():
        session_token = request.cookies.get(config.session_cookie_name)
        if session_token:
            portal_store.delete_session(session_token)
        response = redirect(url_for("login"))
        response.delete_cookie(config.session_cookie_name, path="/")
        return response

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/favicon.ico")
    def favicon():
        return Response(status=204)

    @app.get("/api/builds")
    def list_builds():
        require_authenticated_user()
        lifecycle_service.ensure_builder_ready()
        result = proxy_client.fetch_jobs()
        lifecycle_service.observe_job_list(result.payload)
        return jsonify(result.payload), result.status_code

    @app.get("/api/builds/<job_id>")
    def get_build(job_id: str):
        require_authenticated_user()
        lifecycle_service.ensure_builder_ready()
        result = proxy_client.forward_json_get(f"/api/builds/{job_id}")
        lifecycle_service.observe_snapshot(result.payload)
        return jsonify(result.payload), result.status_code

    @app.post("/api/builds")
    def create_build():
        require_authenticated_user()
        lifecycle_service.ensure_builder_ready()
        result = proxy_client.forward_json_multipart("/api/builds")
        lifecycle_service.observe_snapshot(result.payload)
        return jsonify(result.payload), result.status_code

    @app.get("/api/builds/<job_id>/apk")
    def download_apk(job_id: str):
        require_authenticated_user()
        lifecycle_service.cancel_pending_stop()
        lifecycle_service.ensure_builder_ready()
        return proxy_response(proxy_client.forward_stream_get(f"/api/builds/{job_id}/apk"))

    return app


def proxy_response(remote_response: requests.Response) -> Response:
    headers = {}
    for header_name in ("Content-Type", "Content-Disposition"):
        header_value = remote_response.headers.get(header_name)
        if header_value:
            headers[header_name] = header_value

    def generate() -> Iterable[bytes]:
        try:
            for chunk in remote_response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            remote_response.close()

    return Response(
        stream_with_context(generate()),
        status=remote_response.status_code,
        headers=headers,
        direct_passthrough=True,
    )


def require_authenticated_user() -> Dict[str, str]:
    user = g.get("current_user")
    if not user:
        raise AuthenticationRequiredError("Please sign in to continue.")
    return user


def issue_session_cookie(response: Response, config: HerokuWebConfig, raw_token: str) -> None:
    g.clear_auth_cookie = False
    response.set_cookie(
        config.session_cookie_name,
        raw_token,
        max_age=max(1, config.session_ttl_days) * 24 * 60 * 60,
        httponly=True,
        secure=request.is_secure or bool(os.getenv("DYNO")),
        samesite="Lax",
        path="/",
    )


def resolve_client_key() -> str:
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        client_ip = forwarded_for.split(",", 1)[0].strip()
        if client_ip:
            return client_ip
    return request.remote_addr or "unknown"


def validate_registration_form(full_name: str, email: str, password: str, confirm_password: str) -> Optional[str]:
    if len(full_name) < 2 or len(full_name) > 80:
        return "Full name 2 se 80 characters ke beech hona chahiye."
    if not EMAIL_PATTERN.match(normalize_email(email)):
        return "Valid email address do."
    if len(password) < 8:
        return "Password kam se kam 8 characters ka hona chahiye."
    if password != confirm_password:
        return "Password aur confirm password match nahi kar rahe."
    return None


def payload_has_active_jobs(payload: Any) -> bool:
    if isinstance(payload, list):
        return any(snapshot_state(item) in ACTIVE_JOB_STATES for item in payload)
    return snapshot_state(payload) in ACTIVE_JOB_STATES


def snapshot_state(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("state") or "").upper()
    return ""


def initials_from_name(value: str) -> str:
    parts = [segment[:1].upper() for segment in value.split() if segment.strip()]
    if not parts:
        return "U"
    return "".join(parts[:2])


def normalize_email(value: str) -> str:
    return value.strip().lower()


def safe_next_target(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    return value


def hash_session_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.utcnow()


def load_env_file(env_path: Path) -> None:
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def first_non_blank(*values: Optional[str]) -> Optional[str]:
    for value in values:
        if value and value.strip():
            return value.strip()
    return None


def normalize_path(value: str) -> str:
    return value if value.startswith("/") else f"/{value}"


def require_env(name: str) -> str:
    value = first_non_blank(os.getenv(name))
    if not value:
        raise RuntimeError(f"{name} environment variable required hai.")
    return value


def parse_positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value and raw_value.strip():
        try:
            return max(1, int(raw_value.strip()))
        except ValueError:
            return default
    return default


def parse_non_negative_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value and raw_value.strip():
        try:
            return max(0, int(raw_value.strip()))
        except ValueError:
            return default
    return default


def parse_bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if not raw_value or not raw_value.strip():
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8090")))
