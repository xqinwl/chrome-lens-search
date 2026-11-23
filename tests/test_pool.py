"""RoutePool failover, pacing, cooldowns, L2 dHash cache, single-flight, and server API."""

from __future__ import annotations

import json
import threading
import time
from io import BytesIO

import pytest

from glens import cache as cache_mod
from glens import lens as lens_mod
from glens.pool import RoutePool, RouteSlot

SYNTHETIC_ITEMS = [
    {"href": "https://a.example/p/1", "text": "Acme Widget Pro Runner"},
    {"href": "https://b.example/p/2", "text": "Acme Widget Pro Runner"},
    {"href": "https://c.example/p/3", "text": "Different Gadget Deluxe"},
]
RESULTS_URL = "https://www.google.com/search?udm=26&synthetic=1"


def _write_jar(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "user_agent": "Chrome/149.0.0.0",
        "cookies": [{"name": "SID", "value": "seed", "domain": ".google.com", "path": "/"}],
        "minted_at": time.time(),
    }), encoding="utf-8")
    return path


def _make_pool(tmp_path, *, proxy_fallback=True, vercel_rpm=20, local_rpm=15, clock=None):
    base_jar = _write_jar(tmp_path / "cookies.json")
    vercel_slot = RouteSlot(
        slot_id="vercel-0",
        kind="vercel",
        priority=0,
        cookie_file=tmp_path / "cookies-vercel-0.json",
        proxy_url="https://proxy.example/api/lens",
        proxy_secret="secret",
        rpm_limit=vercel_rpm,
        min_interval=0.0,
    )
    local_slot = RouteSlot(
        slot_id="local-0",
        kind="local",
        priority=10,
        cookie_file=base_jar,
        rpm_limit=local_rpm,
        min_interval=0.0,
    )
    return RoutePool(
        [vercel_slot, local_slot],
        base_cookie_file=base_jar,
        proxy_fallback=proxy_fallback,
        jitter_seconds=0.0,
        clock=clock,
        sleeper=lambda _: None,
    )


def test_route_pool_from_env_supports_multiple_vercel_and_local_proxies(monkeypatch, tmp_path):
    monkeypatch.setenv("GLENS_VERCEL_PROXY_URLS", "https://v1.example/api, https://v2.example/api")
    monkeypatch.setenv("GLENS_VERCEL_PROXY_SECRETS", "s1, s2")
    monkeypatch.setenv("GLENS_LOCAL_PROXIES", "http://127.0.0.1:7901, http://127.0.0.1:7902")
    monkeypatch.setenv("GLENS_PROXY_FALLBACK", "1")
    monkeypatch.setenv("GLENS_VERCEL_RPM", "12")
    monkeypatch.setenv("GLENS_LOCAL_RPM", "8")

    pool = RoutePool.from_env(base_cookie_file=tmp_path / "cookies.json")
    assert pool.proxy_fallback is True
    assert [(s.slot_id, s.kind, s.rpm_limit) for s in pool.slots] == [
        ("vercel-0", "vercel", 12),
        ("vercel-1", "vercel", 12),
        ("local-0", "local", 8),
        ("local-1", "local", 8),
    ]
    assert pool.slots[0].proxy_secret == "s1"
    assert pool.slots[1].proxy_secret == "s2"
    assert pool.slots[2].local_proxy == "http://127.0.0.1:7901"
    assert pool.slots[3].local_proxy == "http://127.0.0.1:7902"


