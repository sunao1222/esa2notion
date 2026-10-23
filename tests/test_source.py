import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile, ZipInfo
import stat

from esa2notion.errors import MigrationError
from esa2notion.source import load_documents


class SourceTests(unittest.TestCase):
    def test_directory_and_zip_preserve_metadata_and_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            export = root / "export"
            export.mkdir()
            header = (
                "---\n# 元のコメント\n"
                "title: 手順\ncategory: /開発//運用/\ntags: [old]\n"
                "created_at: 2024-01-02T03:04:05+09:00\n"
                'updated_at: "2024-02-03T04:05:06Z"\n'
                "published: false\nnumber: 123\nempty:\n"
                "custom:\n  nested: 'quoted value'\n"
                "tags:\n  - new\n---\n"
            )
            (export / "a.md").write_text(header + "# 本文\n", encoding="utf-8")
            (export / "b.markdown").write_text("本文のみ", encoding="utf-8")
            (export / "ignored.md").symlink_to(export / "a.md")
            (export / "__MACOSX").mkdir()
            (export / "__MACOSX" / "ghost.md").write_text("ignored")
            zipped = root / "export.zip"
            with ZipFile(zipped, "w") as archive:
                archive.write(export / "b.markdown", "b.markdown")
                archive.write(export / "a.md", "a.md")
            docs = load_documents(export)
            self.assertEqual(docs, load_documents(zipped))
            self.assertEqual((docs[0].title, docs[0].category_parts, docs[0].body), ("手順", ("開発", "運用"), "# 本文"))
            self.assertEqual((docs[1].title, docs[1].category_parts), ("b", ()))
            self.assertEqual(docs[0].front_matter, header)
            self.assertIsNone(docs[1].front_matter)

    def test_header_boundaries_preserve_raw_text_and_existing_body_semantics(self):
        cases = [
            ("本文のみ", None, "本文のみ"),
            ("---\n---\n", "---\n---\n", ""),
            ("---\n---", "---\n---", ""),
            ("---\ntitle:\ncategory:\n---", "---\ntitle:\ncategory:\n---", ""),
            ("\ufeff \n---\n# comment\n---\n\n    body\n", "---\n# comment\n---\n", "body"),
            ("---\r\ntags: []\r\n---\r\n\r\nbody\r\n", "---\r\ntags: []\r\n---\r\n", "body"),
            ("---\ntags: [a]\nbody", None, "---\ntags: [a]\nbody"),
        ]
        for text, header, body in cases:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as temp:
                (Path(temp) / "article.md").write_bytes(text.encode("utf-8"))
                article = load_documents(Path(temp))[0]
                self.assertEqual(article.front_matter, header)
                self.assertEqual(article.body, body)
                self.assertEqual((article.title, article.category_parts), ("article", ()))

    def test_invalid_input_rejected(self):
        for text in ("---\ntitle: [broken\n---\nx", "---\ntitle: [a]\n---\nx", "---\ncategory: 1\n---\nx"):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as temp:
                (Path(temp) / "bad.md").write_text(text)
                with self.assertRaises(MigrationError):
                    load_documents(Path(temp))

    def test_unsafe_zip_and_symlink_rejected(self):
        for name in ("../bad.md", "/bad.md", "C:/bad.md", "..\\bad.md", "link.md"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "input.zip"
                with ZipFile(path, "w") as archive:
                    info = ZipInfo(name)
                    if name == "link.md":
                        info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, "target")
                with self.assertRaises(MigrationError):
                    load_documents(path)

    def test_empty_and_non_utf8_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(MigrationError):
                load_documents(Path(temp))
            (Path(temp) / "bad.md").write_bytes(b"\xff")
            with self.assertRaises(MigrationError):
                load_documents(Path(temp))
