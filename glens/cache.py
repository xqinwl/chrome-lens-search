"""Two-level local result cache (SHA-256 + dHash) and single-flight deduplication."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any

logger = logging.getLogger("glens")

_DHASH_INDEX_NAME = ".dhash.idx"
_index_lock = threading.Lock()
_evict_lock = threading.Lock()
_dhash_indexes: dict[Path, dict[str, dict[str, Any]]] = {}
_write_counters: dict[Path, int] = {}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def default_cache_dir() -> Path:
    env = os.getenv("GLENS_CACHE_DIR", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".cache" / "glens" / "results"


def dhash_threshold() -> int:
    """Max Hamming distance for L2 perceptual-hash hits (0 disables L2)."""
    return _env_int("GLENS_CACHE_DHASH_THRESHOLD", 4, minimum=0)


def max_cache_entries() -> int:
    """Upper bound on cached result entries on disk (0 disables eviction)."""
    return _env_int("GLENS_CACHE_MAX_ENTRIES", 10000, minimum=0)


class CacheStats:
    """Thread-safe counters for observability on /v1/pool/status."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.l1_hits = 0
        self.l2_hits = 0
        self.misses = 0
        self.inflight_coalesced = 0

    def record_l1_hit(self) -> None:
        with self._lock:
            self.l1_hits += 1

    def record_l2_hit(self) -> None:
        with self._lock:
            self.l2_hits += 1

    def record_miss(self) -> None:
        with self._lock:
            self.misses += 1

    def record_coalesced(self) -> None:
        with self._lock:
            self.inflight_coalesced += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "l1_hits": self.l1_hits,
                "l2_hits": self.l2_hits,
                "misses": self.misses,
                "inflight_coalesced": self.inflight_coalesced,
            }

    def reset(self) -> None:
        with self._lock:
            self.l1_hits = 0
            self.l2_hits = 0
            self.misses = 0
            self.inflight_coalesced = 0


stats = CacheStats()


def cache_key(frame_bytes: bytes, lang: str | None) -> str:
    digest = hashlib.sha256(frame_bytes)
    digest.update(f"|{lang or ''}".encode())
    return digest.hexdigest()


