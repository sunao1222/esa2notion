"""One-shot migration: create pages, then replace and verify every image."""

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import uuid4

from .errors import MigrationError
from .http import safe_url
from .images import download_image
from .markdown import normalize_document
from .notion import NotionClient, plain_text, rich_text
from .source import SourceDocument


def migrate_documents(documents: list[SourceDocument], client: NotionClient,
                      parent_page_id: str, *, esa_team: str | None,
                      esa_token: str | None) -> int:
    completed = 0
    current = "事前検証"
    page_url = None
    failures: dict[tuple[str, str], dict[str, str]] = {}
    try:
        # Complete local validation for the whole batch before touching Notion.
        normalized = []
        for doc in documents:
            run_id = uuid4().hex
            normalized.append((doc, run_id, normalize_document(doc, run_id)))
        for doc, _, body in normalized:
            current = doc.source_path
            rich_text(doc.title)
            for category in doc.category_parts:
                rich_text(category)
            for image in body.images:
                rich_text(image.caption)
                if urlsplit(image.source_url).hostname in {"files.esa.io", "dl.esa.io"}:
                    if not esa_team or not esa_token:
                        raise MigrationError("セキュア画像には--esa-teamとESA_TOKENが必要です")
        current = "事前検証"
        client.get_page(parent_page_id)
        max_bytes = client.get_upload_limit()
        categories: dict[tuple[str, ...], str] = {(): parent_page_id}
        downloads = {}
        download_errors: dict[str, str] = {}
        with TemporaryDirectory(prefix="esa2notion-") as temp:
            for doc, run_id, body in normalized:
                current = doc.source_path
                page_url = None
                document_failure_count = 0
                for url in dict.fromkeys(image.source_url for image in body.images):
                    if url not in downloads and url not in download_errors:
                        try:
                            downloads[url] = download_image(
                                url, esa_team=esa_team, esa_token=esa_token,
                                directory=Path(temp), max_bytes=max_bytes)
                        except MigrationError as exc:
                            download_errors[url] = str(exc)
                    if url in download_errors:
                        document_failure_count += 1
                        key = (doc.source_path, url)
                        if key not in failures:
                            record = {"source": doc.source_path, "title": doc.title,
                                      "url": safe_url(url), "error": download_errors[url]}
                            failures[key] = record
                            print("画像取得失敗: " + json.dumps(record, ensure_ascii=False),
                                  file=sys.stderr, flush=True)
                if document_failure_count:
                    body = normalize_document(doc, run_id, failed_image_urls=download_errors)
                for depth in range(1, len(doc.category_parts) + 1):
                    category = doc.category_parts[:depth]
                    if category not in categories:
                        page = client.create_page(categories[category[:-1]], category[-1])
                        categories[category] = page["id"]
                        print(f"カテゴリ作成: {page.get('url') or _page_url(page['id'])}", flush=True)
                page = client.create_page(categories[doc.category_parts], doc.title,
                                          markdown=body.markdown or None)
                page_url = page.get("url") or _page_url(page["id"])
                print(f"記事作成: {current}: {page_url}", flush=True)
                if body.images:
                    expected = {image.marker: image for image in body.images}
                    matches: dict[str, list[str]] = {marker: [] for marker in expected}
                    for block in client.iter_blocks(page["id"]):
                        if block.get("type") == "image":
                            marker = plain_text(block.get("image", {}).get("caption", []))
                            if marker in matches:
                                matches[marker].append(block["id"])
                    if any(len(ids) != 1 for ids in matches.values()):
                        missing = sum(not ids for ids in matches.values())
                        duplicate = sum(len(ids) > 1 for ids in matches.values())
                        raise MigrationError(f"画像位置markerが不整合です（欠落{missing}、重複{duplicate}）")
                    uploads = {}
                    for image in body.images:
                        if image.source_url not in uploads:
                            uploads[image.source_url] = client.upload_image(downloads[image.source_url])
                        block_id = matches[image.marker][0]
                        client.update_image(block_id, uploads[image.source_url], image.caption)
                        actual = client.get_block(block_id).get("image", {})
                        if (actual.get("type") != "file" or not actual.get("file", {}).get("url")
                                or plain_text(actual.get("caption", [])) != image.caption):
                            raise MigrationError("画像をNotion管理ファイルとして確認できません")
                completed += 1
                suffix = f"（画像取得失敗{document_failure_count}件）" if document_failure_count else ""
                print(f"記事完了: {current}: {page_url}{suffix}", flush=True)
        suffix = f"（画像取得失敗{len(failures)}件）" if failures else ""
        print(f"移行完了: {completed}記事{suffix}", flush=True)
        return len(failures)
    except (MigrationError, OSError) as exc:
        print(f"移行停止: 記事={current} 作成済みURL={page_url or 'なし'} 完了記事数={completed}", file=sys.stderr)
        raise MigrationError(f"{current}: {exc}。部分作成ページを確認し、同じ親へそのまま再実行しないでください") from exc
    finally:
        if failures:
            print(f"画像取得失敗一覧: {len(failures)}件", file=sys.stderr, flush=True)
            for record in failures.values():
                print(json.dumps(record, ensure_ascii=False), file=sys.stderr, flush=True)


def _page_url(page_id: str) -> str:
    return "https://www.notion.so/" + page_id.replace("-", "")
