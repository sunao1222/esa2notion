"""Notion API operations used by the one-shot Markdown migration."""

from __future__ import annotations

import io
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any
from urllib.parse import quote

import requests

from .errors import MigrationError
from .http import HTTPClient, json_object
from .images import DownloadedImage

NOTION_BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
SINGLE_PART_LIMIT = 20 * 1024 * 1024
PART_SIZE = 10 * 1024 * 1024


def rich_text(text: str) -> list[dict[str, Any]]:
    """Preserve all text while respecting 2,000 chars/item and 100 items."""
    if len(text) > 2000 * 100:
        raise MigrationError("Notion rich text exceeds 100 elements of 2,000 characters")
    return [
        {"type": "text", "text": {"content": text[start:start + 2000]}}
        for start in range(0, len(text), 2000)
    ]


def plain_text(items: Sequence[dict[str, Any]]) -> str:
    result: list[str] = []
    for item in items:
        value = item.get("plain_text")
        if not isinstance(value, str):
            text = item.get("text", {})
            value = text.get("content", "") if isinstance(text, dict) else ""
        if not isinstance(value, str):
            raise MigrationError("Notion returned malformed rich text")
        result.append(value)
    return "".join(result)


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = NOTION_BASE_URL,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        min_interval: float = 0.35,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION}
        self.http = HTTPClient(
            session=session, sleep=sleep, monotonic=monotonic,
            min_interval=min_interval, secrets=(token,),
        )
        self._upload_limit: int | None = None

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not path.startswith("/") or path.startswith("//"):
            raise MigrationError("Notion request path must be relative to its API base")
        headers = dict(kwargs.pop("headers", {}))
        headers.update(self._headers)
        # requests must generate the multipart Content-Type and boundary itself.
        if "files" in kwargs:
            headers["Content-Type"] = None
        response = self.http.request(method, self.base_url + path, headers=headers, **kwargs)
        try:
            return json_object(response, context=f"Notion {method} {path}")
        finally:
            response.close()

    def get_upload_limit(self) -> int:
        if self._upload_limit is None:
            user = self.request("GET", "/users/me")
            try:
                limit = user["bot"]["workspace_limits"]["max_file_upload_size_in_bytes"]
            except (KeyError, TypeError):
                raise MigrationError("Notion did not provide the workspace file upload limit") from None
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise MigrationError("Notion returned an invalid workspace file upload limit")
            self._upload_limit = limit
        return self._upload_limit

    def get_page(self, page_id: str) -> dict[str, Any]:
        return self.request("GET", f"/pages/{quote(page_id, safe='')}")

    def create_page(self, parent_page_id: str, title: str, markdown: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {"title": {"title": rich_text(title)}},
        }
        if markdown is not None:
            payload.update(markdown=markdown, markdown_version="v1")
        return self.request("POST", "/pages", json=payload)

    def iter_blocks(self, parent_id: str) -> Iterator[dict[str, Any]]:
        seen_blocks: set[str] = set()

        def children(block_id: str) -> Iterator[dict[str, Any]]:
            cursor: str | None = None
            seen_cursors: set[str] = set()
            while True:
                params: dict[str, Any] = {"page_size": 100}
                if cursor is not None:
                    params["start_cursor"] = cursor
                page = self.request("GET", f"/blocks/{quote(block_id, safe='')}/children", params=params)
                blocks = page.get("results")
                if not isinstance(blocks, list):
                    raise MigrationError("Notion block listing is missing results")
                for block in blocks:
                    if not isinstance(block, dict) or not isinstance(block.get("id"), str):
                        raise MigrationError("Notion returned a malformed block")
                    identity = block["id"]
                    if identity in seen_blocks:
                        raise MigrationError("Notion returned a repeated block in recursive listing")
                    seen_blocks.add(identity)
                    yield block
                    if block.get("has_children"):
                        yield from children(identity)
                if page.get("has_more") is False:
                    break
                if page.get("has_more") is not True:
                    raise MigrationError("Notion block listing is missing has_more")
                cursor = page.get("next_cursor")
                if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                    raise MigrationError("Notion block listing has a missing or repeated pagination cursor")
                seen_cursors.add(cursor)

        yield from children(parent_id)

    def update_image(self, block_id: str, upload_id: str, caption: str) -> None:
        self.request("PATCH", f"/blocks/{quote(block_id, safe='')}", json={
            "type": "image",
            "image": {"file_upload": {"id": upload_id}, "caption": rich_text(caption)},
        })

    def get_block(self, id: str) -> dict[str, Any]:
        return self.request("GET", f"/blocks/{quote(id, safe='')}")

    def upload_image(self, image: DownloadedImage) -> str:
        if image.size <= 0 or image.size > self.get_upload_limit():
            raise MigrationError(f"Image {image.filename!r} is empty or exceeds the workspace upload limit")
        payload: dict[str, Any] = {"filename": image.filename, "content_type": image.content_type}
        multipart = image.size > SINGLE_PART_LIMIT
        payload["mode"] = "multi_part" if multipart else "single_part"
        if multipart:
            payload["number_of_parts"] = (image.size + PART_SIZE - 1) // PART_SIZE
        try:
            with image.path.open("rb") as source:
                # Detect changed cache files before creating a remote upload object.
                source.seek(0, io.SEEK_END)
                if source.tell() != image.size:
                    raise MigrationError(f"Image {image.filename!r} changed size before upload")
                source.seek(0)
                created = self.request("POST", "/file_uploads", json=payload)
                upload_id = created.get("id")
                if not isinstance(upload_id, str) or not upload_id:
                    raise MigrationError("Notion file upload creation did not return an ID")
                path = f"/file_uploads/{quote(upload_id, safe='')}"
                if multipart:
                    for part_number in range(1, payload["number_of_parts"] + 1):
                        chunk = source.read(PART_SIZE)
                        expected = min(PART_SIZE, image.size - (part_number - 1) * PART_SIZE)
                        if len(chunk) != expected:
                            raise MigrationError(f"Image {image.filename!r} changed during upload")
                        with io.BytesIO(chunk) as part:
                            self.request("POST", path + "/send",
                                         data={"part_number": str(part_number)},
                                         files={"file": (image.filename, part, image.content_type)})
                    result = self.request("POST", path + "/complete")
                else:
                    result = self.request("POST", path + "/send",
                                          files={"file": (image.filename, source, image.content_type)})
                if result.get("status") != "uploaded":
                    raise MigrationError(f"Notion file upload {upload_id} did not reach uploaded status")
                return upload_id
        except OSError:
            raise MigrationError(f"Cannot read downloaded image {image.filename!r} for upload") from None
