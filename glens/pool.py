"""Multi-route session pool with per-slot rate limits, cooldowns, and failover.

Out of the box this manages:
  * Vercel proxy route(s) from ``GLENS_VERCEL_PROXY_URLS`` (or single
    ``GLENS_VERCEL_PROXY_URL``), and
  * Docker/local route(s) from ``GLENS_LOCAL_PROXIES`` (or a single default
    local route using ``GLENS_PROXY`` / direct egress).

When ``GLENS_PROXY_FALLBACK=1``, a ``proxy="vercel"`` lookup prefers healthy
Vercel slots within their RPM budget and automatically fails over to healthy
local slots whenever Vercel is rate-capped, cooling down, or hits a Google bot
challenge / 429 / proxy error.
"""

from __future__ import annotations

import logging
import math
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from . import cache as cache_mod
from . import lens as lens_mod
from .driver import build_driver

logger = logging.getLogger("glens")

_CHALLENGE_BACKOFF_SECONDS = (120.0, 300.0, 900.0)
_RATE_LIMIT_BACKOFF_SECONDS = (60.0, 180.0, 600.0)
_TRANSIENT_COOLDOWN_SECONDS = 30.0
_MAX_SPACING_WAIT_SECONDS = 2.5


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
        if not math.isfinite(value):
            return default
        return max(minimum, value)
    except ValueError:
        return default


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = float(value.strip())
        if math.isfinite(parsed) and parsed > 0:
            return parsed
    except ValueError:
        return None
    return None


