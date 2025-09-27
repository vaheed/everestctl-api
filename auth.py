import functools
import hmac
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import Header, HTTPException, Request


logger = logging.getLogger(__name__)


def consttime_equals(a: str, b: str) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode(), b.encode())


class RateLimiter:
    def __init__(self, rate_qps: float, burst: int):
        self.rate = rate_qps
        self.capacity = float(burst)
        self.tokens = {}
        self.timestamps = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            tokens = self.tokens.get(key, self.capacity)
            last = self.timestamps.get(key, now)
            # Refill
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < 1.0:
                # Not enough tokens
                self.tokens[key] = tokens
                self.timestamps[key] = now
                return False
            tokens -= 1.0
            self.tokens[key] = tokens
            self.timestamps[key] = now
            return True


def require_admin_key(expected_key: str):
    async def verifier(x_admin_key: Optional[str] = Header(default=None), authorization: Optional[str] = Header(default=None)):
        provided = x_admin_key
        if not provided and authorization and authorization.lower().startswith("bearer "):
            provided = authorization.split(" ", 1)[1].strip()
        if not consttime_equals(provided or "", expected_key or ""):
            raise HTTPException(status_code=401, detail="invalid admin key")
        return True

    return verifier


def make_rate_limiter(rate_qps: float, burst: int):
    limiter = RateLimiter(rate_qps, burst)

    def dependency(request: Request):
        key = f"{request.client.host}:{request.url.path}"
        if not limiter.allow(key):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        return True

    return dependency


@dataclass
class IdempotencyStore:
    put_fn: Callable[[str, str, int], None]
    get_fn: Callable[[str], Optional[tuple[str, str, int]]]


def idempotency_dependency(store: IdempotencyStore):
    async def dep(request: Request):
        if request.method not in ("POST", "PUT", "DELETE"):
            return None
        key = request.headers.get("Idempotency-Key") or request.headers.get("Idempotency_key")
        if not key:
            return None
        found = store.get_fn(key)
        if found is None:
            # Mark pending with placeholder so concurrent duplicates don't proceed.
            try:
                store.put_fn(key, json.dumps({"pending": True}), 425)
            except Exception:
                # If duplicate insert, ignore; otherwise continue.
                pass
            return key
        # If found, replay response
        body, content_type, status_code = found
        raise HTTPException(status_code=status_code, detail={"replay": True, "body": json.loads(body) if content_type == "application/json" else body})

    return dep

