#!/usr/bin/env python3
"""Sync DOCKERHUB.md to a Docker Hub repository's Overview (full_description).

Uses DOCKERHUB_USERNAME + DOCKERHUB_TOKEN/DOCKERHUB_PASSWORD when set, or
falls back to the local Docker credential helper configured in ~/.docker/config.json.
Never prints credentials or tokens.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SHORT_DESCRIPTION = (
    "Google Lens visual product search HTTP API with Vercel proxy failover, cache & logs."
)


def _read_docker_credentials() -> tuple[str | None, str | None]:
    user = os.getenv("DOCKERHUB_USERNAME", "").strip()
    secret = (os.getenv("DOCKERHUB_TOKEN") or os.getenv("DOCKERHUB_PASSWORD") or "").strip()
    if user and secret:
        return user, secret

    cfg_path = Path.home() / ".docker" / "config.json"
    if not cfg_path.exists():
        return None, None
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return None, None

    creds_store = cfg.get("credsStore")
    if not creds_store:
        return None, None

    helper = f"docker-credential-{creds_store}"
    for server in (
        "https://index.docker.io/v1/",
        "registry-1.docker.io",
        "index.docker.io",
        "docker.io",
    ):
        try:
            proc = subprocess.run(
                [helper, "get"],
                input=server,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout)
                u = (data.get("Username") or "").strip()
                s = (data.get("Secret") or "").strip()
                if u and s:
                    return u, s
        except Exception:
            continue
    return None, None


def _hub_login(username: str, secret: str) -> str:
    payload = json.dumps({"username": username, "password": secret}).encode("utf-8")
    req = Request(
        "https://hub.docker.com/v2/users/login/",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    token = data.get("token")
    if not token:
        raise RuntimeError("Docker Hub login response did not contain a JWT token.")
    return token


def _patch_readme(repo: str, token: str, full_description: str, description: str) -> None:
    payload = json.dumps({
        "description": description[:100],
        "full_description": full_description,
    }).encode("utf-8")
    req = Request(
        f"https://hub.docker.com/v2/repositories/{repo}/",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"JWT {token}",
        },
        method="PATCH",
    )
    with urlopen(req, timeout=20) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"Unexpected HTTP status {resp.status}")


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: sync-dockerhub-readme.py <namespace/repo> [readme_path]", file=sys.stderr)
        return 2

    repo = sys.argv[1].strip()
    readme_path = Path(sys.argv[2]) if len(sys.argv) >= 3 else Path("DOCKERHUB.md")
    if not readme_path.exists():
        print(f"Error: README file not found: {readme_path}", file=sys.stderr)
        return 2

    content = readme_path.read_text(encoding="utf-8")
    if len(content.encode("utf-8")) > 25000:
        print("Error: Docker Hub README exceeds 25,000 byte limit.", file=sys.stderr)
        return 2

    username, secret = _read_docker_credentials()
    if not username or not secret:
        print(
            "Could not load Docker Hub credentials automatically. "
            "Set DOCKERHUB_USERNAME and DOCKERHUB_TOKEN (PAT with Read & Write) "
            "or paste DOCKERHUB.md into Docker Hub -> Repository -> Overview.",
            file=sys.stderr,
        )
        return 1

    try:
        token = _hub_login(username, secret)
        _patch_readme(repo, token, content, SHORT_DESCRIPTION)
    except HTTPError as exc:
        print(f"Docker Hub API returned HTTP {exc.code} while updating {repo}.", file=sys.stderr)
        return 1
    except (URLError, RuntimeError) as exc:
        print(f"Failed to update Docker Hub README for {repo}: {exc}", file=sys.stderr)
        return 1

    print(f"Updated Docker Hub Overview for {repo} from {readme_path} ({len(content)} chars).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
