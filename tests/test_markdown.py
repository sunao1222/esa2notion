from __future__ import annotations

import re
import unittest
from xml.etree import ElementTree

import mistune

from esa2notion.errors import MigrationError
from esa2notion.markdown import normalize_document
from esa2notion.source import SourceDocument


def document(body: str) -> SourceDocument:
    return SourceDocument("exports/article.md", "Article", (), body, None)


def walk(tokens):
    for token in tokens:
        yield token
        yield from walk(token.get("children", []))


class MarkdownNormalizationTests(unittest.TestCase):
    def test_raw_yaml_is_first_code_block_and_metadata_images_stay_literal(self):
        for ticks in ("", "```", "`````"):
            for newline in ("\n", "\r\n"):
                with self.subTest(ticks=ticks, newline=newline):
                    header = (
                        "---\n# keep comment\ntitle: 'Original'\ntags: [one, two]\n"
                        "created_at: 2024-01-02T03:04:05+09:00\n"
                        'updated_at: "2024-02-03T04:05:06Z"\n'
                        "published: false\nnumber: 123\nunknown: |\n"
                        f"  {ticks}\n"
                        "  ![metadata](https://files.esa.io/not-an-image.png)\n"
                        "  <img src='https://never.example/metadata.png'>\n---\n"
                    ).replace("\n", newline)
                    source = SourceDocument("a.md", "Original", (), (
                        "# 本文\n\n![body](https://images.example/body.png)\n"
                    ), header)
                    result = normalize_document(source, "metadata")
                    ast = [token for token in mistune.create_markdown(renderer="ast")(result.markdown)
                           if token["type"] != "blank_line"]
                    self.assertEqual(ast[0]["type"], "block_code")
                    self.assertEqual(ast[0]["attrs"]["info"], "yaml")
                    self.assertEqual(ast[0]["raw"], header.replace("\r\n", "\n"))
                    self.assertEqual([token["type"] for token in ast], ["block_code", "heading", "paragraph"])
                    self.assertEqual(ast[1]["children"][0]["raw"], "本文")
                    self.assertEqual([(image.source_url, image.caption) for image in result.images],
                                     [("https://images.example/body.png", "body")])

    def test_empty_and_header_only_documents_keep_their_yaml_block(self):
        for header in ("---\n---", "---\n---\n", "---\ntitle: Header only\n---"):
            with self.subTest(header=header):
                result = normalize_document(SourceDocument("a.md", "A", (), "", header), "header-only")
                ast = [token for token in mistune.create_markdown(renderer="ast")(result.markdown)
                       if token["type"] != "blank_line"]
                self.assertEqual([token["type"] for token in ast], ["block_code"])
                self.assertEqual(ast[0]["attrs"]["info"], "yaml")
                self.assertEqual(ast[0]["raw"], header if header.endswith("\n") else header + "\n")
                self.assertEqual(result.images, [])

    def test_inline_reference_and_duplicate_images_keep_original_captions(self):
        source = document(
            "Before ![first alt](https://images.example/one.png) after.\n\n"
            "![reference caption][picture]\n\n"
            "![another caption](https://images.example/one.png)\n\n"
            "[picture]: https://images.example/two.png \"not the caption\"\n"
        )
        result = normalize_document(source, "run")
        self.assertEqual([image.caption for image in result.images], ["first alt", "reference caption", "another caption"])
        self.assertEqual([image.source_url for image in result.images], [
            "https://images.example/one.png", "https://images.example/two.png", "https://images.example/one.png",
        ])
        self.assertEqual(len({image.marker for image in result.images}), 3)
        self.assertTrue(all(image.marker.startswith("esa2notion-image-run-") for image in result.images))
        ast = mistune.create_markdown(renderer="ast")(result.markdown)
        image_paragraphs = [token for token in ast if token["type"] == "paragraph" and any(
            child["type"] == "image" for child in token["children"]
        )]
        self.assertEqual(len(image_paragraphs), 3)
        self.assertTrue(all(len(token["children"]) == 1 for token in image_paragraphs))
        self.assertLess(result.markdown.index("Before"), result.markdown.index(result.images[0].marker))
        self.assertLess(result.markdown.index(result.images[0].marker), result.markdown.index("after."))

    def test_html_attributes_entities_and_other_html_are_retained(self):
        source = document(
            "<div class='keep' data-a=\"x&amp;y\">\n"
            "<!-- <img src='https://never.example/comment.png'> -->\n"
            "<IMG\n alt='A &amp; B' width='200'\n"
            " src=\"https://custom-s3.example/a.png?x=1&amp;y=2\" />\n"
            "<span data-x='unchanged'>keep &amp; this</span>\n"
            "<img src='https://images.example/b.png' alt=\"second\">\n"
            "</div>\n\n"
            "Text <img alt='inline' src='https://images.example/c.png'> end.\n"
        )
        result = normalize_document(source, "html")
        self.assertEqual([image.caption for image in result.images], ["A & B", "second", "inline"])
        self.assertEqual(result.images[0].source_url, "https://custom-s3.example/a.png?x=1&y=2")
        for unchanged in [
            "<div class='keep' data-a=\"x&amp;y\">",
            "<!-- <img src='https://never.example/comment.png'> -->",
            "<span data-x='unchanged'>keep &amp; this</span>",
            "</div>",
        ]:
            self.assertIn(unchanged, result.markdown)
        self.assertNotIn("width='200'", result.markdown)
        self.assertEqual(result.markdown.count("![esa2notion-image-"), 3)

    def test_fenced_indented_and_inline_code_are_not_images(self):
        body = (
            "```markdown\n![fake](https://never.example/fenced.png)\n"
            "<img src='https://never.example/html.png'>\n```\n\n"
            "    ![fake](https://never.example/indented.png)\n\n"
            "Inline `![fake](https://never.example/inline.png)` end.\n\n"
            "Ticked `` `![fake](https://never.example/ticks.png)` `` end.\n\n"
            "Padded ``  ![fake](https://never.example/padded.png)  `` end.\n\n"
            "![real](https://images.example/real.png)\n"
        )
        result = normalize_document(document(body), "code")
        self.assertEqual(len(result.images), 1)
        self.assertEqual(result.images[0].caption, "real")
        parser = mistune.create_markdown(renderer="ast")
        before = [(token["type"], token["raw"]) for token in walk(parser(body)) if token["type"] in {"codespan", "block_code"}]
        after = [(token["type"], token["raw"]) for token in walk(parser(result.markdown)) if token["type"] in {"codespan", "block_code"}]
        self.assertEqual(after, before)

    def test_images_are_lifted_out_of_inline_formatting(self):
        result = normalize_document(document(
            "**before ![*plain* &amp; `code`](https://images.example/one.png) after**\n"
        ), "format")
        self.assertEqual(result.images[0].caption, "plain & code")
        ast = mistune.create_markdown(renderer="ast")(result.markdown)
        paragraphs = [token for token in ast if token["type"] == "paragraph"]
        self.assertEqual(len(paragraphs), 3)
        self.assertEqual(paragraphs[0]["children"][0]["type"], "strong")
        self.assertEqual(paragraphs[1]["children"][0]["type"], "image")
        self.assertEqual(paragraphs[2]["children"][0]["type"], "strong")

    def test_nested_lists_and_quotes_keep_images_as_children(self):
        result = normalize_document(document(
            "- outer\n"
            "  - inner before ![nested](https://images.example/nested.png) after\n\n"
            "> quotation before ![quoted](https://images.example/quote.png) after\n"
        ), "nested")
        nested, quoted = result.images
        lines = result.markdown.splitlines()
        nested_line = next(line for line in lines if nested.marker in line)
        quoted_line = next(line for line in lines if quoted.marker in line)
        self.assertTrue(nested_line.startswith("\t\t!["), result.markdown)
        self.assertTrue(quoted_line.startswith("\t!["), result.markdown)
        self.assertIn("\t- inner before", result.markdown)
        self.assertIn("> quotation before", result.markdown)
        self.assertLess(result.markdown.index("inner before"), result.markdown.index(nested.marker))
        self.assertLess(result.markdown.index(nested.marker), result.markdown.index("after"))
        self.assertLess(result.markdown.index("quotation before"), result.markdown.index(quoted.marker))

    def test_image_only_container_uses_empty_parent_and_child_image(self):
        result = normalize_document(document(
            "- ![list](https://images.example/list.png)\n\n"
            "> ![quote](https://images.example/quote.png)\n"
        ), "only")
        first, second = result.images
        self.assertIn("- \n\t![" + first.marker, result.markdown)
        self.assertIn("> \n\t![" + second.marker, result.markdown)

    def test_table_header_and_body_images_are_relocated_after_table(self):
        result = normalize_document(document(
            "| Header ![head](https://images.example/head.png) | Other |\n"
            "| --- | --- |\n"
            "| **![body](https://images.example/body.png)** | <img src='https://images.example/empty.png'> |\n\n"
            "Following paragraph.\n"
        ), "table")
        self.assertEqual([image.caption for image in result.images], [
            "表 1行 1列: head", "表 2行 1列: body", "表 2行 2列: ",
        ])
        table_text = re.search(r"<table\b.*?</table>", result.markdown, re.S).group()
        table = ElementTree.fromstring(table_text)
        self.assertEqual(table.attrib["header-row"], "true")
        rows = table.findall("tr")
        self.assertEqual(len(rows), 2)
        self.assertIn("head", rows[0].findall("td")[0].text)
        self.assertIn("body", rows[1].findall("td")[0].text)
        self.assertEqual(rows[1].findall("td")[1].text, "画像")
        self.assertNotIn("![", table_text)
        positions = [result.markdown.index(image.marker) for image in result.images]
        self.assertEqual(positions, sorted(positions))
        self.assertGreater(min(positions), result.markdown.index("</table>"))
        self.assertLess(max(positions), result.markdown.index("Following paragraph"))

    def test_table_in_list_keeps_relocated_images_in_list(self):
        result = normalize_document(document(
            "- parent\n\n"
            "  | A |\n"
            "  | --- |\n"
            "  | ![cell](https://images.example/cell.png) |\n"
        ), "nested-table")
        self.assertEqual(len(result.images), 1)
        self.assertEqual(result.images[0].caption, "表 2行 1列: cell")
        image_line = next(line for line in result.markdown.splitlines() if result.images[0].marker in line)
        self.assertTrue(image_line.startswith("\t!["), result.markdown)

    def test_task_states_and_strikethrough_are_preserved(self):
        result = normalize_document(document(
            "- [x] completed ~~old~~\n"
            "- [ ] pending ![task](https://images.example/task.png)\n"
            "  - [X] nested complete\n"
        ), "tasks")
        self.assertIn("- [x] completed ~~old~~", result.markdown)
        self.assertIn("- [ ] pending", result.markdown)
        self.assertIn("\t- [x] nested complete", result.markdown)
        self.assertEqual([image.caption for image in result.images], ["task"])

    def test_failed_images_become_text_in_place_without_changing_code_or_metadata(self):
        bad, good = "https://img.test/bad.png", "https://img.test/good.png"
        literal = f"![literal]({bad})"
        header = f"---\nexample: {literal}\n---\n"
        body = (
            f"Before ![bad]({bad}) after\n\n![reference][ref]\n\n"
            f"<img src='{bad}' alt='html'>\n\n![good]({good})\n\n"
            f"```markdown\n{literal}\n```\n\n`{literal}`\n\n[ref]: {bad}\n"
        )
        result = normalize_document(SourceDocument("a.md", "A", (), body, header),
                                    "failed", failed_image_urls={bad})
        ast = [token for token in mistune.create_markdown(renderer="ast")(result.markdown)
               if token["type"] != "blank_line"]
        self.assertEqual(ast[0]["raw"], header)
        paragraphs = [token for token in ast if token["type"] == "paragraph"]
        self.assertEqual([token["children"][0].get("raw") for token in paragraphs[:5]],
                         ["Before", "画像リンク切れ", "after", "画像リンク切れ", "画像リンク切れ"])
        self.assertEqual([(image.source_url, image.caption) for image in result.images], [(good, "good")])
        code = [(token["type"], token["raw"]) for token in walk(ast)
                if token["type"] in {"block_code", "codespan"}]
        self.assertEqual(code, [("block_code", header), ("block_code", literal + "\n"), ("codespan", literal)])
        self.assertEqual([token["attrs"]["url"] for token in walk(ast) if token["type"] == "image"], [good])

    def test_failed_images_remain_inside_nested_list_and_quote(self):
        bad = "https://img.test/bad.png"
        result = normalize_document(document(
            f"- outer\n  - before ![bad]({bad}) after\n\n"
            f"> before ![bad]({bad}) after\n"
        ), "containers", failed_image_urls={bad})
        ast = mistune.create_markdown(renderer="ast")(result.markdown)
        containers = [token for token in ast if token["type"] in {"list", "block_quote"}]
        self.assertEqual([token["type"] for token in containers], ["list", "block_quote"])
        self.assertEqual([token["raw"] for token in walk([containers[0]]) if token["type"] == "text"],
                         ["outer", "before", "画像リンク切れ", "after"])
        # Notion Enhanced Markdown uses tab-indented quote children, which
        # CommonMark's AST parser does not represent as quote descendants.
        quote = result.markdown.split("> ", 1)[1]
        self.assertEqual([line.rstrip() for line in quote.splitlines() if line.strip()],
                         ["before", "\t画像リンク切れ", "\t after"])
        self.assertEqual(result.images, [])

    def test_failed_table_images_stay_in_cells_without_relocation(self):
        bad, good = "https://img.test/bad.png", "https://img.test/good.png"
        result = normalize_document(document(
            f"| Before ![bad]({bad}) after | Good |\n| --- | --- |\n"
            f"| <img src='{bad}'> | ![good]({good}) |\n"
        ), "table-failure", failed_image_urls={bad})
        table_text = re.search(r"<table\b.*?</table>", result.markdown, re.S).group()
        table = ElementTree.fromstring(table_text)
        rows = table.findall("tr")
        self.assertEqual(rows[0].findall("td")[0].text, "Before 画像リンク切れ after")
        self.assertEqual(rows[1].findall("td")[0].text, "画像リンク切れ")
        self.assertNotIn("画像リンク切れ", result.markdown.split("</table>", 1)[1])
        self.assertEqual([(image.source_url, image.caption) for image in result.images],
                         [(good, "表 2行 2列: good")])

    def test_invalid_image_urls_report_source_document(self):
        for body in [
            "![relative](./image.png)",
            "![data](data:image/png;base64,AAAA)",
            "<img alt='missing'>",
            "<img src='//images.example/protocol-relative.png'>",
            "<img src='https://'>",
        ]:
            with self.subTest(body=body):
                with self.assertRaises(MigrationError) as error:
                    normalize_document(document(body), "invalid")
                self.assertIn("exports/article.md", str(error.exception))
                self.assertIn("HTTP(S)", str(error.exception))

    def test_custom_hosts_and_empty_body_are_supported(self):
        result = normalize_document(document("![custom](http://private-storage.example/image.png)"), "custom")
        self.assertEqual(result.images[0].source_url, "http://private-storage.example/image.png")
        empty = normalize_document(document(""), "empty")
        self.assertEqual(empty.markdown.strip(), "")
        self.assertEqual(empty.images, [])


if __name__ == "__main__":
    unittest.main()
