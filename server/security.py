"""보안 유틸: 토큰, 레이트리밋, 보안 헤더.

[토큰]
- secrets.token_urlsafe(24) = 192비트 난수. 추측/무차별 대입이 사실상 불가능하다.
- 서버에는 SHA-256 해시만 저장한다 (data 폴더가 유출돼도 접속 링크를 복원할 수 없다).
- 비교는 hmac.compare_digest 로 상수 시간 비교한다.
- 토큰은 URL 의 fragment(#t=...)로만 전달한다. fragment 는 브라우저가 서버로 보내지 않으므로
  서버 접속 로그/프록시 로그/Referer 에 남지 않는다.

[레이트리밋]
- 토큰 버킷: 입찰/채팅/주최자 조작의 폭주를 막는다.
- 실패 제한: 접속(join)·방 생성(admin key) 실패가 잦은 IP 를 잠시 차단한다.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time

from . import config


# --- 토큰 ---------------------------------------------------------------------

def new_token() -> str:
    return secrets.token_urlsafe(config.TOKEN_BYTES)


def new_room_id() -> str:
    return secrets.token_urlsafe(config.ROOM_ID_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(candidate: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False
    return hmac.compare_digest(hash_token(candidate), stored_hash)


def safe_equal(a: str, b: str) -> bool:
    """문자열 상수 시간 비교 (관리자 키 등)."""
    return hmac.compare_digest(hashlib.sha256(a.encode()).digest(), hashlib.sha256(b.encode()).digest())


# --- 레이트리밋 -------------------------------------------------------------------

class RateLimiter:
    """키별 토큰 버킷. capacity 만큼 한 번에 허용하고 refill_per_sec 로 채워진다."""

    def __init__(self, capacity: float, refill_per_sec: float):
        self.capacity = capacity
        self.refill = refill_per_sec
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._last_prune = time.monotonic()

    def allow(self, key: str, cost: float = 1.0, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill)
            ok = tokens >= cost
            if ok:
                tokens -= cost
            self._buckets[key] = (tokens, now)
            if now - self._last_prune > 300:
                self._prune(now)
            return ok

    def forget(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def _prune(self, now: float) -> None:
        full_after = self.capacity / self.refill if self.refill > 0 else 3600
        stale = [k for k, (_, last) in self._buckets.items() if now - last > full_after + 60]
        for k in stale:
            del self._buckets[k]
        self._last_prune = now


class FailureLimiter:
    """실패 횟수가 window 안에 limit 에 도달하면 lockout 동안 차단한다."""

    def __init__(self, limit: int = 20, window: float = 60.0, lockout: float = 120.0):
        self.limit = limit
        self.window = window
        self.lockout = lockout
        self._fails: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            until = self._blocked_until.get(key)
            if until is None:
                return False
            if now >= until:
                del self._blocked_until[key]
                self._fails.pop(key, None)
                return False
            return True

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            hist = [t for t in self._fails.get(key, []) if now - t < self.window]
            hist.append(now)
            self._fails[key] = hist
            if len(hist) >= self.limit:
                self._blocked_until[key] = now + self.lockout
            if len(self._fails) > 5000:       # 메모리 상한
                self._fails.clear()

    def record_success(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


# --- HTTP 보안 헤더 -----------------------------------------------------------------

_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](:\d{1,5})?$")


def security_headers(host: str | None, secure: bool, is_static: bool) -> dict[str, str]:
    """모든 응답에 붙일 보안 헤더. Host 헤더는 검증된 값만 CSP 에 넣는다 (헤더 인젝션 방지)."""
    connect = "'self'"
    if host and _HOST_RE.match(host):
        connect += f" ws://{host} wss://{host}"
    csp = "; ".join([
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data: blob:",
        f"connect-src {connect}",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])
    headers = {
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Cache-Control": "no-cache" if is_static else "no-store",
    }
    if secure:
        headers["Strict-Transport-Security"] = "max-age=31536000"
    return headers
