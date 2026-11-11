"""Shared HTTP pacing, disk TTL caching, and 403/429 cooldown protection across scrapers.

Designed to keep external site request volume low and non-bursty (lower concurrency,
inter-request jittered spacing per domain, TTL disk caching for slow-changing/heavy
sources, and automatic circuit-breaker cooldowns with stale cache fallback).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import random
import threading
import time
from typing import Any, Callable, Dict, Optional

CACHE_DIR = Path.home() / ".cache" / "nfl-ev-tracker"
COOLDOWN_PATH = CACHE_DIR / "domain_cooldowns.json"

_PACE_LOCK = threading.Lock()
_LAST_HIT: Dict[str, float] = {}
_COOLDOWN_LOCK = threading.Lock()


class RateLimitError(RuntimeError):
    """Raised when a target domain responds with 403 or 429."""

    def __init__(self, domain: str, status: int):
        super().__init__(f"{domain} returned HTTP {status}")
        self.domain = domain
        self.status = status


def pace_domain(domain: str, min_delay: float = 0.45, jitter: float = 0.35) -> None:
    """Enforce polite minimum spacing (+ random jitter) between requests to the same domain."""
    wait_for = 0.0
    target_gap = max(0.0, float(min_delay)) + random.uniform(0.0, max(0.0, float(jitter)))
    with _PACE_LOCK:
        now = time.monotonic()
        last = _LAST_HIT.get(domain, 0.0)
        elapsed = now - last
        if last > 0.0 and elapsed < target_gap:
            wait_for = target_gap - elapsed
            _LAST_HIT[domain] = now + wait_for
        else:
            _LAST_HIT[domain] = now
    if wait_for > 0.0:
        time.sleep(wait_for)


def _read_cooldowns() -> Dict[str, float]:
    if not COOLDOWN_PATH.exists():
        return {}
    try:
        data = json.loads(COOLDOWN_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_cooldowns(data: Dict[str, float]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = COOLDOWN_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(COOLDOWN_PATH)
    except Exception:
        pass


def get_domain_cooldown_remaining(domain: str) -> float:
    """Return seconds remaining in a 403/429 cooldown for `domain`, or 0.0 if clear."""
    now = time.time()
    with _COOLDOWN_LOCK:
        cds = _read_cooldowns()
        until = float(cds.get(domain) or 0.0)
        if until > now:
            return until - now
        if domain in cds:
            cds.pop(domain, None)
            _write_cooldowns(cds)
    return 0.0


def trip_domain_cooldown(domain: str, minutes: float = 20.0) -> None:
    """Pause live requests to `domain` for `minutes` after a 403/429 response."""
    until = time.time() + max(1.0, float(minutes)) * 60.0
    with _COOLDOWN_LOCK:
        cds = _read_cooldowns()
        cds[domain] = max(float(cds.get(domain) or 0.0), until)
        _write_cooldowns(cds)


def load_source_cache(cache_key: str, ttl_seconds: float, allow_stale: bool = False) -> Optional[Any]:
    """Load cached scraper data if younger than `ttl_seconds` (or up to 24h when `allow_stale=True`)."""
    path = CACHE_DIR / f"src_cache_{cache_key}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        ts = float(raw.get("ts") or 0.0)
        age = max(0.0, time.time() - ts)
        max_age = 86400.0 if allow_stale else float(ttl_seconds)
        if age <= max_age:
            return raw.get("data")
    except Exception:
        return None
    return None


def save_source_cache(cache_key: str, data: Any) -> None:
    """Atomically write scraper data and UTC timestamp to disk cache."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / f"src_cache_{cache_key}.json"
        payload = {
            "ts": time.time(),
            "cached_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "data": data,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def fetch_with_cache(
    cache_key: str,
    domain: str,
    ttl_seconds: float,
    fetcher: Callable[[], Any],
    *,
    is_valid: Optional[Callable[[Any], bool]] = None,
    cooldown_minutes: float = 20.0,
    min_delay: float = 0.4,
    jitter: float = 0.3,
) -> Any:
    """Return fresh cached data, else fetch politely with 403/429 circuit breaker & stale fallback."""
    fresh = load_source_cache(cache_key, ttl_seconds=ttl_seconds, allow_stale=False)
    if fresh is not None:
        return fresh

    stale = load_source_cache(cache_key, ttl_seconds=ttl_seconds, allow_stale=True)
    if get_domain_cooldown_remaining(domain) > 0:
        return stale

    pace_domain(domain, min_delay=min_delay, jitter=jitter)
    try:
        result = fetcher()
        valid = is_valid(result) if is_valid else bool(result)
        if valid:
            save_source_cache(cache_key, result)
            return result
        return stale if stale is not None else result
    except RateLimitError:
        trip_domain_cooldown(domain, minutes=cooldown_minutes)
        return stale
    except Exception:
        return stale
