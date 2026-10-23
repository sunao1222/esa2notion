"""Download public or esa-secured images without forwarding API credentials."""

from __future__ import annotations

import mimetypes
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import requests

from .errors import MigrationError
from .http import HTTPClient, json_object

ESA_API_BASE_URL = "https://api.esa.io/v1"
SECURE_HOSTS = frozenset({"files.esa.io", "dl.esa.io"})
SUPPORTED_IMAGE_TYPES = frozenset({
    "image/gif", "image/heic", "image/jpeg", "image/png", "image/svg+xml",
    "image/tiff", "image/webp", "image/vnd.microsoft.icon", "image/bmp",
    "image/avif", "image/apng",
})


@dataclass(frozen=True)
class DownloadedImage:
    path: Path
    filename: str
    content_type: str
    size: int


def _image_url(url: str) -> tuple[str, str]:
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise ValueError
        # Accessing port detects malformed netlocs without exposing the URL.
        parts.port
        return parts.hostname.lower().rstrip("."), parts.path
    except ValueError:
        raise MigrationError("Image source must be an absolute HTTP(S) URL without credentials") from None


def is_secure_url(url: str) -> bool:
    return _image_url(url)[0] in SECURE_HOSTS


def _signed_url(
    url: str,
    *,
    team: str | None,
    token: str | None,
    base_url: str,
    session: requests.Session | None,
) -> str:
    if not team or not token:
        raise MigrationError("Secure esa images require --esa-team and ESA_TOKEN")
    transport = HTTPClient(session=session, secrets=(token,))
    response = transport.request(
        "POST", f"{base_url.rstrip('/')}/teams/{quote(team, safe='')}/signed_urls",
        headers={"Authorization": f"Bearer {token}"},
        json={"urls": [url], "expires_in": 3600},
    )
    try:
        data = json_object(response, context="esa signed_urls")
    finally:
        response.close()
    pairs = data.get("signed_urls")
    if isinstance(pairs, list):
        matches = [pair[1] for pair in pairs if isinstance(pair, list) and len(pair) == 2 and pair[0] == url]
        if len(matches) == 1 and isinstance(matches[0], str):
            _image_url(matches[0])
            return matches[0]
    raise MigrationError("esa signed_urls: missing or invalid original-URL match")


def download_image(
    url: str,
    *,
    esa_team: str | None,
    esa_token: str | None,
    directory: Path,
    max_bytes: int,
    esa_base_url: str | None = None,
    session: requests.Session | None = None,
    esa_session: requests.Session | None = None,
) -> DownloadedImage:
    """Stream into a temporary file; callers own cache lifetime and deduplication.

    ``esa_base_url`` and separate sessions are internal injection points, not CLI
    settings. Never share an authenticated API session with the image GET.
    """
    host, source_path = _image_url(url)
    filename = unquote(source_path.rsplit("/", 1)[-1]).replace("\\", "_").replace("/", "_")
    filename = "".join(char for char in filename if char.isprintable()).strip() or "image"
    label = f"Image {filename!r} from {host}"
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise MigrationError(f"{label}: invalid workspace upload limit")
    if session is not None and session is esa_session:
        raise MigrationError("Image GET and esa API must use separate sessions")
    target = url
    try:
        if host in SECURE_HOSTS:
            target = _signed_url(
                url, team=esa_team, token=esa_token,
                base_url=esa_base_url if esa_base_url is not None else ESA_API_BASE_URL,
                session=esa_session,
            )
        transport = HTTPClient(session=session, secrets=(esa_token or "",))
        transport.session.auth = None
        # Explicit removal also protects tests/callers passing a preconfigured Session.
        response = transport.request(
            "GET", target, stream=True, allow_redirects=True,
            # Identify the downloader; some CDNs reject requests' generic UA.
            headers={"User-Agent": "esa2notion", "Authorization": None, "Notion-Version": None},
        )
    except MigrationError as error:
        # A signed target's path can itself contain secrets; keep only origin context.
        if target != url:
            raise MigrationError(f"{label}: signed image download failed") from None
        raise MigrationError(f"{label}: {error}") from None
    path: Path | None = None
    try:
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type in ("", "application/octet-stream"):
            content_type = mimetypes.guess_type(source_path)[0] or ""
        if content_type not in SUPPORTED_IMAGE_TYPES:
            raise MigrationError(f"{label}: unsupported or non-image Content-Type {content_type!r}")
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                raise MigrationError(f"{label}: invalid Content-Length") from None
            if declared < 0:
                raise MigrationError(f"{label}: invalid Content-Length")
            if declared > max_bytes:
                raise MigrationError(f"{label}: Content-Length exceeds workspace limit ({max_bytes} bytes)")
        if filename == "image":
            filename += mimetypes.guess_extension(content_type) or ""
        if len(filename.encode("utf-8")) > 900:
            raise MigrationError(f"{label}: filename exceeds Notion's 900-byte limit")
        directory.mkdir(parents=True, exist_ok=True)
        size = 0
        prefix = b""
        with tempfile.NamedTemporaryFile(mode="wb", dir=directory, prefix="esa2notion-", delete=False) as output:
            path = Path(output.name)
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                if len(prefix) < 512:
                    prefix += chunk[:512 - len(prefix)]
                    beginning = prefix.lstrip().lower()
                    if beginning.startswith((b"<!doctype html", b"<html")):
                        raise MigrationError(f"{label}: received an HTML login/error document instead of an image")
                size += len(chunk)
                if size > max_bytes:
                    raise MigrationError(f"{label}: streamed image exceeds workspace limit ({max_bytes} bytes)")
                output.write(chunk)
        if not size:
            raise MigrationError(f"{label}: empty image response")
        return DownloadedImage(path, filename, content_type, size)
    except (requests.RequestException, OSError):
        if path is not None:
            path.unlink(missing_ok=True)
        raise MigrationError(f"{label}: failed while streaming image") from None
    except BaseException:
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    finally:
        response.close()