@dataclass
class RouteSlot:
    """One isolated outbound route with its own cookie file, rate cap, and cooldown."""

    slot_id: str
    kind: Literal["vercel", "local"]
    priority: int
    cookie_file: Path
    proxy_url: str | None = None
    proxy_secret: str | None = None
    local_proxy: str | None = None
    profile_dir: Path | None = None
    rpm_limit: int = 15
    min_interval: float = 0.5
    state: str = "healthy"
    cooldown_until: float = 0.0
    cooldown_reason: str | None = None
    challenge_streak: int = 0
    rate_limit_streak: int = 0
    success_count: int = 0
    challenge_count: int = 0
    rate_limit_count: int = 0
    error_count: int = 0
    last_used_at: float = 0.0
    last_refresh_failed_at: float = 0.0
    timestamps: deque[float] = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    refresh_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _prune_locked(self, now: float) -> None:
        cutoff = now - 60.0
        while self.timestamps and self.timestamps[0] <= cutoff:
            self.timestamps.popleft()

    def _refresh_state_locked(self, now: float) -> str:
        if self.state == "cooldown" and now >= self.cooldown_until:
            self.state = "healthy"
            self.cooldown_until = 0.0
            self.cooldown_reason = None
        return self.state

    def effective_state(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        with self._lock:
            return self._refresh_state_locked(now)

    def cooldown_remaining(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        with self._lock:
            self._refresh_state_locked(now)
            if self.state != "cooldown":
                return 0.0
            return max(0.0, self.cooldown_until - now)

    def try_reserve(self, now: float | None = None) -> tuple[bool, float]:
        """Attempt to reserve one dispatch slot.

        Returns ``(reserved, wait_seconds)`` where ``wait_seconds > 0`` means the
        slot is healthy but temporarily paced by ``min_interval`` or ``rpm_limit``.
        """
        now = time.time() if now is None else now
        with self._lock:
            state = self._refresh_state_locked(now)
            if state != "healthy":
                return False, max(0.0, self.cooldown_until - now)
            self._prune_locked(now)
            if self.rpm_limit > 0 and len(self.timestamps) >= self.rpm_limit:
                oldest = self.timestamps[0]
                return False, max(0.1, (oldest + 60.0) - now)
            if self.min_interval > 0 and self.last_used_at > 0:
                elapsed = now - self.last_used_at
                if elapsed < self.min_interval:
                    return False, self.min_interval - elapsed
            self.timestamps.append(now)
            self.last_used_at = now
            return True, 0.0

    def mark_success(self) -> None:
        with self._lock:
            self.success_count += 1
            self.challenge_streak = 0
            self.rate_limit_streak = 0
            self.state = "healthy"
            self.cooldown_until = 0.0
            self.cooldown_reason = None

    def mark_bot_challenge(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        with self._lock:
            self.challenge_count += 1
            self.challenge_streak += 1
            idx = min(self.challenge_streak - 1, len(_CHALLENGE_BACKOFF_SECONDS) - 1)
            duration = _CHALLENGE_BACKOFF_SECONDS[idx]
            self.state = "cooldown"
            self.cooldown_until = now + duration
            self.cooldown_reason = "bot_challenge"
            return duration

    def mark_rate_limited(
        self,
        retry_after: str | None = None,
        now: float | None = None,
    ) -> float:
        now = time.time() if now is None else now
        parsed = _parse_retry_after(retry_after)
        with self._lock:
            self.rate_limit_count += 1
            self.rate_limit_streak += 1
            idx = min(self.rate_limit_streak - 1, len(_RATE_LIMIT_BACKOFF_SECONDS) - 1)
            duration = parsed if parsed is not None else _RATE_LIMIT_BACKOFF_SECONDS[idx]
            self.state = "cooldown"
            self.cooldown_until = now + duration
            self.cooldown_reason = "rate_limited"
            return duration

    def mark_transient_error(
        self,
        duration: float = _TRANSIENT_COOLDOWN_SECONDS,
        now: float | None = None,
        reason: str = "transient_error",
    ) -> float:
        now = time.time() if now is None else now
        with self._lock:
            self.error_count += 1
            self.state = "cooldown"
            self.cooldown_until = now + duration
            self.cooldown_reason = reason
            return duration

    def set_refreshing(self, refreshing: bool) -> None:
        with self._lock:
            if refreshing:
                self.state = "refreshing"
            elif self.state == "refreshing":
                self.state = "healthy"

    def reset_cooldown(self) -> None:
        with self._lock:
            self.state = "healthy"
            self.cooldown_until = 0.0
            self.cooldown_reason = None
            self.challenge_streak = 0
            self.rate_limit_streak = 0

    def status_dict(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            state = self._refresh_state_locked(now)
            self._prune_locked(now)
            remaining = max(0.0, self.cooldown_until - now) if state == "cooldown" else 0.0
            return {
                "slot_id": self.slot_id,
                "kind": self.kind,
                "state": state,
                "cooldown_reason": self.cooldown_reason,
                "cooldown_remaining_seconds": round(remaining, 1),
                "rpm_used": len(self.timestamps),
                "rpm_limit": self.rpm_limit,
                "success_count": self.success_count,
                "challenge_count": self.challenge_count,
                "rate_limit_count": self.rate_limit_count,
                "error_count": self.error_count,
                "cookie_ready": lens_mod._load_cookie_jar(self.cookie_file) is not None,
            }


def _route_cookie_path(base_cookie: Path, suffix: str) -> Path:
    stem = base_cookie.stem or "cookies"
    ext = base_cookie.suffix or ".json"
    return base_cookie.with_name(f"{stem}-{suffix}{ext}")


class RoutePool:
    """Coordinate Vercel and local Lens slots with pacing, caching, and failover."""

    def __init__(
        self,
        slots: list[RouteSlot],
        *,
        base_cookie_file: Path | None = None,
        proxy_fallback: bool | None = None,
        jitter_seconds: float | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.slots = list(slots)
        self.base_cookie_file = base_cookie_file or lens_mod._default_cookie_file()
        self.proxy_fallback = (
            _env_flag("GLENS_PROXY_FALLBACK", False)
            if proxy_fallback is None else proxy_fallback
        )
        self.jitter_seconds = (
            _env_float("GLENS_SLOT_JITTER", 0.3, minimum=0.0)
            if jitter_seconds is None else jitter_seconds
        )
        self._clock = clock or time.time
        self._sleep = sleeper or time.sleep
        self._inflight = cache_mod.InFlightCoordinator()

    @classmethod
    def from_env(cls, *, base_cookie_file: Path | None = None) -> RoutePool:
        base_cookie = base_cookie_file or lens_mod._default_cookie_file()
        vercel_rpm = _env_int("GLENS_VERCEL_RPM", 20, minimum=0)
        local_rpm = _env_int("GLENS_LOCAL_RPM", 15, minimum=0)
        min_interval = _env_float("GLENS_SLOT_MIN_INTERVAL", 0.5, minimum=0.0)

        slots: list[RouteSlot] = []

        # 1) Vercel slot(s): GLENS_VERCEL_PROXY_URLS or single GLENS_VERCEL_PROXY_URL
        raw_vercel_urls = os.getenv("GLENS_VERCEL_PROXY_URLS", "").strip()
        if raw_vercel_urls:
            vercel_urls = _split_csv(raw_vercel_urls)
        else:
            single_url = os.getenv("GLENS_VERCEL_PROXY_URL", "").strip()
            vercel_urls = [single_url] if single_url else []

        raw_secrets = os.getenv("GLENS_VERCEL_PROXY_SECRETS", "").strip()
        shared_secret = os.getenv("GLENS_VERCEL_PROXY_SECRET", "")
        vercel_secrets = _split_csv(raw_secrets) if raw_secrets else []

        for idx, url in enumerate(vercel_urls):
            secret = vercel_secrets[idx] if idx < len(vercel_secrets) else shared_secret
            slots.append(RouteSlot(
                slot_id=f"vercel-{idx}",
                kind="vercel",
                priority=0,
                cookie_file=_route_cookie_path(base_cookie, f"vercel-{idx}"),
                proxy_url=url,
                proxy_secret=secret,
                rpm_limit=vercel_rpm,
                min_interval=min_interval,
            ))

        # 2) Local slot(s): GLENS_LOCAL_PROXIES or default single local slot
        base_profile_raw = os.getenv("GLENS_CHROME_PROFILE", "").strip()
        base_profile = Path(base_profile_raw) if base_profile_raw else None
        raw_local_proxies = os.getenv("GLENS_LOCAL_PROXIES", "").strip()
        if raw_local_proxies:
            local_proxies = _split_csv(raw_local_proxies)
            for idx, proxy_addr in enumerate(local_proxies):
                cookie_path = (
                    base_cookie if idx == 0 else _route_cookie_path(base_cookie, f"local-{idx}")
                )
                profile_path = (
                    base_profile if (idx == 0 or base_profile is None)
                    else base_profile.with_name(f"{base_profile.name}-local-{idx}")
                )
                slots.append(RouteSlot(
                    slot_id=f"local-{idx}",
                    kind="local",
                    priority=10,
                    cookie_file=cookie_path,
                    local_proxy=proxy_addr,
                    profile_dir=profile_path,
                    rpm_limit=local_rpm,
                    min_interval=min_interval,
                ))
        else:
            single_local_proxy = os.getenv("GLENS_PROXY", "").strip() or None
            slots.append(RouteSlot(
                slot_id="local-0",
                kind="local",
                priority=10,
                cookie_file=base_cookie,
                local_proxy=single_local_proxy,
                profile_dir=base_profile,
                rpm_limit=local_rpm,
                min_interval=min_interval,
            ))

        return cls(slots, base_cookie_file=base_cookie)

    def _candidate_slots(self, proxy: str | None) -> list[RouteSlot]:
        vercel_slots = sorted(
            (s for s in self.slots if s.kind == "vercel"),
            key=lambda s: s.last_used_at,
        )
        local_slots = sorted(
            (s for s in self.slots if s.kind == "local"),
            key=lambda s: s.last_used_at,
        )
        if proxy == "vercel":
            if self.proxy_fallback:
                return vercel_slots + local_slots
            return vercel_slots
        return local_slots

    def _make_driver_factory(self, slot: RouteSlot) -> Callable:
        def _factory():
            return build_driver(user_data_dir=slot.profile_dir, proxy=slot.local_proxy)
        return _factory

    def _execute_on_slot(
        self,
        slot: RouteSlot,
        frame_bytes: bytes,
        *,
        lang: str | None,
        timeout: int,
        backend: str,
    ) -> tuple[list[dict] | None, str | None]:
        if slot.kind == "vercel":
            lens_mod._ensure_route_cookie_file(slot.cookie_file, self.base_cookie_file)
            return lens_mod._search_via_requests(
                frame_bytes,
                slot.cookie_file,
                lang,
                proxy="vercel",
                proxy_url=slot.proxy_url,
                proxy_secret=slot.proxy_secret,
            )

        # Local route: try fast HTTP first (curl_cffi / requests), then single-flight Chromium.
        items: list[dict] | None = None
        results_url: str | None = None
        if backend in ("auto", "req"):
            items, results_url = lens_mod._search_via_requests(
                frame_bytes,
                slot.cookie_file,
                lang,
                local_proxy=slot.local_proxy,
            )
        if items is not None or backend == "req":
            return items, results_url

        # Single-flight browser refresh for this slot on stale/missing cookies.
        before_stamp = lens_mod._jar_stamp(slot.cookie_file)
        with slot.refresh_lock:
            now = self._clock()
            after_wait_stamp = lens_mod._jar_stamp(slot.cookie_file)
            if after_wait_stamp is not None and after_wait_stamp != before_stamp:
                # Another request just refreshed this slot's cookies; ride req path.
                items, results_url = lens_mod._search_via_requests(
                    frame_bytes,
                    slot.cookie_file,
                    lang,
                    local_proxy=slot.local_proxy,
                )
                if items is not None:
                    return items, results_url
            if slot.last_refresh_failed_at and now - slot.last_refresh_failed_at < 30.0:
                return None, results_url
            slot.set_refreshing(True)
            try:
                driver_factory = self._make_driver_factory(slot)
                with lens_mod._browser_lock:
                    items, results_url = lens_mod._search_via_browser(
                        frame_bytes, slot.cookie_file, timeout, driver_factory
                    )
                if items is None:
                    slot.last_refresh_failed_at = self._clock()
                else:
                    slot.last_refresh_failed_at = 0.0
                    # Sync freshly minted cookies to Vercel slots and release stale-jar cooldowns.
                    for other in self.slots:
                        if other.kind == "vercel":
                            lens_mod._ensure_route_cookie_file(
                                other.cookie_file, self.base_cookie_file
                            )
                            if other.cooldown_reason == "proxy_lookup_failed":
                                other.reset_cooldown()
                return items, results_url
            finally:
                slot.set_refreshing(False)

    def _try_acquire_any(
        self,
        candidates: list[RouteSlot],
        tried: set[str],
    ) -> tuple[RouteSlot | None, float, str | None]:
        """Try reserving the next untried slot; returns (slot, min_wait, block_reason)."""
        now = self._clock()
        min_spacing_wait: float | None = None
        min_cooldown_wait: float | None = None
        saw_bot_challenge = False
        saw_rate_limit = False
        refreshing_slot: RouteSlot | None = None

        for slot in candidates:
            if slot.slot_id in tried:
                continue
            reserved, wait = slot.try_reserve(now)
            if reserved:
                return slot, 0.0, None
            state = slot.effective_state(now)
            if state == "refreshing":
                if refreshing_slot is None:
                    refreshing_slot = slot
            elif state == "cooldown":
                if slot.cooldown_reason == "bot_challenge":
                    saw_bot_challenge = True
                elif slot.cooldown_reason == "rate_limited":
                    saw_rate_limit = True
                if min_cooldown_wait is None or wait < min_cooldown_wait:
                    min_cooldown_wait = wait
            elif state == "healthy" and wait > 0:
                if min_spacing_wait is None or wait < min_spacing_wait:
                    min_spacing_wait = wait

        # If a local slot is currently re-minting cookies, wait once for it to finish
        # so concurrent callers ride the freshly minted session instead of failing fast.
        if refreshing_slot is not None:
            with refreshing_slot.refresh_lock:
                pass
            now = self._clock()
            for slot in candidates:
                if slot.slot_id in tried:
                    continue
                reserved, wait = slot.try_reserve(now)
                if reserved:
                    return slot, 0.0, None
                if slot.effective_state(now) == "healthy" and wait > 0:
                    if min_spacing_wait is None or wait < min_spacing_wait:
                        min_spacing_wait = wait

        # If a healthy slot only needs a short spacing wait, pause once and re-acquire.
        if min_spacing_wait is not None and min_spacing_wait <= _MAX_SPACING_WAIT_SECONDS:
            jitter = random.uniform(0.0, self.jitter_seconds) if self.jitter_seconds > 0 else 0.0
            self._sleep(min_spacing_wait + jitter)
            now = self._clock()
            for slot in candidates:
                if slot.slot_id in tried:
                    continue
                reserved, _ = slot.try_reserve(now)
                if reserved:
                    return slot, 0.0, None

        if saw_bot_challenge:
            return None, min_cooldown_wait or 60.0, "bot_challenge"
        if saw_rate_limit or min_spacing_wait is not None:
            wait_s = min_spacing_wait if min_spacing_wait is not None else (min_cooldown_wait or 5)
            return None, max(1.0, wait_s), "rate_limited"
        return None, min_cooldown_wait or 5.0, "unavailable"

    def search(
        self,
        frame_bytes: bytes,
        *,
        lang: str | None = None,
        max_matches: int | None = None,
        max_titles: int | None = None,
        timeout: int | None = None,
        use_cache: bool | None = None,
        resolve_links: bool = False,
        proxy: str | None = None,
        backend: str | None = None,
    ) -> tuple[lens_mod.LensResult | None, str]:
        """Run one search through cache, single-flight dedup, and the route pool."""
        max_matches = lens_mod.DEFAULT_MAX_MATCHES if max_matches is None else max_matches
        max_titles = lens_mod.DEFAULT_MAX_TITLES if max_titles is None else max_titles
        timeout = lens_mod.DEFAULT_RESULT_TIMEOUT if timeout is None else timeout
        lang = lang or lens_mod.DEFAULT_LANG
        use_cache = lens_mod.DEFAULT_USE_CACHE if use_cache is None else use_cache
        backend = (backend or "").strip().lower() or lens_mod.DEFAULT_BACKEND

        if proxy not in (None, "vercel"):
            raise ValueError("proxy must be None or 'vercel'")
        if proxy == "vercel" and not self.proxy_fallback and not any(
            s.kind == "vercel" for s in self.slots
        ):
            raise lens_mod.LensProxyError(
                "proxy_not_configured",
                "Vercel proxy URL and secret must be configured.",
            )

        c_key = cache_mod.cache_key(frame_bytes, lang)
        dhash: int | None = None
        if use_cache:
            cached = lens_mod._cache_read(c_key, lens_mod.DEFAULT_CACHE_TTL)
            if cached is not None:
                cache_mod.stats.record_l1_hit()
                items, results_url = cached
                shaped = lens_mod._shape_result(
                    items, results_url,
                    max_matches=max_matches, max_titles=max_titles,
                    resolve_links=resolve_links, cookie_path=self.base_cookie_file,
                )
                return shaped, "cache-l1"
            dhash = cache_mod.compute_dhash(frame_bytes)
            cached_l2 = lens_mod._cache_read_dhash(dhash, lang, lens_mod.DEFAULT_CACHE_TTL)
            if cached_l2 is not None:
                cache_mod.stats.record_l2_hit()
                items, results_url = cached_l2
                shaped = lens_mod._shape_result(
                    items, results_url,
                    max_matches=max_matches, max_titles=max_titles,
                    resolve_links=resolve_links, cookie_path=self.base_cookie_file,
                )
                return shaped, "cache-l2"
            cache_mod.stats.record_miss()

        flight_key = f"{c_key}|{proxy or 'local'}|{int(resolve_links)}"

        def _run_upstream() -> tuple[list[dict] | None, str | None, str, Path]:
            candidates = self._candidate_slots(proxy)
            if not candidates:
                raise lens_mod.LensProxyError(
                    "proxy_not_configured",
                    "No route slots configured for this request.",
                )
            tried: set[str] = set()
            last_challenge: lens_mod.LensBotChallengeError | None = None
            last_rate_limit: lens_mod.LensRateLimitError | None = None
            last_proxy_error: lens_mod.LensProxyError | None = None

            while len(tried) < len(candidates):
                slot, wait_s, reason = self._try_acquire_any(candidates, tried)
                if slot is None:
                    if not tried:
                        retry_str = str(max(1, int(math.ceil(wait_s))))
                        if reason == "bot_challenge":
                            err = lens_mod.LensBotChallengeError(reason="all_routes_in_cooldown")
                            err.retry_after = retry_str  # type: ignore[attr-defined]
                            raise err
                        if reason == "rate_limited":
                            raise lens_mod.LensRateLimitError(retry_str)
                    break

                tried.add(slot.slot_id)
                try:
                    items, results_url = self._execute_on_slot(
                        slot, frame_bytes, lang=lang, timeout=timeout, backend=backend,
                    )
                except lens_mod.LensBotChallengeError as exc:
                    cooldown = slot.mark_bot_challenge(self._clock())
                    exc.retry_after = str(int(math.ceil(cooldown)))  # type: ignore[attr-defined]
                    last_challenge = exc
                    logger.warning(
                        "glens pool: slot %s hit bot challenge; cooling down %.0fs and failing "
                        "over if another route is available.",
                        slot.slot_id, cooldown,
                    )
                    continue
                except lens_mod.LensRateLimitError as exc:
                    cooldown = slot.mark_rate_limited(exc.retry_after, self._clock())
                    if not exc.retry_after:
                        exc.retry_after = str(int(math.ceil(cooldown)))
                    last_rate_limit = exc
                    logger.warning(
                        "glens pool: slot %s rate limited (429); cooling down %.0fs.",
                        slot.slot_id, cooldown,
                    )
                    continue
                except lens_mod.LensProxyError as exc:
                    slot.mark_transient_error(now=self._clock(), reason=exc.code)
                    last_proxy_error = exc
                    logger.warning(
                        "glens pool: slot %s proxy error (%s); trying next route.",
                        slot.slot_id, exc.code,
                    )
                    continue

                if items is not None:
                    slot.mark_success()
                    return items, results_url, slot.slot_id, slot.cookie_file

                # Degraded/empty inconclusive page on a Vercel slot triggers failover.
                if slot.kind == "vercel":
                    slot.mark_transient_error(
                        duration=60.0, now=self._clock(), reason="proxy_lookup_failed",
                    )
                    last_proxy_error = lens_mod.LensProxyError(
                        "proxy_lookup_failed",
                        "Vercel proxy did not return usable Lens results.",
                    )
                    continue
                slot.mark_transient_error(
                    duration=15.0, now=self._clock(), reason="lookup_failed",
                )

            if last_challenge is not None:
                raise last_challenge
            if last_rate_limit is not None:
                raise last_rate_limit
            if last_proxy_error is not None:
                raise last_proxy_error
            return None, None, "none", self.base_cookie_file

        items, results_url, route_id, used_cookie = self._inflight.run(flight_key, _run_upstream)
        shaped = lens_mod._shape_result(
            items, results_url,
            max_matches=max_matches, max_titles=max_titles,
            resolve_links=resolve_links, cookie_path=used_cookie,
        )
        if shaped is not None and items and use_cache:
            lens_mod._cache_write(c_key, items, results_url, dhash=dhash, lang=lang)
        return shaped, route_id

    def warmup(
        self,
        *,
        slot_id: str | None = None,
        reset_cooldown: bool = True,
        force: bool = True,
    ) -> dict:
        """Reset cooldowns and/or refresh session cookies for one or all slots."""
        targets = (
            [s for s in self.slots if s.slot_id == slot_id]
            if slot_id else list(self.slots)
        )
        # Warm up local slots first so Vercel slots clone freshly minted cookies.
        targets.sort(key=lambda s: 0 if s.kind == "local" else 1)
        results: dict[str, dict] = {}
        for slot in targets:
            if reset_cooldown:
                slot.reset_cooldown()
            if slot.kind == "local":
                ok = lens_mod.warmup(
                    cookie_file=slot.cookie_file,
                    driver_factory=self._make_driver_factory(slot),
                    force=force,
                )
                results[slot.slot_id] = {"ok": ok, "state": slot.effective_state(self._clock())}
            else:
                # Re-seed Vercel cookie file from the base local jar when forced.
                if force and slot.cookie_file.exists():
                    try:
                        slot.cookie_file.unlink()
                    except OSError:
                        pass
                lens_mod._ensure_route_cookie_file(slot.cookie_file, self.base_cookie_file)
                ready = lens_mod._load_cookie_jar(slot.cookie_file) is not None
                results[slot.slot_id] = {"ok": ready, "state": slot.effective_state(self._clock())}
        return {"slots": results}

    def status(self) -> dict:
        now = self._clock()
        return {
            "proxy_fallback": self.proxy_fallback,
            "routes": [slot.status_dict(now) for slot in self.slots],
            "cache_stats": cache_mod.stats.snapshot(),
        }
