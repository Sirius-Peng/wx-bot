"""Management authentication, kept separate from OneBot callback credentials."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import time
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request, Response

COOKIE = "wxbot_admin_session"
SESSION_SECONDS = 12 * 3600


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class AdminAuth:
    def __init__(self, data_dir: Path, bind_host: str):
        self.bind_host = bind_host
        self.token = os.environ.get("WXBOT_ADMIN_TOKEN", "").strip()
        if not self.token:
            path = data_dir / "admin-token"
            try:
                self.token = path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                pass
            if not self.token:
                self.token = secrets.token_urlsafe(32)
                fd, tmp = tempfile.mkstemp(dir=data_dir, suffix=".token.tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(self.token + "\n")
                    # 空文件可恢复；正常情况下只在首次启动时执行。
                    os.replace(tmp, path)
                finally:
                    Path(tmp).unlink(missing_ok=True)
            os.chmod(path, 0o600)

    def matches(self, value: str) -> bool:
        return secrets.compare_digest(value.encode("utf-8"), self.token.encode("utf-8"))

    def same_origin(self, request: Request) -> bool:
        if request.headers.get("sec-fetch-site") == "cross-site":
            return False
        origin = request.headers.get("origin")
        if not origin:
            return True  # CLI requests don't necessarily carry Origin.
        try:
            parsed = urlsplit(origin)
            return (parsed.scheme == request.url.scheme
                    and parsed.netloc == request.headers.get("host", "")
                    and not parsed.path and not parsed.query and not parsed.fragment)
        except ValueError:
            return False

    def can_bootstrap(self, request: Request) -> bool:
        # Only a loopback-bound desktop/source instance can auto-login a local page.
        # Wildcard/Docker bindings always require the configured token.
        return (is_loopback(self.bind_host)
                and request.client is not None and is_loopback(request.client.host)
                and is_loopback(request.url.hostname or "")
                and self.same_origin(request))

    def _signature(self, payload: str) -> str:
        return hmac.new(self.token.encode(), payload.encode(), hashlib.sha256).hexdigest()

    def set_cookie(self, request: Request, response: Response) -> None:
        payload = f"{int(time.time()) + SESSION_SECONDS}.{secrets.token_urlsafe(24)}"
        response.set_cookie(COOKIE, payload + "." + self._signature(payload),
                            max_age=SESSION_SECONDS, httponly=True,
                            secure=request.url.scheme == "https", samesite="strict")

    def authenticated(self, request: Request) -> bool:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer ") and self.matches(auth[7:]):
            return True
        cookie = request.cookies.get(COOKIE, "")
        try:
            expiry, nonce, signature = cookie.split(".")
            return (int(expiry) > time.time() and bool(nonce)
                    and secrets.compare_digest(signature, self._signature(f"{expiry}.{nonce}")))
        except (ValueError, TypeError):
            return False
