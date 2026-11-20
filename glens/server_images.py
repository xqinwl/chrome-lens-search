"""Bounded image intake for the HTTP service."""

from __future__ import annotations

import ipaddress
import socket
from io import BytesIO
from urllib.parse import urljoin

import certifi
import urllib3
from PIL import Image
from urllib3.util import parse_url

MAX_IMAGE_BYTES = 10 * 1024 * 1024
_REDIRECTS = {301, 302, 303, 307, 308}


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.headers = headers or {}


def too_large() -> APIError:
    return APIError(413, "image_too_large", "The maximum image size is 10 MiB.")


def validate_image(data: bytes) -> None:
    if len(data) > MAX_IMAGE_BYTES:
        raise too_large()
    try:
        with Image.open(BytesIO(data)) as picture:
            picture.verify()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise APIError(415, "invalid_image", "Expected a readable image file.") from exc


def _public_address(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    if isinstance(ip, ipaddress.IPv6Address):
        # Exclude scoped and transition addresses that can reach an embedded IPv4 target.
        if ip not in ipaddress.IPv6Network("2000::/3") or ip.sixtofour or ip.teredo:
            return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def _fetch(url: str) -> bytes | str:
    try:
        target = parse_url(url)
        if (target.scheme not in ("http", "https") or not target.host or target.auth is not None
                or "%" in target.host or "\\" in url or any(ord(c) < 32 for c in url)):
            raise ValueError
        host = target.host.strip("[]")
        port = target.port if target.port is not None else (443 if target.scheme == "https" else 80)
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses or not all(_public_address(info[4][0]) for info in addresses):
            raise ValueError
    except (ValueError, UnicodeError, urllib3.exceptions.LocationParseError) as exc:
        raise APIError(422, "invalid_image_url", "Expected a public HTTP(S) image URL.") from exc

    # Connect to the checked IP. Keep the original HTTP Host, TLS SNI and certificate identity.
    address = addresses[0][4][0]
    if target.scheme == "https":
        pool = urllib3.HTTPSConnectionPool(address, port, server_hostname=host,
                                           assert_hostname=host, cert_reqs="CERT_REQUIRED",
                                           ca_certs=certifi.where())
    else:
        pool = urllib3.HTTPConnectionPool(address, port)
    with pool as connection:
        with connection.urlopen(
            "GET", target.request_uri, headers={"Host": target.netloc}, preload_content=False,
            decode_content=False, redirect=False, retries=False,
            timeout=urllib3.Timeout(connect=5, read=15),
        ) as response:
            if response.status in _REDIRECTS and response.headers.get("Location"):
                return urljoin(url, response.headers["Location"])
            if not 200 <= response.status < 300:
                raise APIError(502, "image_download_failed", "The image host refused the download.")
            body = bytearray()
            for chunk in response.stream(65536, decode_content=False):
                if len(body) + len(chunk) > MAX_IMAGE_BYTES:
                    raise too_large()
                body.extend(chunk)
            return bytes(body)


def download_image(url: str) -> bytes:
    try:
        for _ in range(6):
            result = _fetch(url)
            if isinstance(result, bytes):
                return result
            url = result
    except (OSError, urllib3.exceptions.HTTPError) as exc:
        raise APIError(502, "image_download_failed", "Could not download the image.") from exc
    except ValueError as exc:  # e.g. malformed redirect authority
        raise APIError(422, "invalid_image_url", "Invalid image URL.") from exc
    raise APIError(502, "image_download_failed", "Too many image redirects.")
