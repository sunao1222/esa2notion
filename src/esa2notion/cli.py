"""CLI entry point; dry-run never constructs an HTTP client."""

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Sequence
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .errors import MigrationError
from .source import load_documents


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="esaのMarkdown exportと画像をNotionへ新規移行します（再実行は重複を作成）")
    parser.add_argument("input", type=Path, help="esa export ZIPまたは展開済みディレクトリ")
    parser.add_argument("--parent-page-id", help="移行先親ページUUID")
    parser.add_argument("--dry-run", action="store_true", help="解析・集計のみ。ネットワーク通信なし")
    parser.add_argument("--esa-team", help="セキュア添付の署名URLを取得するesaチーム名")
    args = parser.parse_args(argv)
    parent = None
    if args.parent_page_id:
        try:
            parent = str(UUID(args.parent_page_id))
        except ValueError:
            parser.error("--parent-page-idはUUIDで指定してください")
    if not args.dry_run and not parent:
        parser.error("移行時は--parent-page-idが必要です")
    try:
        documents = load_documents(args.input)
        if args.dry_run:
            from .markdown import normalize_document
            run_id = uuid4().hex
            total_occurrences = 0
            total_urls = set()
            for document in documents:
                normalized = normalize_document(document, run_id)
                urls = {image.source_url for image in normalized.images}
                secure = any(urlsplit(url).hostname in {"files.esa.io", "dl.esa.io"} for url in urls)
                print(json.dumps({"source": document.source_path, "title": document.title,
                                  "category": list(document.category_parts),
                                  "images": len(normalized.images), "unique_images": len(urls),
                                  "requires_esa_credentials": secure}, ensure_ascii=False))
                total_occurrences += len(normalized.images)
                total_urls.update(urls)
            print(json.dumps({"articles": len(documents), "images": total_occurrences,
                              "unique_images": len(total_urls)}, ensure_ascii=False))
            return 0
        token = os.environ.get("NOTION_TOKEN", "").strip()
        if not token:
            raise MigrationError("NOTION_TOKENを環境変数に設定してください")
        from .notion import NotionClient
        from .migrate import migrate_documents
        print("新規作成モード: 再実行すると重複します。移行先の共有範囲を確認してください。", file=sys.stderr)
        client = NotionClient(token)
        failed_images = migrate_documents(
            documents, client, parent,
            esa_team=args.esa_team, esa_token=os.environ.get("ESA_TOKEN"))
        return 1 if failed_images else 0
    except (MigrationError, OSError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("中断しました。部分作成されたページを確認し、同じ親への再実行を避けてください。", file=sys.stderr)
        return 1