def test_vercel_bot_challenge_automatically_fails_over_to_local(monkeypatch, tmp_path):
    pool = _make_pool(tmp_path, proxy_fallback=True)
    seen = []

    def fake_req(frame, cookie_file, lang=None, proxy=None, **kwargs):
        seen.append((proxy, cookie_file.name))
        if proxy == "vercel":
            raise lens_mod.LensBotChallengeError("https://www.google.com/sorry/index")
        return SYNTHETIC_ITEMS, RESULTS_URL

    monkeypatch.setattr(lens_mod, "_search_via_requests", fake_req)
    result, route = pool.search(b"img-1", proxy="vercel", use_cache=False)

    assert result is not None and result.top_title == "Acme Widget Pro Runner"
    assert route == "local-0"
    assert seen == [("vercel", "cookies-vercel-0.json"), (None, "cookies.json")]
    status = {r["slot_id"]: r for r in pool.status()["routes"]}
    assert status["vercel-0"]["state"] == "cooldown"
    assert status["vercel-0"]["cooldown_reason"] == "bot_challenge"
    assert status["local-0"]["state"] == "healthy"
    assert status["local-0"]["success_count"] == 1


def test_vercel_rpm_cap_spills_overflow_traffic_to_local(monkeypatch, tmp_path):
    pool = _make_pool(tmp_path, proxy_fallback=True, vercel_rpm=1, local_rpm=10)
    monkeypatch.setattr(
        lens_mod, "_search_via_requests",
        lambda *a, **kw: (SYNTHETIC_ITEMS, RESULTS_URL),
    )

    _, route1 = pool.search(b"img-1", proxy="vercel", use_cache=False)
    _, route2 = pool.search(b"img-2", proxy="vercel", use_cache=False)

    assert route1 == "vercel-0"
    assert route2 == "local-0"


def test_strict_proxy_mode_does_not_fail_over_to_local(monkeypatch, tmp_path):
    pool = _make_pool(tmp_path, proxy_fallback=False)
    calls = []

    def fake_req(frame, cookie_file, lang=None, proxy=None, **kwargs):
        calls.append(proxy)
        raise lens_mod.LensBotChallengeError("https://www.google.com/sorry/index")

    monkeypatch.setattr(lens_mod, "_search_via_requests", fake_req)
    with pytest.raises(lens_mod.LensBotChallengeError):
        pool.search(b"img-strict", proxy="vercel", use_cache=False)

    assert calls == ["vercel"]


def test_all_slots_in_bot_challenge_cooldown_short_circuits_immediately(monkeypatch, tmp_path):
    now = [1000.0]
    pool = _make_pool(tmp_path, proxy_fallback=True, clock=lambda: now[0])
    calls = []

    def always_sorry(*args, **kwargs):
        calls.append(1)
        raise lens_mod.LensBotChallengeError("https://www.google.com/sorry/index")

    monkeypatch.setattr(lens_mod, "_search_via_requests", always_sorry)

    with pytest.raises(lens_mod.LensBotChallengeError):
        pool.search(b"img-1", proxy="vercel", use_cache=False)
    assert len(calls) == 2  # tried vercel-0 then local-0

    # Next request must short-circuit without touching upstream at all.
    with pytest.raises(lens_mod.LensBotChallengeError) as exc:
        pool.search(b"img-2", proxy="vercel", use_cache=False)
    assert len(calls) == 2
    assert getattr(exc.value, "retry_after", None) is not None


