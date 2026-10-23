"""Read esa exports without extracting ZIP members or following symlinks."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
import os
import stat
import zipfile

import frontmatter
from frontmatter.default_handlers import YAMLHandler
import yaml

from .errors import MigrationError


@dataclass(frozen=True)
class SourceDocument:
    source_path: str
    title: str
    category_parts: tuple[str, ...]
    body: str
    front_matter: str | None


def _extract_yaml_front_matter(text: str) -> str | None:
    candidate = text.lstrip()
    handler = YAMLHandler()
    if not handler.detect(candidate):
        return None
    boundaries = handler.FM_BOUNDARY.finditer(candidate)
    next(boundaries, None)
    closing = next(boundaries, None)
    if closing is None:
        return None
    # The boundary's \s* can consume blank lines and body indentation.
    newline = candidate.find("\n", closing.start())
    end = newline + 1 if newline != -1 else len(candidate)
    return candidate[:end]


def _parse(name: str, data: bytes) -> SourceDocument:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MigrationError(f"{name}: UTF-8として読み込めません") from exc
    try:
        post = frontmatter.loads(text)
    except (yaml.YAMLError, ValueError, TypeError) as exc:
        raise MigrationError(f"{name}: YAML front matterが不正です") from exc
    values = {}
    for key in ("title", "category"):
        value = post.get(key)
        if value is None or value == "":
            value = ""
        elif not isinstance(value, str):
            raise MigrationError(f"{name}: {key}は文字列で指定してください")
        values[key] = value.strip()
    return SourceDocument(
        name,
        values["title"] or PurePosixPath(name).stem,
        tuple(part for part in values["category"].split("/") if part),
        post.content,
        _extract_yaml_front_matter(text),
    )


def _is_article(path: PurePosixPath) -> bool:
    return "__MACOSX" not in path.parts and path.suffix.lower() in {".md", ".markdown"}


def load_documents(source: Path) -> list[SourceDocument]:
    """Validate and read every article before any remote modification."""
    source = Path(source)
    if source.is_symlink():
        raise MigrationError("入力のsymlinkは使用できません")
    try:
        articles: list[tuple[str, bytes]] = []
        if source.is_dir():
            for root, dirs, files in os.walk(source, followlinks=False):
                dirs[:] = sorted(d for d in dirs if d != "__MACOSX" and not (Path(root) / d).is_symlink())
                for filename in files:
                    path = Path(root) / filename
                    name = path.relative_to(source).as_posix()
                    if not path.is_symlink() and _is_article(PurePosixPath(name)):
                        articles.append((name, path.read_bytes()))
        elif source.is_file():
            with zipfile.ZipFile(source) as archive:
                seen = set()
                for member in archive.infolist():
                    path = PurePosixPath(member.filename)
                    if (path.is_absolute() or ".." in path.parts or "\\" in member.filename
                            or PureWindowsPath(member.filename).drive
                            or stat.S_ISLNK(member.external_attr >> 16)):
                        raise MigrationError(f"ZIPの安全でないメンバー: {member.filename}")
                    if not member.is_dir() and _is_article(path):
                        name = path.as_posix()
                        if name in seen:
                            raise MigrationError(f"ZIPの記事パスが重複しています: {name}")
                        seen.add(name)
                        articles.append((name, archive.read(member)))
        else:
            raise MigrationError(f"入力が存在しません: {source}")
        if not articles:
            raise MigrationError("入力にMarkdown記事がありません")
        return [_parse(name, data) for name, data in sorted(articles)]
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise MigrationError("入力のディレクトリまたはZIPを読み込めません") from exc