def compute_dhash(frame_bytes: bytes) -> int | None:
    """Compute a 64-bit horizontal difference hash using Pillow, if available."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(BytesIO(frame_bytes)) as src:
            img = ImageOps.exif_transpose(src).convert("L")
            resized = img.resize((9, 8), Image.Resampling.LANCZOS)
            pixels = resized.tobytes()
    except Exception:
        return None
    bits = 0
    for row in range(8):
        offset = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if pixels[offset + col] > pixels[offset + col + 1] else 0)
    return bits


def hamming_distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _discard(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _load_index_locked(cache_dir: Path) -> dict[str, dict[str, Any]]:
    index = _dhash_indexes.get(cache_dir)
    if index is not None:
        return index
    idx_path = cache_dir / _DHASH_INDEX_NAME
    loaded: dict[str, dict[str, Any]] = {}
    if idx_path.exists():
        try:
            raw = json.loads(idx_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key, entry in raw.items():
                    if (
                        isinstance(key, str)
                        and isinstance(entry, dict)
                        and isinstance(entry.get("dhash"), int)
                        and isinstance(entry.get("lang"), str)
                    ):
                        loaded[key] = entry
        except Exception:
            loaded = {}
    _dhash_indexes[cache_dir] = loaded
    return loaded


def _save_index_locked(cache_dir: Path, index: dict[str, dict[str, Any]]) -> None:
    tmp: Path | None = None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        idx_path = cache_dir / _DHASH_INDEX_NAME
        tmp = idx_path.with_name(
            f"{_DHASH_INDEX_NAME}.{os.getpid()}.{threading.get_ident()}.part"
        )
        tmp.write_text(json.dumps(index), encoding="utf-8")
        tmp.replace(idx_path)
        tmp = None
    except Exception as exc:
        logger.info("glens: could not write dHash cache index (%s)", exc)
        _discard(tmp)


def _evict_if_needed(cache_dir: Path) -> None:
    limit = max_cache_entries()
    if limit <= 0:
        return
    with _evict_lock:
        count = _write_counters.get(cache_dir, 0) + 1
        _write_counters[cache_dir] = count
        # Check disk count periodically (or immediately for small test limits).
        check_every = min(50, max(1, limit // 2))
        if count % check_every != 0:
            return
        try:
            entries = list(cache_dir.glob("*.json"))
        except OSError:
            return
        if len(entries) <= limit:
            return
        try:
            entries.sort(key=lambda p: p.stat().st_mtime_ns)
        except OSError:
            return
        to_remove = entries[: len(entries) - limit]
        removed_keys: set[str] = set()
        for path in to_remove:
            try:
                path.unlink(missing_ok=True)
                removed_keys.add(path.stem)
            except OSError:
                continue
        if removed_keys:
            with _index_lock:
                index = _load_index_locked(cache_dir)
                changed = False
                for key in removed_keys:
                    if key in index:
                        del index[key]
                        changed = True
                if changed:
                    _save_index_locked(cache_dir, index)


def cache_read(
    key: str,
    ttl: int,
    *,
    cache_dir: Path | None = None,
) -> tuple[list[dict], str | None] | None:
    try:
        directory = cache_dir or default_cache_dir()
        path = directory / f"{key}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        saved_at = float(data.get("saved_at", 0))
        if not math.isfinite(saved_at) or time.time() - saved_at > ttl:
            return None
        items = data.get("items")
        if not isinstance(items, list) or not items or any(
            not isinstance(item, dict)
            or not isinstance(item.get("href"), str)
            or not isinstance(item.get("text"), str)
            for item in items
        ):
            return None
        results_url = data.get("results_url")
        if results_url is not None and not isinstance(results_url, str):
            return None
        logger.info("glens: result cache hit (%s...)", key[:12])
        return items, results_url
    except Exception:
        return None


def cache_read_dhash(
    dhash: int | None,
    lang: str | None,
    ttl: int,
    *,
    threshold: int | None = None,
    cache_dir: Path | None = None,
) -> tuple[list[dict], str | None] | None:
    """Search L2 perceptual-hash index for a near-duplicate cached image."""
    if dhash is None:
        return None
    max_dist = dhash_threshold() if threshold is None else threshold
    if max_dist <= 0:
        return None
    directory = cache_dir or default_cache_dir()
    lang_norm = lang or ""
    now = time.time()
    best_key: str | None = None
    best_rank: tuple[int, float] | None = None
    stale_keys: list[str] = []

    with _index_lock:
        index = _load_index_locked(directory)
        for key, entry in list(index.items()):
            saved_at = float(entry.get("saved_at", 0))
            if not math.isfinite(saved_at) or now - saved_at > ttl:
                stale_keys.append(key)
                continue
            if entry.get("lang", "") != lang_norm:
                continue
            dist = hamming_distance(dhash, int(entry["dhash"]))
            if dist <= max_dist:
                rank = (dist, -saved_at)
                if best_rank is None or rank < best_rank:
                    best_rank = rank
                    best_key = key
        if stale_keys:
            for key in stale_keys:
                index.pop(key, None)
            _save_index_locked(directory, index)

    if best_key is None:
        return None
    hit = cache_read(best_key, ttl, cache_dir=directory)
    if hit is None:
        with _index_lock:
            index = _load_index_locked(directory)
            if best_key in index:
                del index[best_key]
                _save_index_locked(directory, index)
        return None
    logger.info("glens: L2 dHash cache hit (%s..., dist=%d)", best_key[:12], best_rank[0])
    return hit


def cache_write(
    key: str,
    items: list[dict],
    results_url: str | None,
    *,
    dhash: int | None = None,
    lang: str | None = None,
    cache_dir: Path | None = None,
) -> None:
    tmp: Path | None = None
    directory = cache_dir or default_cache_dir()
    now = time.time()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f"{key}.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_text(
            json.dumps({"saved_at": now, "results_url": results_url, "items": items}),
            encoding="utf-8",
        )
        tmp.replace(directory / f"{key}.json")
        tmp = None
    except Exception as exc:
        logger.info("glens: could not write the result cache (%s)", exc)
        return
    finally:
        _discard(tmp)

    if dhash is not None:
        with _index_lock:
            index = _load_index_locked(directory)
            index[key] = {"dhash": dhash, "lang": lang or "", "saved_at": now}
            _save_index_locked(directory, index)

    _evict_if_needed(directory)


class _FlightSlot:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: Any = None
        self.exc: BaseException | None = None

    def finish(self, value: Any = None, exc: BaseException | None = None) -> None:
        self.value = value
        self.exc = exc
        self.event.set()

    def wait(self, timeout: float | None = None) -> Any:
        self.event.wait(timeout)
        if self.exc is not None:
            raise self.exc
        return self.value


class InFlightCoordinator:
    """Coalesce concurrent identical lookups so only one request hits upstream."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flights: dict[str, _FlightSlot] = {}

    def run(self, key: str, fn):
        with self._lock:
            existing = self._flights.get(key)
            if existing is not None:
                is_leader = False
                slot = existing
            else:
                is_leader = True
                slot = _FlightSlot()
                self._flights[key] = slot

        if not is_leader:
            stats.record_coalesced()
            return slot.wait()

        try:
            value = fn()
            slot.finish(value=value)
            return value
        except BaseException as exc:
            slot.finish(exc=exc)
            raise
        finally:
            with self._lock:
                if self._flights.get(key) is slot:
                    del self._flights[key]
