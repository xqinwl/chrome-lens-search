import logging

import pytest

from glens import lens
from glens.logging_setup import _HANDLER_TAG, configure_logging


def _cleanup_glens_logger():
    target = logging.getLogger("glens")
    for handler in list(target.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            target.removeHandler(handler)
            handler.close()
    target.propagate = True


def test_configure_logging_writes_rotating_utc_file_and_is_idempotent(tmp_path):
    log_path = tmp_path / "logs" / "glens.log"
    try:
        log = configure_logging(level="INFO", log_file=log_path, max_bytes=4096, backup_count=2)
        # Re-configuring must replace managed handlers rather than duplicating lines.
        log = configure_logging(level="INFO", log_file=log_path, max_bytes=4096, backup_count=2)
        log.info("hello-production-audit")
        for handler in log.handlers:
            handler.flush()

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert "INFO [glens] hello-production-audit" in lines[0]
        assert lines[0].split()[0].endswith("Z")
    finally:
        _cleanup_glens_logger()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_server_emits_request_id_and_sanitized_logs(monkeypatch, caplog):
    pytest.importorskip("fastapi")
    import httpx

    from glens import server

    image = lens._tiny_png()
    monkeypatch.setattr(server, "download_image", lambda url: image)
    monkeypatch.setattr(
        server,
        "_search_bytes",
        lambda frame, **opts: lens.LensResult(
            "Chair", ["Chair"], [lens.Match("Chair", "https://shop.example/1", "shop.example")]
        ),
    )

    caplog.set_level(logging.INFO, logger="glens")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.create_app()),
        base_url="http://test",
    ) as client:
        # Client-supplied X-Request-Id is echoed and URL query tokens are stripped from logs.
        resp = await client.post(
            "/v1/search",
            headers={"X-Request-Id": "req-abc-123"},
            json={"image_url": "https://cdn.example.com/item.webp?signature=supersecret"},
        )
        assert resp.status_code == 200
        assert resp.headers["x-request-id"] == "req-abc-123"

        # Invalid request still returns an X-Request-Id and logs a warning.
        bad = await client.post("/v1/search", json={"image_url": ""})
        assert bad.status_code == 422
        assert bad.headers.get("x-request-id")

    combined = "\n".join(r.getMessage() for r in caplog.records)
    assert "req_id=req-abc-123" in combined
    assert "status=200" in combined
    assert "input=https://cdn.example.com" in combined
    assert "supersecret" not in combined
    assert "status=422" in combined
