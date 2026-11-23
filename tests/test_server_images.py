import socket
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

pytest.importorskip("PIL")

from glens import server_images as images  # noqa: E402


@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.1.5", "169.254.169.254", "224.0.0.2",
                                     "::1", "::ffff:127.0.0.1", "64:ff9b::7f00:1"])
def test_private_targets(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(0, 0, 0, "", (address, 80))])
    with pytest.raises(images.APIError) as exc:
        images.download_image("https://images.example/test")
    assert exc.value.status == 422


def test_checks_actual_peer_tls_identity_and_redirect(monkeypatch):
    hosts = []
    requests = []

    def resolve(host, port, **kw):
        hosts.append(host)
        ip = "93.184.216.34" if host == "images.example" else "127.0.0.1"
        return [(0, 0, 0, "", (ip, port))]

    def tls_pool(address, port, **kw):
        assert (address, port) == ("93.184.216.34", 443)
        assert kw["server_hostname"] == kw["assert_hostname"] == "images.example"

        def get(method, path, **kw):
            requests.append((method, path, kw["headers"]["Host"]))
            assert kw["redirect"] is False
            return nullcontext(SimpleNamespace(status=302, headers={"Location": "http://local/a"}))
        return nullcontext(SimpleNamespace(urlopen=get))

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(images.urllib3, "HTTPSConnectionPool", tls_pool)
    with pytest.raises(images.APIError) as exc:
        images.download_image("https://images.example/a?q=1")
    assert exc.value.status == 422
    assert hosts == ["images.example", "local"]
    assert requests == [("GET", "/a?q=1", "images.example")]


def test_remote_size_without_content_length(monkeypatch):
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 3)
    response = SimpleNamespace(status=200, headers={}, stream=lambda *a, **kw: iter([b"12", b"34"]))
    pool = SimpleNamespace(urlopen=lambda *a, **kw: nullcontext(response))
    monkeypatch.setattr(images.urllib3, "HTTPConnectionPool", lambda *a: nullcontext(pool))
    with pytest.raises(images.APIError) as exc:
        images.download_image("http://8.8.8.8/a")
    assert exc.value.status == 413


def test_dns_failure(monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(images.APIError) as exc:
        images.download_image("https://example.com/a")
    assert exc.value.status == 502
    assert exc.value.code == "image_download_failed"


def test_compress_image_for_proxy_keeps_payload_within_budget():
    import os
    from io import BytesIO

    from PIL import Image

    raw = Image.frombytes("RGB", (2000, 2000), os.urandom(12_000_000))
    output = BytesIO()
    raw.save(output, format="PNG")
    source = output.getvalue()
    assert len(source) > images.MAX_PROXY_IMAGE_BYTES

    compressed = images.compress_image_for_proxy(source)
    assert len(compressed) <= images.MAX_PROXY_IMAGE_BYTES
    with Image.open(BytesIO(compressed)) as result:
        assert result.format == "JPEG"