def test_proxy_search_hits_l1_and_l2_dhash_cache(monkeypatch, tmp_path):
    PIL = pytest.importorskip("PIL")
    from PIL import Image

    assert PIL is not None
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("GLENS_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("GLENS_CACHE_DHASH_THRESHOLD", "4")
    cache_mod.stats.reset()

    # Build a patterned image and a re-encoded slightly-resized copy (different SHA-256, same dHash)
    img = Image.new("RGB", (200, 200))
    for y in range(200):
        for x in range(200):
            img.putpixel((x, y), ((x * 255) // 200, (y * 255) // 200, 128))
    buf1 = BytesIO()
    img.save(buf1, format="PNG")
    bytes_v1 = buf1.getvalue()

    buf2 = BytesIO()
    img.resize((180, 180)).save(buf2, format="JPEG", quality=80)
    bytes_v2 = buf2.getvalue()
    assert bytes_v1 != bytes_v2

    pool = _make_pool(tmp_path, proxy_fallback=True)
    upstream_calls = []

    def fake_req(*args, **kwargs):
        upstream_calls.append(1)
        return SYNTHETIC_ITEMS, RESULTS_URL

    monkeypatch.setattr(lens_mod, "_search_via_requests", fake_req)

    res1, route1 = pool.search(bytes_v1, proxy="vercel", use_cache=True)
    res2, route2 = pool.search(bytes_v1, proxy="vercel", use_cache=True)
    res3, route3 = pool.search(bytes_v2, proxy="vercel", use_cache=True)

    assert len(upstream_calls) == 1
    assert res1 is not None and route1 == "vercel-0"
    assert res2 is not None and route2 == "cache-l1"
    assert res3 is not None and route3 == "cache-l2"
    snap = cache_mod.stats.snapshot()
    assert snap["l1_hits"] == 1
    assert snap["l2_hits"] == 1


def test_concurrent_identical_requests_coalesce_single_flight(monkeypatch, tmp_path):
    pool = _make_pool(tmp_path, proxy_fallback=True)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow_req(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(timeout=5)
        return SYNTHETIC_ITEMS, RESULTS_URL

    monkeypatch.setattr(lens_mod, "_search_via_requests", slow_req)
    results = []

    def worker():
        results.append(pool.search(b"same-image", proxy="vercel", use_cache=False))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    assert entered.wait(timeout=3)
    t2.start()
    time.sleep(0.05)
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(calls) == 1
    assert len(results) == 2
    assert all(r is not None and r.top_title == "Acme Widget Pro Runner" for r, _ in results)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_server_exposes_route_header_bot_challenge_503_pool_status_and_warmup(
    monkeypatch, tmp_path,
):
    pytest.importorskip("fastapi")
    import httpx

    from glens import server

    pool = _make_pool(tmp_path, proxy_fallback=True)
    server.reset_pool(pool)
    monkeypatch.setattr(server, "download_image", lambda _url: lens_mod._tiny_png())
    monkeypatch.setattr(lens_mod, "DEFAULT_USE_CACHE", False)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.create_app()),
            base_url="http://test",
        ) as client:
            # 1) Vercel hits /sorry -> automatic failover to local-0 -> HTTP 200 + X-Glens-Route
            def vercel_sorry_local_ok(frame, cookie_file, lang=None, proxy=None, **kw):
                if proxy == "vercel":
                    raise lens_mod.LensBotChallengeError("https://www.google.com/sorry/index")
                return SYNTHETIC_ITEMS, RESULTS_URL

            monkeypatch.setattr(lens_mod, "_search_via_requests", vercel_sorry_local_ok)
            ok = await client.post(
                "/v1/search", json={"image_url": "https://img.example/a.png", "proxy": "vercel"}
            )
            assert ok.status_code == 200
            assert ok.headers["x-glens-route"] == "local-0"

            # 2) /v1/pool/status shows vercel-0 in cooldown and local-0 healthy
            ps = await client.get("/v1/pool/status")
            assert ps.status_code == 200
            body = ps.json()
            assert body["proxy_fallback"] is True
            by_id = {r["slot_id"]: r for r in body["routes"]}
            assert by_id["vercel-0"]["state"] == "cooldown"
            assert by_id["local-0"]["state"] == "healthy"

            # 3) Local also hits /sorry -> HTTP 503 upstream_bot_challenge + Retry-After
            def all_sorry(*a, **kw):
                raise lens_mod.LensBotChallengeError("https://www.google.com/sorry/index")

            monkeypatch.setattr(lens_mod, "_search_via_requests", all_sorry)
            blocked = await client.post(
                "/v1/search", json={"image_url": "https://img.example/b.png", "proxy": "vercel"}
            )
            assert blocked.status_code == 503
            assert blocked.json()["error"]["code"] == "upstream_bot_challenge"
            assert "retry-after" in blocked.headers

            # 4) POST /v1/warmup resets cooldowns and refreshes cookies
            monkeypatch.setattr(lens_mod, "warmup", lambda **kw: True)
            warmed = await client.post("/v1/warmup", json={"reset_cooldown": True, "force": False})
            assert warmed.status_code == 200
            assert warmed.json()["slots"]["local-0"]["state"] == "healthy"
            assert warmed.json()["slots"]["vercel-0"]["state"] == "healthy"
            assert "auto_warmup" in ps.json()
    finally:
        server.reset_pool(None)


def _write_dated_jar(path, minted_at: float, token: str = "v1") -> None:
    import json

    path.write_text(
        json.dumps({
            "user_agent": "UA",
            "cookies": [{"name": "NID", "value": token, "domain": ".google.com", "path": "/"}],
            "minted_at": minted_at,
        }),
        encoding="utf-8",
    )


def test_auto_warmup_cold_start_mints_and_syncs_vercel(monkeypatch, tmp_path):
    now = [10_000.0]
    pool = _make_pool(tmp_path, clock=lambda: now[0])
    pool.slots[1].cookie_file.unlink(missing_ok=True)
    pool.slots[0].cookie_file.unlink(missing_ok=True)
    pool.auto_warmup_enabled = True
    pool.auto_warmup_interval = 3600

    # Vercel slot previously hit proxy_cookie_missing cooldown
    pool.slots[0].mark_transient_error(duration=30.0, now=now[0], reason="proxy_cookie_missing")
    assert pool.slots[0].effective_state(now[0]) == "cooldown"

    seen_states = []

    def fake_warmup(*, cookie_file, driver_factory, force, **_kw):
        assert force is False
        # Cold start marks local-0 as refreshing so live requests ride it
        seen_states.append(pool.slots[1].effective_state(now[0]))
        _write_dated_jar(cookie_file, now[0], token="fresh-cold")
        return True

    monkeypatch.setattr(lens_mod, "warmup", fake_warmup)

    outcomes = pool.run_auto_warmup_once()
    assert outcomes == {"local-0": "ok:cookie_missing"}
    assert seen_states == ["refreshing"]
    assert pool.slots[1].effective_state(now[0]) == "healthy"
    # Vercel slot got atomically synced AND its proxy_cookie_missing cooldown cleared
    assert pool.slots[0].effective_state(now[0]) == "healthy"
    synced_jar = lens_mod._load_cookie_jar(pool.slots[0].cookie_file)
    assert synced_jar is not None
    assert synced_jar["cookies"][0]["value"] == "fresh-cold"
    aw = pool.status()["auto_warmup"]
    assert aw["runs_total"] == 1
    assert aw["success_total"] == 1
    assert aw["last_ok"] is True
    assert aw["last_reason"] == "cookie_missing"


def test_auto_warmup_proactive_stale_refresh_keeps_slot_healthy_and_swaps_atomically(
    monkeypatch, tmp_path,
):
    now = [20_000.0]
    pool = _make_pool(tmp_path, clock=lambda: now[0])
    pool.auto_warmup_enabled = True
    pool.auto_warmup_interval = 3600

    # Seed fresh jar -> should be a no-op
    _write_dated_jar(pool.slots[1].cookie_file, now[0] - 600.0, token="old-tok")
    _write_dated_jar(pool.slots[0].cookie_file, now[0] - 600.0, token="old-tok")

    calls = []
    monkeypatch.setattr(lens_mod, "warmup", lambda **kw: calls.append(kw) or True)
    assert pool.run_auto_warmup_once() == {"local-0": "fresh"}
    assert calls == []

    # Advance past interval -> proactive renewal runs WITHOUT blocking live traffic
    now[0] += 4000.0
    states_during_refresh = []

    def fake_proactive_warmup(*, cookie_file, driver_factory, force, **_kw):
        assert force is True
        # Must remain healthy so incoming req/vercel traffic continues uninterrupted
        states_during_refresh.append(pool.slots[1].effective_state(now[0]))
        _write_dated_jar(cookie_file, now[0], token="new-tok")
        return True

    monkeypatch.setattr(lens_mod, "warmup", fake_proactive_warmup)
    outcomes = pool.run_auto_warmup_once()
    assert outcomes == {"local-0": "ok:cookie_stale"}
    assert states_during_refresh == ["healthy"]
    assert lens_mod._load_cookie_jar(pool.slots[1].cookie_file)["cookies"][0]["value"] == "new-tok"
    assert lens_mod._load_cookie_jar(pool.slots[0].cookie_file)["cookies"][0]["value"] == "new-tok"


def test_auto_warmup_respects_cooldown_and_exponential_backoff(monkeypatch, tmp_path):
    now = [30_000.0]
    pool = _make_pool(tmp_path, clock=lambda: now[0])
    pool.slots[1].cookie_file.unlink(missing_ok=True)
    pool.slots[0].cookie_file.unlink(missing_ok=True)
    pool.auto_warmup_enabled = True
    pool.auto_warmup_retry_base = 60.0

    # 1) Slot in bot_challenge cooldown -> skipped without touching Chromium
    pool.slots[1].mark_bot_challenge(now[0])
    called = []
    monkeypatch.setattr(lens_mod, "warmup", lambda **kw: called.append(1) or False)
    assert pool.run_auto_warmup_once() == {"local-0": "skipped_cooldown"}
    assert called == []

    # 2) Cooldown expires, warmup fails -> exponential backoff (60s, then 120s)
    pool.slots[1].reset_cooldown()
    assert pool.run_auto_warmup_once() == {"local-0": "failed:cookie_missing"}
    assert len(called) == 1
    assert pool.run_auto_warmup_once() == {"local-0": "skipped_backoff"}
    assert len(called) == 1

    now[0] += 61.0
    assert pool.run_auto_warmup_once() == {"local-0": "failed:cookie_missing"}
    assert len(called) == 2
    assert pool.auto_warmup_status(now[0])["consecutive_failures"] == 2
    assert pool.auto_warmup_status(now[0])["next_retry_in_seconds"] == pytest.approx(120.0, abs=1.0)


def test_auto_warmup_background_thread_start_and_stop(monkeypatch, tmp_path):
    ran = threading.Event()
    pool = _make_pool(tmp_path)
    pool.slots[1].cookie_file.unlink(missing_ok=True)
    pool.slots[0].cookie_file.unlink(missing_ok=True)
    pool.auto_warmup_enabled = True
    pool.auto_warmup_startup_jitter = 0.0
    pool.auto_warmup_check_interval = 60

    def fake_warmup(*, cookie_file, driver_factory, force, **_kw):
        _write_dated_jar(cookie_file, time.time(), token="bg-tok")
        ran.set()
        return True

    monkeypatch.setattr(lens_mod, "warmup", fake_warmup)
    try:
        assert pool.start_auto_warmup() is True
        assert ran.wait(timeout=3.0)
        deadline = time.time() + 3.0
        while pool.auto_warmup_status()["runs_total"] < 1 and time.time() < deadline:
            time.sleep(0.01)
        assert pool.auto_warmup_status()["running"] is True
        assert pool.auto_warmup_status()["success_total"] == 1
        assert lens_mod._load_cookie_jar(pool.slots[0].cookie_file) is not None
    finally:
        pool.stop_auto_warmup(timeout=2.0)
    assert pool.auto_warmup_status()["running"] is False


def test_auto_warmup_fresh_poll_preserves_vercel_lookup_failed_and_bot_challenge_cooldowns(
    monkeypatch, tmp_path,
):
    now = [40_000.0]
    pool = _make_pool(tmp_path, clock=lambda: now[0])
    pool.auto_warmup_enabled = True
    pool.auto_warmup_interval = 3600

    # Both jars already exist and are fresh -> no new mint happens
    _write_dated_jar(pool.slots[1].cookie_file, now[0] - 100.0, token="same-tok")
    _write_dated_jar(pool.slots[0].cookie_file, now[0] - 100.0, token="same-tok")

    pool.slots[0].mark_transient_error(duration=60.0, now=now[0], reason="proxy_lookup_failed")
    assert pool.run_auto_warmup_once() == {"local-0": "fresh"}
    # Cooldown must NOT be prematurely cleared because no newer jar was synced
    assert pool.slots[0].effective_state(now[0]) == "cooldown"
    assert pool.slots[0].cooldown_reason == "proxy_lookup_failed"

    # Bot challenge cooldown is also preserved even when a newer jar IS synced
    pool.slots[0].mark_bot_challenge(now[0])
    _write_dated_jar(pool.slots[1].cookie_file, now[0], token="brand-new")
    assert pool.run_auto_warmup_once() == {"local-0": "fresh"}
    assert pool.slots[0].effective_state(now[0]) == "cooldown"
    assert pool.slots[0].cooldown_reason == "bot_challenge"


def test_auto_warmup_bot_challenge_puts_local_slot_in_cooldown(monkeypatch, tmp_path):
    now = [50_000.0]
    pool = _make_pool(tmp_path, clock=lambda: now[0])
    pool.slots[1].cookie_file.unlink(missing_ok=True)
    pool.auto_warmup_enabled = True

    def sorry_warmup(**kw):
        assert kw.get("raise_on_bot_challenge") is True
        raise lens_mod.LensBotChallengeError("https://www.google.com/sorry/index")

    monkeypatch.setattr(lens_mod, "warmup", sorry_warmup)
    outcomes = pool.run_auto_warmup_once()
    assert outcomes == {"local-0": "bot_challenge:cookie_missing"}
    assert pool.slots[1].effective_state(now[0]) == "cooldown"
    assert pool.slots[1].cooldown_reason == "bot_challenge"


def test_auto_warmup_double_checks_freshness_under_refresh_lock(monkeypatch, tmp_path):
    now = [60_000.0]
    pool = _make_pool(tmp_path, clock=lambda: now[0])
    pool.auto_warmup_enabled = True
    pool.auto_warmup_interval = 3600

    # Stale jar initially
    _write_dated_jar(pool.slots[1].cookie_file, now[0] - 5000.0, token="stale")

    # Simulate a concurrent request refreshing the jar right before refresh_lock is acquired
    orig_lock = pool.slots[1].refresh_lock

    class InterceptLock:
        def acquire(self, blocking=True):
            _write_dated_jar(pool.slots[1].cookie_file, now[0], token="just-refreshed")
            return orig_lock.acquire(blocking=blocking)

        def release(self):
            orig_lock.release()

    pool.slots[1].refresh_lock = InterceptLock()  # type: ignore[assignment]
    called = []
    monkeypatch.setattr(lens_mod, "warmup", lambda **kw: called.append(1) or True)

    assert pool.run_auto_warmup_once() == {"local-0": "fresh"}
    assert called == []


@pytest.mark.anyio
async def test_server_lifespan_starts_and_stops_auto_warmup(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from glens import server

    ran = threading.Event()
    pool = _make_pool(tmp_path)
    pool.slots[1].cookie_file.unlink(missing_ok=True)
    pool.slots[0].cookie_file.unlink(missing_ok=True)
    pool.auto_warmup_enabled = True
    pool.auto_warmup_startup_jitter = 0.0
    pool.auto_warmup_check_interval = 60

    def fake_warmup(*, cookie_file, driver_factory, force, **_kw):
        _write_dated_jar(cookie_file, time.time(), token="lifespan-tok")
        ran.set()
        return True

    monkeypatch.setattr(lens_mod, "warmup", fake_warmup)
    server.reset_pool(pool)
    app = server.create_app()
    try:
        async with app.router.lifespan_context(app):
            assert ran.wait(timeout=3.0)
            assert pool.auto_warmup_status()["running"] is True
        assert pool.auto_warmup_status()["running"] is False
    finally:
        server.reset_pool(None)
