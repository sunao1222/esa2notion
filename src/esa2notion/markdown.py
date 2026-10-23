"""Normalize esa Markdown while retaining each image's position and caption."""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from html import escape, unescape
from html.parser import HTMLParser
from textwrap import indent
from typing import Any, Iterable
from urllib.parse import urlsplit

import mistune
from mistune.core import BlockState
from mistune.plugins.table import table_in_list, table_in_quote
from mistune.renderers.markdown import MarkdownRenderer

from .errors import MigrationError
from .source import SourceDocument


@dataclass(frozen=True)
class ImageOccurrence:
    marker: str
    source_url: str
    caption: str


@dataclass(frozen=True)
class NormalizedDocument:
    markdown: str
    images: list[ImageOccurrence]


@dataclass(frozen=True)
class _InlineImage:
    source_url: str | None
    caption: str


Token = dict[str, Any]


class _HTMLImages(HTMLParser):
    """Locate img start tags without serializing or altering surrounding HTML."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.offsets = [0]
        for line in source.splitlines(keepends=True):
            self.offsets.append(self.offsets[-1] + len(line))
        self.matches: list[tuple[int, int, _InlineImage]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return
        attributes = dict(attrs)
        line, column = self.getpos()
        start = self.offsets[line - 1] + column
        raw = self.get_starttag_text()
        self.matches.append(
            (start, start + len(raw), _InlineImage(attributes.get("src"), attributes.get("alt") or ""))
        )

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def pieces(self) -> Iterable[str | _InlineImage]:
        self.feed(self.source)
        self.close()
        cursor = 0
        for start, end, image in self.matches:
            if start > cursor:
                yield self.source[cursor:start]
            yield image
            cursor = end
        if cursor < len(self.source):
            yield self.source[cursor:]


def _caption(tokens: Iterable[Token]) -> str:
    parts = []
    for token in tokens:
        if "children" in token:
            parts.append(_caption(token["children"]))
        elif token["type"] in {"softbreak", "linebreak"}:
            parts.append("\n")
        else:
            parts.append(unescape(token.get("raw", "")))
    return "".join(parts)


def _trim_inline_edge(tokens: list[Token], *, leading: bool) -> list[Token]:
    """Drop only image-boundary whitespace, including inside formatting nodes."""
    result = list(tokens)
    while result:
        index = 0 if leading else -1
        token = result[index]
        if token["type"] in {"softbreak", "linebreak"}:
            result.pop(index)
            continue
        if token["type"] == "text":
            raw = token["raw"].lstrip() if leading else token["raw"].rstrip()
            if raw:
                result[index] = {**token, "raw": raw}
                break
            result.pop(index)
            continue
        if "children" in token:
            children = _trim_inline_edge(token["children"], leading=leading)
            if children:
                result[index] = {**token, "children": children}
                break
            result.pop(index)
            continue
        break
    return result


def _inline_parts(tokens: Iterable[Token]) -> Iterable[Token | _InlineImage]:
    """Lift images out of inline formatting, retaining formatting on either side."""
    for token in tokens:
        kind = token["type"]
        if kind == "image":
            yield _InlineImage(token["attrs"]["url"], _caption(token["children"]))
        elif kind == "inline_html":
            for piece in _HTMLImages(token["raw"]).pieces():
                yield piece if isinstance(piece, _InlineImage) else {**token, "raw": piece}
        elif "children" in token:
            children: list[Token] = []
            after_image = False
            for part in _inline_parts(token["children"]):
                if isinstance(part, _InlineImage):
                    fragment = _trim_inline_edge(children, leading=False)
                    if after_image:
                        fragment = _trim_inline_edge(fragment, leading=True)
                    if fragment:
                        yield {**token, "children": fragment}
                    children = []
                    after_image = True
                    yield part
                else:
                    children.append(part)
            if after_image:
                children = _trim_inline_edge(children, leading=True)
            if children:
                yield {**token, "children": children}
        else:
            yield token


def _cell_alt(text: str) -> str:
    # The replacement is literal rich text, not newly introduced Markdown/HTML.
    text = escape(text, quote=False)
    for character in "\\*~`$[]{}|^":
        text = text.replace(character, "\\" + character)
    return text.replace("\n", "<br>")


class _NormalizationRenderer(MarkdownRenderer):
    def __init__(self, document: SourceDocument, run_id: str,
                 failed_image_urls: Collection[str]) -> None:
        super().__init__()
        self.document = document
        self.run_id = run_id
        self.failed_image_urls = failed_image_urls
        self.images: list[ImageOccurrence] = []

    def __call__(self, tokens: Iterable[Token], state: BlockState) -> str:
        return super().__call__(self._normalize_blocks(tokens), state)

    def _image(self, image: _InlineImage, caption: str | None = None) -> Token:
        url = (image.source_url or "").strip()
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
        except ValueError:
            valid = False
        if not valid:
            raise MigrationError(
                f"{self.document.source_path}: image requires an absolute HTTP(S) URL "
                "(relative paths, data URLs, and missing src are unsupported)"
            )
        if url in self.failed_image_urls:
            return {"type": "paragraph", "children": [{"type": "text", "raw": "画像リンク切れ"}]}
        occurrence = ImageOccurrence(
            marker=f"esa2notion-image-{self.run_id}-{len(self.images) + 1}",
            source_url=url,
            caption=image.caption if caption is None else caption,
        )
        self.images.append(occurrence)
        return {"type": "migration_image", "occurrence": occurrence}

    def _normalize_blocks(self, tokens: Iterable[Token]) -> list[Token]:
        result: list[Token] = []
        for token in tokens:
            kind = token["type"]
            if kind in {"paragraph", "block_text", "heading"}:
                parts = list(_inline_parts(token["children"]))
                has_images = any(isinstance(part, _InlineImage) for part in parts)
                children: list[Token] = []
                for part in parts:
                    if isinstance(part, _InlineImage):
                        self._flush_inline(result, token, children, has_images)
                        children = []
                        result.append(self._image(part))
                    else:
                        children.append(part)
                self._flush_inline(result, token, children, has_images)
            elif kind == "block_html":
                for piece in _HTMLImages(token["raw"]).pieces():
                    if isinstance(piece, _InlineImage):
                        result.append(self._image(piece))
                    else:
                        result.append({**token, "raw": piece})
            elif kind == "table":
                table, relocated = self._normalize_table(token)
                result.append(table)
                result.extend(relocated)
            elif "children" in token:
                result.append({**token, "children": self._normalize_blocks(token["children"])})
            else:
                result.append(token)
        return result

    @staticmethod
    def _flush_inline(result: list[Token], token: Token, children: list[Token], split: bool) -> None:
        if not children:
            return
        if split and all(child["type"] in {"text", "softbreak", "linebreak"} for child in children):
            if not _caption(children).strip():
                return
        kind = "paragraph" if split and token["type"] == "block_text" else token["type"]
        result.append({**token, "type": kind, "children": children})

    def _normalize_table(self, token: Token) -> tuple[Token, list[Token]]:
        relocated: list[Token] = []
        row_number = 0

        def cell(token: Token, column: int) -> Token:
            def replace(tokens: Iterable[Token]) -> list[Token]:
                replaced = []
                for child in tokens:
                    if child["type"] == "image":
                        pieces: Iterable[Token | _InlineImage] = [
                            _InlineImage(child["attrs"]["url"], _caption(child["children"]))
                        ]
                    elif child["type"] == "inline_html":
                        pieces = [
                            piece if isinstance(piece, _InlineImage) else {**child, "raw": piece}
                            for piece in _HTMLImages(child["raw"]).pieces()
                        ]
                    elif "children" in child:
                        replaced.append({**child, "children": replace(child["children"])})
                        continue
                    else:
                        replaced.append(child)
                        continue
                    for piece in pieces:
                        if isinstance(piece, _InlineImage):
                            block = self._image(piece, f"表 {row_number}行 {column}列: {piece.caption}")
                            if block["type"] == "paragraph":
                                replaced.append({"type": "text", "raw": "画像リンク切れ"})
                            else:
                                relocated.append(block)
                                replaced.append({"type": "text", "raw": _cell_alt(piece.caption or "画像")})
                        else:
                            replaced.append(piece)
                return replaced

            return {**token, "children": replace(token["children"])}

        sections = []
        for section in token["children"]:
            if section["type"] == "table_head":
                row_number += 1
                sections.append({**section, "children": [cell(value, i) for i, value in enumerate(section["children"], 1)]})
            else:
                rows = []
                for row in section["children"]:
                    row_number += 1
                    rows.append({**row, "children": [cell(value, i) for i, value in enumerate(row["children"], 1)]})
                sections.append({**section, "children": rows})
        return {**token, "children": sections}, relocated

    def migration_image(self, token: Token, state: BlockState) -> str:
        occurrence = token["occurrence"]
        # Angle destinations safely retain parentheses in signed/public URLs.
        url = occurrence.source_url.replace("<", "%3C").replace(">", "%3E").replace("\n", "%0A").replace("\r", "%0D")
        return f"![{occurrence.marker}](<{url}>)\n\n"

    def strikethrough(self, token: Token, state: BlockState) -> str:
        return "~~" + self.render_children(token, state) + "~~"

    def codespan(self, token: Token, state: BlockState) -> str:
        # Mistune's MarkdownRenderer always emits a single backtick, which can
        # turn literal image syntax containing backticks back into live Markdown.
        raw = token["raw"]
        fence = "`" * (max((len(run) for run in re.findall(r"`+", raw)), default=0) + 1)
        needs_padding = raw.startswith("`") or raw.endswith("`") or (
            raw.startswith(" ") and raw.endswith(" ") and bool(raw.strip())
        )
        content = " " + raw + " " if needs_padding else raw
        return fence + content + fence

    def block_code(self, token: Token, state: BlockState) -> str:
        if token.get("style") == "indent":
            # Converting to a fence would add a newline to Mistune's code raw.
            return indent(token["raw"], "    ", lambda _: True) + "\n\n"
        return super().block_code(token, state)

    def table(self, token: Token, state: BlockState) -> str:
        return '<table header-row="true">\n' + self.render_children(token, state) + "</table>\n\n"

    def table_head(self, token: Token, state: BlockState) -> str:
        return "\t<tr>\n" + self.render_children(token, state) + "\t</tr>\n"

    def table_body(self, token: Token, state: BlockState) -> str:
        return self.render_children(token, state)

    def table_row(self, token: Token, state: BlockState) -> str:
        return "\t<tr>\n" + self.render_children(token, state) + "\t</tr>\n"

    def table_cell(self, token: Token, state: BlockState) -> str:
        return "\t\t<td>" + self.render_children(token, state) + "</td>\n"

    def _container_text(self, token: Token, state: BlockState) -> str:
        def breaks(tokens: Iterable[Token]) -> Iterable[Token]:
            for child in tokens:
                if child["type"] in {"softbreak", "linebreak"}:
                    yield {"type": "inline_html", "raw": "<br>"}
                elif "children" in child:
                    yield {**child, "children": list(breaks(child["children"]))}
                else:
                    yield child

        return self.render_tokens(breaks(token["children"]), state)

    def list(self, token: Token, state: BlockState) -> str:
        # Enhanced Markdown uses tab-indented children, not CommonMark's spaces.
        output = []
        start = token["attrs"].get("start", 1)
        for index, item in enumerate(token["children"], start):
            marker = f"{index}. " if token["attrs"]["ordered"] else "- "
            children = [child for child in item["children"] if child["type"] != "blank_line"]
            checkbox = ""
            if item["type"] == "task_list_item":
                checkbox = "[x] " if item["attrs"]["checked"] else "[ ] "
            text = marker + checkbox
            if children and children[0]["type"] in {"paragraph", "block_text"}:
                text += self._container_text(children.pop(0), state)
            text += "\n"
            if children:
                text += indent(self.render_tokens(children, state), "\t", lambda _: True)
            output.append(text.rstrip("\n") + "\n")
        return "\n".join(output) + "\n"

    def block_quote(self, token: Token, state: BlockState) -> str:
        children = [child for child in token["children"] if child["type"] != "blank_line"]
        text = "> "
        if children and children[0]["type"] in {"paragraph", "block_text"}:
            text += self._container_text(children.pop(0), state)
        text += "\n"
        if children:
            text += indent(self.render_tokens(children, state), "\t", lambda _: True)
        return text + "\n"


def _front_matter_block(raw: str | None) -> str:
    if raw is None:
        return ""
    longest = max((len(match.group()) for match in re.finditer(r"`+", raw)), default=0)
    fence = "`" * max(3, longest + 1)
    ending = "" if raw.endswith("\n") else "\n"
    return f"{fence}yaml\n{raw}{ending}{fence}\n\n"


def normalize_document(document: SourceDocument, run_id: str, *,
                       failed_image_urls: Collection[str] = ()) -> NormalizedDocument:
    renderer = _NormalizationRenderer(document, run_id, failed_image_urls)
    parser = mistune.create_markdown(
        renderer=renderer,
        plugins=["table", "task_lists", "strikethrough", table_in_quote, table_in_list],
    )
    body_markdown = parser(document.body)
    markdown = _front_matter_block(document.front_matter) + body_markdown
    return NormalizedDocument(markdown=markdown, images=renderer.images)
