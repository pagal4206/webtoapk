from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, Iterable, Optional
from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, redirect, request, stream_with_context
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix


BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "src" / "main" / "resources" / "public"
BUILDER_TOKEN_HEADER = "X-Builder-Token"


class BuilderUnavailableError(RuntimeError):
    pass


class RateLimitExceededError(RuntimeError):
    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass
class HerokuWebConfig:
    service_root: Path
    port: int
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
    api_rate_limit_max_requests: int
    api_rate_limit_window_seconds: int

    @classmethod
    def from_environment(cls, service_root: Path) -> "HerokuWebConfig":
        return cls(
            service_root=service_root.resolve(),
            port=parse_positive_int("PORT", parse_positive_int("WEB_PORT", 8080)),
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
            api_rate_limit_max_requests=parse_positive_int("API_RATE_LIMIT_MAX_REQUESTS", 60),
            api_rate_limit_window_seconds=parse_positive_int("API_RATE_LIMIT_WINDOW_SECONDS", 60),
        )

    def wake_enabled(self) -> bool:
        return bool(self.github_access_token and self.github_codespace_name)

    def describe_environment(self) -> Dict[str, object]:
        return {
            "serviceRoot": str(self.service_root),
            "port": self.port,
            "remoteBuilderBaseUrl": self.remote_builder_base_url,
            "remoteBuilderHealthPath": self.remote_builder_health_path,
            "remoteBuilderTokenConfigured": bool(self.remote_builder_token),
            "codespaceWakeEnabled": self.wake_enabled(),
            "githubCodespaceName": self.github_codespace_name,
            "githubApiBaseUrl": self.github_api_base_url,
            "githubApiVersion": self.github_api_version,
            "builderRequestTimeoutSeconds": self.builder_request_timeout_seconds,
            "codespaceStartTimeoutSeconds": self.codespace_start_timeout_seconds,
            "codespaceWakeCooldownSeconds": self.codespace_wake_cooldown_seconds,
            "apiRateLimitMaxRequests": self.api_rate_limit_max_requests,
            "apiRateLimitWindowSeconds": self.api_rate_limit_window_seconds,
        }


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

    def forward_get(self, path: str) -> requests.Response:
        return self._execute("GET", path, headers={"Accept": "*/*"})

    def forward_multipart(self, path: str) -> requests.Response:
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

        return self._execute("POST", path, headers={"Accept": "application/json"}, data=data, files=files)

    def _execute(self, method: str, path: str, headers=None, data=None, files=None) -> requests.Response:
        try:
            return self.session.request(
                method=method,
                url=self._builder_url(path),
                headers=self._builder_headers(headers),
                timeout=self.config.builder_request_timeout_seconds,
                data=data,
                files=files,
                stream=True,
            )
        except requests.RequestException as error:
            raise BuilderUnavailableError(
                "Remote Codespace builder se connect nahi ho paya. REMOTE_BUILDER_BASE_URL aur port forwarding check karo."
            ) from error

    def _builder_headers(self, extra_headers=None) -> Dict[str, str]:
        headers = dict(extra_headers or {})
        if self.config.remote_builder_token:
            headers[BUILDER_TOKEN_HEADER] = self.config.remote_builder_token
        return headers

    def _builder_url(self, path: str) -> str:
        normalized = path if path.startswith("/") else f"/{path}"
        return f"{self.config.remote_builder_base_url}{normalized}"


class CodespaceWakeService:
    def __init__(self, config: HerokuWebConfig, proxy_client: BuilderProxyClient) -> None:
        self.config = config
        self.proxy_client = proxy_client
        self.session = requests.Session()
        self._last_wake_attempt_at = 0.0
        self._lock = threading.Lock()

    def ensure_builder_ready(self) -> None:
        if self.proxy_client.is_builder_healthy():
            return

        with self._lock:
            if self.proxy_client.is_builder_healthy():
                return

            if not self.config.wake_enabled():
                raise BuilderUnavailableError(
                    "Codespace builder abhi reachable nahi hai. GITHUB_ACCESS_TOKEN aur GITHUB_CODESPACE_NAME set karke auto-wake enable karo."
                )

            self._maybe_start_codespace()
            self._wait_for_builder()

    def probe_builder(self) -> bool:
        return self.proxy_client.is_builder_healthy()

    def _maybe_start_codespace(self) -> None:
        now = time.time()
        if now - self._last_wake_attempt_at < self.config.codespace_wake_cooldown_seconds:
            return

        if not self.config.github_codespace_name:
            raise BuilderUnavailableError("GITHUB_CODESPACE_NAME missing hai.")
        if not self.config.github_access_token:
            raise BuilderUnavailableError("GITHUB_ACCESS_TOKEN missing hai.")

        url = f"{self.config.github_api_base_url}/user/codespaces/{quote(self.config.github_codespace_name, safe='')}/start"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.config.github_access_token}",
            "X-GitHub-Api-Version": self.config.github_api_version,
            "User-Agent": "apk-builder-heroku-web",
        }

        try:
            response = self.session.post(url, headers=headers, timeout=30)
        except requests.RequestException as error:
            raise BuilderUnavailableError("GitHub Codespace ko wake nahi kar paye.") from error

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


def create_app() -> Flask:
    load_env_file(BASE_DIR / ".env")
    config = HerokuWebConfig.from_environment(BASE_DIR)
    proxy_client = BuilderProxyClient(config)
    wake_service = CodespaceWakeService(config, proxy_client)
    rate_limiter = ApiRateLimiter(config.api_rate_limit_max_requests, config.api_rate_limit_window_seconds)

    app = Flask(__name__, static_folder=str(PUBLIC_DIR), static_url_path="")
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[assignment]

    @app.before_request
    def apply_request_guards():
        if request.path.startswith("/api/"):
            rate_limiter.check(resolve_client_key())

    @app.after_request
    def apply_security_headers(response: Response) -> Response:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob: https:; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "no-cache"
        return response

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

        return Response("Unexpected server error", status=500, mimetype="text/plain")

    @app.get("/")
    def root():
        return redirect("/index.html", code=302)

    @app.get("/health")
    def health():
        return jsonify(
            {
                "status": "ok",
                "environment": config.describe_environment(),
                "builderReachable": wake_service.probe_builder(),
            }
        )

    @app.get("/api/builds")
    def list_builds():
        wake_service.ensure_builder_ready()
        return proxy_response(proxy_client.forward_get("/api/builds"))

    @app.get("/api/builds/<job_id>")
    def get_build(job_id: str):
        wake_service.ensure_builder_ready()
        return proxy_response(proxy_client.forward_get(f"/api/builds/{job_id}"))

    @app.post("/api/builds")
    def create_build():
        wake_service.ensure_builder_ready()
        return proxy_response(proxy_client.forward_multipart("/api/builds"))

    @app.get("/api/builds/<job_id>/apk")
    def download_apk(job_id: str):
        wake_service.ensure_builder_ready()
        return proxy_response(proxy_client.forward_get(f"/api/builds/{job_id}/apk"))

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


def resolve_client_key() -> str:
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        client_ip = forwarded_for.split(",", 1)[0].strip()
        if client_ip:
            return client_ip
    return request.remote_addr or "unknown"


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


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8090")))
