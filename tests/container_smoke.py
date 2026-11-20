"""Run inside a started service container on each target architecture."""

import json
import os
import tempfile
from urllib.request import urlopen

from glens.driver import build_driver

assert os.getuid() != 0
with tempfile.TemporaryFile(dir="/data") as probe:
    probe.write(b"ok")

with urlopen("http://127.0.0.1:8000/healthz", timeout=3) as response:
    assert json.load(response) == {"ok": True}

driver = build_driver()
try:
    driver.get("data:text/html,<title>glens smoke test</title>")
    assert driver.title == "glens smoke test"
finally:
    driver.quit()
