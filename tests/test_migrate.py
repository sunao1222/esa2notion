from contextlib import redirect_stdout, redirect_stderr
import io
import json
import unittest
from unittest.mock import patch

import mistune

from esa2notion.errors import MigrationError
from esa2notion.migrate import migrate_documents
from esa2notion.source import SourceDocument
from esa2notion.notion import plain_text, rich_text


class StatefulNotion:
    def __init__(self, marker_mode="ok", persist=True):
        self.pages = []
        self.blocks = {}
        self.uploads = 0
        self.marker_mode = marker_mode
        self.persist = persist

    def get_page(self, page_id):
        return {"id": page_id}

    def get_upload_limit(self):
        return 10000

    def create_page(self, parent_page_id, title, markdown=None):
        page = {"id": str(len(self.pages)), "parent": parent_page_id, "title": title}
        self.pages.append(page)

        def add(tokens):
            ids = []
            for token in tokens:
                kind = token["type"]
                if kind == "blank_line":
                    continue
                count = {"missing": 0, "duplicate": 2}.get(self.marker_mode, 1) if kind == "image" else 1
                for _ in range(count):
                    block_id = "b" + str(len(self.blocks))
                    block = {"id": block_id, "page": page["id"], "type": kind}
                    self.blocks[block_id] = block
                    ids.append(block_id)
                    if kind == "image":
                        caption = "".join(t.get("raw", "") for t in token["children"])
                        block["image"] = {"type": "external", "external": {"url": token["attrs"]["url"]},
                                          "caption": rich_text(caption)}
                    else:
                        block["raw"] = token.get("raw", "")
                        block["children"] = add(token.get("children", []))
            return ids

        # Adapt Notion's tab-indented quote children for CommonMark parsing.
        lines = []
        in_quote = False
        for line in (markdown or "").splitlines(keepends=True):
            if line.startswith("> "):
                in_quote = True
            elif in_quote and line.startswith("\t"):
                line = "> " + line[1:]
            elif line.strip():
                in_quote = False
            lines.append(line)
        page["children"] = add(mistune.create_markdown(renderer="ast")("".join(lines)))
        return page

    def iter_blocks(self, parent_id):
        def walk(ids):
            for block_id in ids:
                block = self.blocks[block_id]
                yield block
                yield from walk(block.get("children", []))
        page = next(page for page in self.pages if page["id"] == parent_id)
        yield from walk(page["children"])

    def visible(self, page):
        return [plain_text(block["image"]["caption"]) if block["type"] == "image" else block["raw"]
                for block in self.iter_blocks(page["id"])
                if block["type"] == "image" or block.get("raw")]

    def upload_image(self, image):
        self.uploads += 1
        return "upload" + str(self.uploads)

    def update_image(self, block_id, upload_id, caption):
        if self.persist:
            self.blocks[block_id]["image"] = {"type": "file", "file": {"url": "https://notion.test/managed"},
                                               "caption": rich_text(caption)}

    def get_block(self, block_id):
        return self.blocks[block_id]


def document(path, title, body):
    return SourceDocument(path, title, (), body, None)


def failure_records(stderr):
    warnings, summary = stderr.split("画像取得失敗一覧: ", 1)
    return ([json.loads(line.removeprefix("画像取得失敗: ")) for line in warnings.splitlines()
             if line.startswith("画像取得失敗: ")],
            [json.loads(line) for line in summary.splitlines()[1:]])


