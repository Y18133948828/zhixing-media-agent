"""Refresh the pinned Swagger UI assets. No Node runtime is needed by the server."""

import base64
import hashlib
import io
import tarfile
from pathlib import Path

import httpx

VERSION = "5.34.0"
INTEGRITY = "H6tAIjr3XqE31PbkZl0OFya1ziRLDfTwh3J6OHdumPtPCLfjTnyLeagl/55kFNZcTOz9a9QI9WP0FXldwiGJag=="
URL = f"https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-{VERSION}.tgz"
response = httpx.get(URL, timeout=60)
response.raise_for_status()
assert base64.b64encode(hashlib.sha512(response.content).digest()).decode() == INTEGRITY
target = Path(__file__).resolve().parents[1] / "app/api/static/docs"
target.mkdir(parents=True, exist_ok=True)
with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
    for name in ("swagger-ui-bundle.js", "swagger-ui.css", "favicon-32x32.png", "LICENSE", "NOTICE"):
        try:
            member = archive.extractfile(f"package/{name}")
        except KeyError:
            continue
        (target / name).write_bytes(member.read())
(target / "SOURCE.txt").write_text(
    f"Swagger UI {VERSION}\n{URL}\nsha512-{INTEGRITY}\nRepository: https://github.com/swagger-api/swagger-ui\n",
    encoding="utf-8",
)
print(f"Swagger UI {VERSION} local assets ready")
