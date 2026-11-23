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
_AUTO_WARMUP_MAX_BACKOFF_SECONDS = 1800.0


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

    def reset_cooldown_if_reason(self, reasons: tuple[str, ...]) -> bool:
        """Atomically clear cooldown only if currently cooling down for one of ``reasons``."""
        with self._lock:
            if self.state == "cooldown" and self.cooldown_reason in reasons:
                self.state = "healthy"
                self.cooldown_until = 0.0
                self.cooldown_reason = None
                self.challenge_streak = 0
                self.rate_limit_streak = 0
                return True
            return False

    def status_dict(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        jar = lens_mod._load_cookie_jar(self.cookie_file)
        cookie_age: float | None = None
        if jar is not None and jar.get("minted_at") is not None:
            try:
                cookie_age = round(max(0.0, now - float(jar["minted_at"])), 1)
            except (TypeError, ValueError):
                cookie_age = None
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
                "cookie_ready": jar is not None,
                "cookie_age_seconds": cookie_age,
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
        auto_warmup: bool | None = None,
        auto_warmup_interval: int | None = None,
        auto_warmup_check_interval: int | None = None,
        auto_warmup_startup_jitter: float | None = None,
        auto_warmup_retry_base: float | None = None,
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

        self.auto_warmup_enabled = (
            _env_flag("GLENS_AUTO_WARMUP", False)
            if auto_warmup is None else auto_warmup
        )
        self.auto_warmup_interval = (
            _env_int("GLENS_AUTO_WARMUP_INTERVAL", 14400, minimum=300)
            if auto_warmup_interval is None else max(1, int(auto_warmup_interval))
        )
        self.auto_warmup_check_interval = (
            _env_int("GLENS_AUTO_WARMUP_CHECK_INTERVAL", 60, minimum=5)
            if auto_warmup_check_interval is None else max(1, int(auto_warmup_check_interval))
        )
        self.auto_warmup_startup_jitter = (
            _env_float("GLENS_AUTO_WARMUP_STARTUP_JITTER", 5.0, minimum=0.0)
            if auto_warmup_startup_jitter is None else max(0.0, float(auto_warmup_startup_jitter))
        )
        self.auto_warmup_retry_base = (
            _env_float("GLENS_AUTO_WARMUP_RETRY_BASE", 60.0, minimum=5.0)
            if auto_warmup_retry_base is None else max(1.0, float(auto_warmup_retry_base))
        )
        self._auto_warmup_lock = threading.Lock()
        self._auto_warmup_stop = threading.Event()
        self._auto_warmup_thread: threading.Thread | None = None
        self._auto_warmup_runs = 0
        self._auto_warmup_success = 0
        self._auto_warmup_failure = 0
        self._auto_warmup_last_run_at: float | None = None
        self._auto_warmup_last_ok: bool | None = None
        self._auto_warmup_last_reason: str | None = None
        self._slot_warmup_failures: dict[str, int] = {}
        self._slot_warmup_next_retry: dict[str, float] = {}

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
                logger.info("glens pool: slot %s rode freshly refreshed cookies.", slot.slot_id)
                items, results_url = lens_mod._search_via_requests(
                    frame_bytes,
                    slot.cookie_file,
                    lang,
                    local_proxy=slot.local_proxy,
                )
                if items is not None:
                    return items, results_url
            if slot.last_refresh_failed_at and now - slot.last_refresh_failed_at < 30.0:
                logger.warning(
                    "glens pool: slot %s skipping Chromium refresh (recent failure %.1fs ago).",
                    slot.slot_id, now - slot.last_refresh_failed_at,
                )
                return None, results_url
            slot.set_refreshing(True)
            refresh_t0 = self._clock()
            logger.info("glens pool: slot %s starting Chromium cookie refresh.", slot.slot_id)
            try:
                driver_factory = self._make_driver_factory(slot)
                with lens_mod._browser_lock:
                    items, results_url = lens_mod._search_via_browser(
                        frame_bytes, slot.cookie_file, timeout, driver_factory
                    )
                refresh_dt = self._clock() - refresh_t0
                if items is None:
                    slot.last_refresh_failed_at = self._clock()
                    logger.warning(
                        "glens pool: slot %s Chromium cookie refresh failed after %.2fs.",
                        slot.slot_id, refresh_dt,
                    )
                else:
                    slot.last_refresh_failed_at = 0.0
                    # Sync freshly minted cookies to Vercel slots and release stale-jar cooldowns.
                    synced = (
                        self._sync_vercel_slots_from_base()
                        if slot.cookie_file == self.base_cookie_file else 0
                    )
                    logger.info(
                        "glens pool: slot %s Chromium cookie refresh succeeded in %.2fs "
                        "(synced %d vercel slot(s)).",
                        slot.slot_id, refresh_dt, synced,
                    )
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
                        logger.warning(
                            "glens pool: all candidate routes blocked (reason=%s retry_after=%ss).",
                            reason, retry_str,
                        )
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
                    logger.warning(
                        "glens pool: slot %s returned degraded/empty results; cooling down 60s "
                        "and failing over if another route is available.",
                        slot.slot_id,
                    )
                    last_proxy_error = lens_mod.LensProxyError(
                        "proxy_lookup_failed",
                        "Vercel proxy did not return usable Lens results.",
                    )
                    continue
                slot.mark_transient_error(
                    duration=15.0, now=self._clock(), reason="lookup_failed",
                )
                logger.warning(
                    "glens pool: slot %s lookup returned no usable results; cooling down 15s.",
                    slot.slot_id,
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

    def _sync_vercel_slots_from_base(self) -> int:
        """Atomically copy a freshly minted base cookie jar to every Vercel slot."""
        synced = 0
        for other in self.slots:
            if other.kind != "vercel":
                continue
            before_stamp = lens_mod._jar_stamp(other.cookie_file)
            lens_mod._ensure_route_cookie_file(other.cookie_file, self.base_cookie_file)
            after_stamp = lens_mod._jar_stamp(other.cookie_file)
            if after_stamp is None:
                continue
            if after_stamp != before_stamp:
                synced += 1
                other.reset_cooldown_if_reason(("proxy_cookie_missing", "proxy_lookup_failed"))
            else:
                other.reset_cooldown_if_reason(("proxy_cookie_missing",))
        return synced

    def _classify_auto_warmup_need(
        self, slot: RouteSlot, now: float,
    ) -> tuple[str | None, bool, bool]:
        """Return ``(reason, force, mark_refreshing)`` for a local slot."""
        jar = lens_mod._load_cookie_jar(slot.cookie_file)
        if jar is None:
            return "cookie_missing", False, True
        minted_at = jar.get("minted_at")
        try:
            age = max(0.0, now - float(minted_at)) if minted_at is not None else 0.0
        except (TypeError, ValueError):
            age = float(self.auto_warmup_interval)
        if age >= self.auto_warmup_interval:
            return "cookie_stale", True, False
        return None, False, False

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
                with slot.refresh_lock:
                    ok = lens_mod.warmup(
                        cookie_file=slot.cookie_file,
                        driver_factory=self._make_driver_factory(slot),
                        force=force,
                    )
                if ok:
                    with self._auto_warmup_lock:
                        self._slot_warmup_failures.pop(slot.slot_id, None)
                        self._slot_warmup_next_retry.pop(slot.slot_id, None)
                results[slot.slot_id] = {"ok": ok, "state": slot.effective_state(self._clock())}
            else:
                # Atomically sync Vercel cookie file from the base local jar without
                # deleting an existing valid jar if local warmup failed.
                lens_mod._ensure_route_cookie_file(slot.cookie_file, self.base_cookie_file)
                ready = lens_mod._load_cookie_jar(slot.cookie_file) is not None
                results[slot.slot_id] = {"ok": ready, "state": slot.effective_state(self._clock())}
        return {"slots": results}

    def run_auto_warmup_once(self) -> dict[str, str]:
        """Inspect local slots and proactively mint/renew cookies when needed.

        * Missing jar (cold start): acquires ``refresh_lock`` and marks the slot
          ``refreshing`` so concurrent requests ride the startup mint.
        * Stale jar (``age >= auto_warmup_interval``): acquires ``refresh_lock``
          WITHOUT marking ``refreshing``, so live traffic keeps using the valid
          jar on disk at full speed until the new jar is atomically swapped in.
        """
        outcomes: dict[str, str] = {}
        local_slots = [s for s in self.slots if s.kind == "local"]
        for slot in local_slots:
            if self._auto_warmup_stop.is_set():
                break
            now = self._clock()
            state = slot.effective_state(now)
            if state == "refreshing":
                outcomes[slot.slot_id] = "skipped_refreshing"
                continue
            if state == "cooldown":
                outcomes[slot.slot_id] = "skipped_cooldown"
                continue

            with self._auto_warmup_lock:
                next_retry = self._slot_warmup_next_retry.get(slot.slot_id, 0.0)
            if now < next_retry:
                outcomes[slot.slot_id] = "skipped_backoff"
                continue

            reason, force, mark_refreshing = self._classify_auto_warmup_need(slot, now)
            if reason is None:
                # Cookie is fresh; still ensure Vercel slots are seeded from base jar.
                if slot.cookie_file == self.base_cookie_file:
                    self._sync_vercel_slots_from_base()
                outcomes[slot.slot_id] = "fresh"
                continue

            if not slot.refresh_lock.acquire(blocking=False):
                outcomes[slot.slot_id] = "skipped_busy"
                continue

            t0 = self._clock()
            ok = False
            hit_challenge = False
            challenge_cooldown = 0.0
            skipped_outcome: str | None = None
            try:
                # Double-check state & cookie freshness under refresh_lock to avoid
                # redundant Chromium launches if a live request just refreshed or cooled down.
                state_after_lock = slot.effective_state(t0)
                if state_after_lock == "cooldown":
                    skipped_outcome = "skipped_cooldown"
                else:
                    reason, force, mark_refreshing = self._classify_auto_warmup_need(slot, t0)
                    if reason is None:
                        skipped_outcome = "fresh"

                if skipped_outcome is None:
                    if mark_refreshing:
                        slot.set_refreshing(True)
                    logger.info(
                        "glens auto-warmup: slot=%s start reason=%s force=%s",
                        slot.slot_id, reason, force,
                    )
                    try:
                        ok = lens_mod.warmup(
                            cookie_file=slot.cookie_file,
                            driver_factory=self._make_driver_factory(slot),
                            force=force,
                            raise_on_bot_challenge=True,
                        )
                    except lens_mod.LensBotChallengeError:
                        hit_challenge = True
                        challenge_cooldown = slot.mark_bot_challenge(self._clock())
                        ok = False
                    except Exception as exc:
                        logger.warning(
                            "glens auto-warmup: slot=%s unexpected error: %s",
                            slot.slot_id, exc,
                        )
                        ok = False
            finally:
                if mark_refreshing and skipped_outcome is None:
                    slot.set_refreshing(False)
                slot.refresh_lock.release()

            if skipped_outcome is not None:
                if skipped_outcome == "fresh" and slot.cookie_file == self.base_cookie_file:
                    self._sync_vercel_slots_from_base()
                outcomes[slot.slot_id] = skipped_outcome
                continue

            end_now = self._clock()
            duration_s = max(0.0, end_now - t0)
            synced = (
                self._sync_vercel_slots_from_base()
                if (ok and slot.cookie_file == self.base_cookie_file) else 0
            )

            with self._auto_warmup_lock:
                self._auto_warmup_runs += 1
                self._auto_warmup_last_run_at = end_now
                self._auto_warmup_last_ok = ok
                self._auto_warmup_last_reason = reason
                if ok:
                    self._auto_warmup_success += 1
                    self._slot_warmup_failures.pop(slot.slot_id, None)
                    self._slot_warmup_next_retry.pop(slot.slot_id, None)
                    backoff_s = 0.0
                    failures = 0
                else:
                    self._auto_warmup_failure += 1
                    failures = self._slot_warmup_failures.get(slot.slot_id, 0) + 1
                    self._slot_warmup_failures[slot.slot_id] = failures
                    backoff_s = min(
                        _AUTO_WARMUP_MAX_BACKOFF_SECONDS,
                        self.auto_warmup_retry_base * (2 ** (failures - 1)),
                    )
                    self._slot_warmup_next_retry[slot.slot_id] = end_now + backoff_s

            if ok:
                logger.info(
                    "glens auto-warmup: slot=%s ok=True reason=%s duration=%.2fs "
                    "synced_vercel=%d",
                    slot.slot_id, reason, duration_s, synced,
                )
                outcomes[slot.slot_id] = f"ok:{reason}"
            elif hit_challenge:
                logger.warning(
                    "glens auto-warmup: slot=%s ok=False reason=%s bot_challenge "
                    "duration=%.2fs slot_cooldown=%.0fs failures=%d next_retry_in=%.0fs",
                    slot.slot_id, reason, duration_s, challenge_cooldown, failures, backoff_s,
                )
                outcomes[slot.slot_id] = f"bot_challenge:{reason}"
            else:
                logger.warning(
                    "glens auto-warmup: slot=%s ok=False reason=%s duration=%.2fs "
                    "failures=%d next_retry_in=%.0fs",
                    slot.slot_id, reason, duration_s, failures, backoff_s,
                )
                outcomes[slot.slot_id] = f"failed:{reason}"

        return outcomes

    def _auto_warmup_loop(self) -> None:
        jitter = (
            random.uniform(0.0, self.auto_warmup_startup_jitter)
            if self.auto_warmup_startup_jitter > 0 else 0.0
        )
        if jitter > 0 and self._auto_warmup_stop.wait(jitter):
            return
        while not self._auto_warmup_stop.is_set():
            try:
                self.run_auto_warmup_once()
            except Exception as exc:
                logger.warning("glens auto-warmup: loop error: %s", exc)
            if self._auto_warmup_stop.wait(self.auto_warmup_check_interval):
                break

    def start_auto_warmup(self) -> bool:
        """Start the background proactive cookie warmup daemon thread."""
        with self._auto_warmup_lock:
            if not self.auto_warmup_enabled:
                return False
            if self._auto_warmup_thread is not None and self._auto_warmup_thread.is_alive():
                return True
            self._auto_warmup_stop.clear()
            thread = threading.Thread(
                target=self._auto_warmup_loop,
                name="glens-auto-warmup",
                daemon=True,
            )
            self._auto_warmup_thread = thread
            thread.start()
        logger.info(
            "glens auto-warmup: started (interval=%ds check=%ds jitter=%.1fs).",
            self.auto_warmup_interval,
            self.auto_warmup_check_interval,
            self.auto_warmup_startup_jitter,
        )
        return True

    def stop_auto_warmup(self, timeout: float = 2.0) -> None:
        """Signal the background warmup worker to stop and wait briefly."""
        with self._auto_warmup_lock:
            thread = self._auto_warmup_thread
            self._auto_warmup_stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._auto_warmup_lock:
            if self._auto_warmup_thread is thread and not (thread and thread.is_alive()):
                self._auto_warmup_thread = None

    def auto_warmup_status(self, now: float | None = None) -> dict:
        now = self._clock() if now is None else now
        with self._auto_warmup_lock:
            running = (
                self._auto_warmup_thread is not None and self._auto_warmup_thread.is_alive()
            )
            max_failures = max(self._slot_warmup_failures.values(), default=0)
            next_retry = max(
                (max(0.0, ts - now) for ts in self._slot_warmup_next_retry.values()),
                default=0.0,
            )
            return {
                "enabled": self.auto_warmup_enabled,
                "running": running,
                "interval_seconds": self.auto_warmup_interval,
                "check_interval_seconds": self.auto_warmup_check_interval,
                "runs_total": self._auto_warmup_runs,
                "success_total": self._auto_warmup_success,
                "failure_total": self._auto_warmup_failure,
                "consecutive_failures": max_failures,
                "last_run_at": self._auto_warmup_last_run_at,
                "last_ok": self._auto_warmup_last_ok,
                "last_reason": self._auto_warmup_last_reason,
                "next_retry_in_seconds": round(next_retry, 1),
            }

    def status(self) -> dict:
        now = self._clock()
        return {
            "proxy_fallback": self.proxy_fallback,
            "routes": [slot.status_dict(now) for slot in self.slots],
            "cache_stats": cache_mod.stats.snapshot(),
            "auto_warmup": self.auto_warmup_status(now),
        }