class MigrationTests(unittest.TestCase):
    def run_migration(self, docs, client):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            failed = migrate_documents(docs, client, "parent", esa_team=None, esa_token=None)
        return failed, stdout.getvalue(), stderr.getvalue()

    def test_hierarchy_and_all_occurrences_become_managed(self):
        docs = [SourceDocument("a.md", "A", ("手順", "運用"), "![one](https://img.test/a.png)\n\n> ![two](https://img.test/a.png)", None),
                document("b.md", "B", "本文")]
        client = StatefulNotion()
        with patch("esa2notion.migrate.download_image", return_value=object()) as download:
            failed, output, errors = self.run_migration(docs, client)
        self.assertEqual([(p["parent"], p["title"]) for p in client.pages],
                         [("parent", "手順"), ("0", "運用"), ("1", "A"), ("parent", "B")])
        self.assertEqual([b["image"]["type"] for b in client.blocks.values() if b["type"] == "image"], ["file", "file"])
        self.assertEqual(client.uploads, 1)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(failed, 0)
        self.assertEqual(errors, "")
        self.assertIn("移行完了: 2記事", output)

    def test_missing_or_duplicate_marker_stops_before_upload_or_later_article(self):
        docs = [document("a.md", "A", "![a](https://img.test/a.png)"), document("b.md", "B", "later")]
        for mode in ("missing", "duplicate"):
            with self.subTest(mode=mode):
                client = StatefulNotion(mode)
                with patch("esa2notion.migrate.download_image", return_value=object()), self.assertRaises(MigrationError):
                    self.run_migration(docs, client)
                self.assertEqual([p["title"] for p in client.pages], ["A"])
                self.assertEqual(client.uploads, 0)

    def test_readback_external_is_not_success(self):
        client = StatefulNotion(persist=False)
        with patch("esa2notion.migrate.download_image", return_value=object()), self.assertRaises(MigrationError):
            self.run_migration([document("a.md", "A", "![a](https://img.test/a.png)")], client)

    def test_failed_downloads_are_cached_replaced_and_reported_per_article(self):
        bad, good, next_image = ("https://img.test/" + name + ".png" for name in ("bad", "good", "next"))
        docs = [document("a.md", "A", f"Before ![bad]({bad}) between ![again]({bad})\n\n![good]({good})\n\nAfter"),
                document("b.md", "B", f"![bad]({bad})\n\n![next]({next_image})")]
        client = StatefulNotion()

        def download(url, **kwargs):
            if url == bad:
                raise MigrationError("HTTP 404")
            return object()

        with patch("esa2notion.migrate.download_image", side_effect=download) as fetch:
            failed, output, errors = self.run_migration(docs, client)
        self.assertEqual(failed, 2)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [bad, good, next_image])
        self.assertEqual([p["title"] for p in client.pages], ["A", "B"])
        self.assertEqual(client.visible(client.pages[0]), ["Before", "画像リンク切れ", "between", "画像リンク切れ", "good", "After"])
        self.assertEqual(client.visible(client.pages[1]), ["画像リンク切れ", "next"])
        images = [b["image"] for b in client.blocks.values() if b["type"] == "image"]
        self.assertEqual([(image["type"], plain_text(image["caption"])) for image in images], [("file", "good"), ("file", "next")])
        self.assertEqual(client.uploads, 2)
        warnings, summary = failure_records(errors)
        self.assertEqual(warnings, summary)
        self.assertEqual(summary, [{"source": "a.md", "title": "A", "url": bad, "error": "HTTP 404"},
                                   {"source": "b.md", "title": "B", "url": bad, "error": "HTTP 404"}])
        self.assertIn("移行完了: 2記事（画像取得失敗2件）", output)
        self.assertNotIn("移行停止", errors)

    def test_all_images_failed_still_creates_body_and_later_article(self):
        client = StatefulNotion()
        docs = [document("a.md", "A", "Before ![bad](https://img.test/bad.png) after"), document("b.md", "B", "later")]
        with patch("esa2notion.migrate.download_image", side_effect=MigrationError("404")):
            failed, _, errors = self.run_migration(docs, client)
        self.assertEqual(failed, 1)
        self.assertEqual([client.visible(page) for page in client.pages], [["Before", "画像リンク切れ", "after"], ["later"]])
        self.assertEqual(client.uploads, 0)
        self.assertEqual(len(failure_records(errors)[1]), 1)

    def test_known_failures_are_summarized_when_notion_operations_stop(self):
        docs = [document("a.md", "A", "![bad](https://img.test/bad.png)\n\n![good](https://img.test/good.png)"),
                document("b.md", "B", "later")]
        for mode in ("upload", "readback", "missing", "duplicate"):
            with self.subTest(mode=mode):
                client = StatefulNotion(marker_mode=mode, persist=mode != "readback")
                if mode == "upload":
                    client.upload_image = lambda image: (_ for _ in ()).throw(MigrationError("upload failed"))
                def download(url, **kwargs):
                    if url.endswith("bad.png"):
                        raise MigrationError("HTTP 404")
                    return object()
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("esa2notion.migrate.download_image", side_effect=download), redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(MigrationError):
                        migrate_documents(docs, client, "parent", esa_team=None, esa_token=None)
                self.assertEqual([page["title"] for page in client.pages], ["A"])
                self.assertIn("移行停止", stderr.getvalue())
                warnings, summary = failure_records(stderr.getvalue())
                self.assertEqual(warnings, summary)
                self.assertEqual(summary[0]["source"], "a.md")
                self.assertEqual(summary[0]["error"], "HTTP 404")

    def test_logs_hide_url_secrets_but_query_variants_have_separate_results(self):
        urls = [f"https://user:password@img.test/a.png?signature={value}#fragment" for value in ("secret1", "secret2")]
        client = StatefulNotion()
        docs = [document("a\n.md", "A\nTitle", "\n\n".join(f"![bad](<{url}>)" for url in urls + urls))]
        with patch("esa2notion.migrate.download_image", side_effect=MigrationError("HTTP 403")) as fetch:
            failed, _, errors = self.run_migration(docs, client)
        self.assertEqual(failed, 2)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], urls)
        warnings, summary = failure_records(errors)
        self.assertEqual(warnings, summary)
        self.assertEqual([record["url"] for record in summary], ["https://img.test/a.png"] * 2)
        self.assertEqual(summary[0]["source"], "a\n.md")
        self.assertEqual(summary[0]["title"], "A\nTitle")
        for secret in ("user", "password", "signature", "secret1", "secret2", "fragment"):
            self.assertNotIn(secret, errors)
        self.assertEqual(client.visible(client.pages[0]), ["画像リンク切れ"] * 4)

    def test_raw_oserror_is_not_a_recoverable_image_failure(self):
        client = StatefulNotion()
        with patch("esa2notion.migrate.download_image", side_effect=OSError("disk full")), self.assertRaises(MigrationError):
            self.run_migration([document("a.md", "A", "![a](https://img.test/a.png)")], client)
        self.assertEqual(client.pages, [])

    def test_all_documents_validated_before_writes(self):
        for body in ("![bad](relative.png)", "![secret](https://files.esa.io/a.png)"):
            client = StatefulNotion()
            with self.subTest(body=body), self.assertRaises(MigrationError):
                self.run_migration([document("first.md", "First", "valid"), document("bad.md", "Bad", body)], client)
            self.assertEqual(client.pages, [])
