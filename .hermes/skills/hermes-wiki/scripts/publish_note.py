from __future__ import annotations

import argparse
import html
import json
import os
import re

import stat
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
THREAD_ID_RE = re.compile(r"^[0-9]{1,32}$")
INLINE_CITATION_RE = re.compile(r"\[(\d+)]")
MARKDOWN_ESCAPE_RE = re.compile(r"([\\`*_[\]{}()#+.!|])")
REQUIRED_HEADINGS = (
    "## 들어가며",
    "## 작품과 녹음",
    "## 배경과 맥락",
    "## 듣기의 포인트",
    "## 크레딧과 검증",
    "## 함께 탐색할 것",
    "## 출처와 불확실성",
    "## Sources",
)
TRANSCRIPT_STATUSES = {
    "available",
    "unavailable",
    "language-not-verified",
}
MAX_BODY_BYTES = 1_000_000
MAX_LEDGER_BYTES = 5_000_000
MAX_REQUEST_BYTES = 64_000


def _youtube_video_id(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            return None
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.port is not None:
            return None
    except (TypeError, ValueError):
        return None

    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }:
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.path.rstrip("/") == "/watch":
            values = parse_qs(parsed.query, keep_blank_values=True).get("v", [])
            candidate = values[0] if len(values) == 1 else ""
        elif len(parts) == 2 and parts[0] in {"shorts", "live", "embed"}:
            candidate = parts[1]
        else:
            candidate = ""
    else:
        return None
    return candidate if VIDEO_ID_RE.fullmatch(candidate or "") else None


def _has_unsafe_unicode(value: str) -> bool:
    return any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in value
    )


def _validate_source_url(url: str) -> str:
    if _has_unsafe_unicode(url) or any(character.isspace() for character in url):
        raise ValueError("citation ledger source URL is unsafe")
    if any(character in "<>" for character in url):
        raise ValueError("citation ledger source URL is Markdown-active")
    try:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or not parsed.hostname
        ):
            raise ValueError("citation ledger source URL must be credential-free HTTPS")
        _ = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("citation ledger source URL is invalid") from exc
    return url


def _validate_source_title(title: str) -> str:
    if _has_unsafe_unicode(title):
        raise ValueError("citation ledger source title contains unsafe Unicode")
    return title


def _validate_body(body: str) -> tuple[str, int]:
    normalized = body.strip()
    if not normalized or len(normalized.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError("body must be non-empty and at most 1MB")
    if normalized.startswith("---"):
        raise ValueError("body must not contain frontmatter")

    lines = normalized.splitlines()
    indexes: list[int] = []
    for heading in REQUIRED_HEADINGS:
        occurrences = [index for index, line in enumerate(lines) if line == heading]
        if not occurrences:
            raise ValueError(f"body is missing required headings: {heading}")
        if len(occurrences) != 1:
            raise ValueError(f"required heading must appear exactly once: {heading}")
        indexes.append(occurrences[0])
    if indexes != sorted(indexes):
        raise ValueError("required headings are not in the required order")
    return normalized, indexes[-1]


def _secure_directory_flags() -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("secure vault publishing is unsupported on this platform")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _absolute_path(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _close_fds(fds: list[int]) -> None:
    for fd in reversed(fds):
        try:
            os.close(fd)
        except OSError:
            pass


def _open_directory_chain(
    path: str | Path,
    *,
    create: bool,
) -> tuple[list[int], list[tuple[int, str, int]]]:
    flags = _secure_directory_flags()
    absolute = _absolute_path(path)
    if not absolute.is_absolute() or not absolute.parts or absolute.parts[0] != "/":
        raise RuntimeError("secure path walking requires an absolute POSIX path")

    fds = [os.open("/", flags)]
    links: list[tuple[int, str, int]] = []
    try:
        for component in absolute.parts[1:]:
            if component in {"", ".", ".."}:
                raise ValueError("unsafe path component")
            parent_fd = fds[-1]
            if create:
                try:
                    os.mkdir(component, mode=0o755, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            child_fd = os.open(component, flags, dir_fd=parent_fd)
            links.append((parent_fd, component, child_fd))
            fds.append(child_fd)
        return fds, links
    except OSError as exc:
        _close_fds(fds)
        raise RuntimeError("cannot securely walk path components") from exc
    except Exception:
        _close_fds(fds)
        raise


def _assert_chain_unchanged(links: list[tuple[int, str, int]]) -> None:
    for parent_fd, component, child_fd in links:
        current = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(child_fd)
        if (
            not stat.S_ISDIR(current.st_mode)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            raise RuntimeError("directory chain changed during publishing")


def _read_secure_file(path: str | Path, *, max_bytes: int) -> str:
    absolute = _absolute_path(path)
    fds, links = _open_directory_chain(absolute.parent, create=False)
    fd = -1
    try:
        _assert_chain_unchanged(links)
        fd = os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fds[-1])
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise ValueError("secure file size or type is invalid")

        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            chunk = os.read(fd, min(65_536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > max_bytes:
            raise ValueError("secure file exceeds size limit")

        after = os.fstat(fd)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise RuntimeError("secure file changed during read")
        _assert_chain_unchanged(links)
        return b"".join(chunks).decode("utf-8")
    finally:
        if fd >= 0:
            os.close(fd)
        _close_fds(fds)


def _load_ledger(ledger: str | Path, video_id: str) -> list[dict]:
    try:
        payload = json.loads(_read_secure_file(ledger, max_bytes=MAX_LEDGER_BYTES))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        raise ValueError("citation ledger is unavailable or invalid") from exc

    sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(sources, list) or not sources:
        raise ValueError("citation ledger has no sources")
    seen: set[int] = set()
    normalized: list[dict] = []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("citation ledger source is invalid")
        source_id = source.get("id")
        url = source.get("url")
        title = source.get("title")
        if (
            not isinstance(source_id, int)
            or isinstance(source_id, bool)
            or source_id <= 0
            or source_id in seen
            or not isinstance(url, str)
            or not url.strip()
            or not isinstance(title, str)
            or not title.strip()
            or "\n" in url
            or "\n" in title
        ):
            raise ValueError("citation ledger source fields are invalid")
        safe_url = _validate_source_url(url)
        safe_title = _validate_source_title(title)
        seen.add(source_id)
        normalized.append({"id": source_id, "url": safe_url, "title": safe_title})
    normalized.sort(key=lambda source: source["id"])
    if not any(_youtube_video_id(source["url"]) == video_id for source in normalized):
        raise ValueError("citation ledger does not contain the canonical video")
    return normalized


def _validate_citations(
    body: str,
    sources_heading_index: int,
    sources: list[dict],
    source_count: int,
) -> str:
    if source_count != len(sources):
        raise ValueError("source_count does not match citation ledger")
    lines = body.splitlines()
    expected_lines = [
        "",
        *(f"[{source['id']}] {source['url']} — {source['title']}" for source in sources),
    ]
    actual_lines = lines[sources_heading_index + 1 :]
    if actual_lines != expected_lines:
        raise ValueError("Sources block does not exactly match citation ledger")

    valid_ids = {source["id"] for source in sources}
    prose = "\n".join(lines[:sources_heading_index])
    used_ids = {int(value) for value in INLINE_CITATION_RE.findall(prose)}
    unknown = used_ids - valid_ids
    if unknown:
        raise ValueError(f"unknown inline citation IDs: {sorted(unknown)}")
    if not used_ids:
        raise ValueError("body has no inline citations")

    safe_lines = lines[: sources_heading_index + 1]
    safe_lines.append("")
    safe_lines.extend(
        f"[{source['id']}] {source['url']} — {_markdown_text(source['title'])}"
        for source in sources
    )
    return "\n".join(safe_lines)


def _validate_title(title: str) -> str:
    clean = str(title).strip()
    if not clean or len(clean) > 300:
        raise ValueError("title must be 1-300 characters")
    if _has_unsafe_unicode(clean):
        raise ValueError("title contains control or line-breaking characters")
    return clean


def _markdown_text(value: str) -> str:
    escaped = MARKDOWN_ESCAPE_RE.sub(r"\\\1", value)
    return html.escape(escaped, quote=False)


def _markdown_heading(title: str) -> str:
    return _markdown_text(title)


def _configured_vault(requested_vault: str | Path) -> Path:
    hermes_home = _absolute_path(
        os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
    )
    config_path = (
        hermes_home
        / "plugin-data"
        / "hermes-wiki-router"
        / "publishing.json"
    )
    try:
        payload = json.loads(
            _read_secure_file(config_path, max_bytes=MAX_REQUEST_BYTES)
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("profile-local publishing configuration is invalid") from exc
    vault_value = payload.get("vault_path") if isinstance(payload, dict) else None
    if not isinstance(vault_value, str) or not Path(vault_value).expanduser().is_absolute():
        raise ValueError("configured vault_path must be absolute")
    configured = _absolute_path(vault_value)
    requested = _absolute_path(requested_vault)
    if requested != configured:
        raise ValueError("requested vault does not match profile-local configuration")
    return configured


def _open_articles(
    vault_path: Path,
) -> tuple[list[int], list[tuple[int, str, int]], int]:
    fds, links = _open_directory_chain(vault_path, create=True)
    vault_fd = fds[-1]
    try:
        try:
            os.mkdir("articles", mode=0o755, dir_fd=vault_fd)
        except FileExistsError:
            pass
        articles_fd = os.open(
            "articles",
            _secure_directory_flags(),
            dir_fd=vault_fd,
        )
        fds.append(articles_fd)
        links.append((vault_fd, "articles", articles_fd))
        _assert_chain_unchanged(links)
    except Exception:
        _close_fds(fds)
        raise
    return fds, links, articles_fd


def publish_note(
    *,
    vault: str | Path,
    ledger: str | Path,
    video_id: str,
    title: str,
    description: str,
    canonical_url: str,
    thread_id: str,
    source_count: int,
    transcript_status: str,
    tags: list[str],
    body: str,
    now: datetime | None = None,
) -> Path:
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError("invalid YouTube video ID")
    if _youtube_video_id(canonical_url) != video_id:
        raise ValueError("canonical URL does not match video ID")
    if not THREAD_ID_RE.fullmatch(thread_id):
        raise ValueError("invalid Discord thread ID")

    clean_title = _validate_title(title)
    clean_description = str(description).strip()
    if not clean_description or len(clean_description) > 500:
        raise ValueError("description must be 1-500 characters")
    if _has_unsafe_unicode(clean_description):
        raise ValueError("description contains control characters")
    if not isinstance(source_count, int) or isinstance(source_count, bool) or source_count < 0:
        raise ValueError("source_count must be a non-negative integer")
    if transcript_status not in TRANSCRIPT_STATUSES:
        raise ValueError("invalid transcript_status")

    clean_body, sources_index = _validate_body(body)
    sources = _load_ledger(ledger, video_id)
    clean_body = _validate_citations(
        clean_body,
        sources_index,
        sources,
        source_count,
    )
    clean_tags = list(dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip()))
    if any(_has_unsafe_unicode(tag) for tag in clean_tags):
        raise ValueError("tag contains control characters")

    vault_path = _configured_vault(vault)
    output_name = f"{video_id}.md"
    output = vault_path / "articles" / output_name
    chain_fds: list[int] = []
    chain_links: list[tuple[int, str, int]] = []
    articles_fd = -1
    output_fd = -1
    created_identity: tuple[int, int] | None = None
    try:
        chain_fds, chain_links, articles_fd = _open_articles(vault_path)
        _assert_chain_unchanged(chain_links)

        timestamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
        frontmatter = "\n".join(
            (
                "---",
                f"title: {json.dumps(clean_title, ensure_ascii=False)}",
                f"description: {json.dumps(clean_description, ensure_ascii=False)}",
                "status: draft",
                "draft: true",
                "type: music-research",
                f"created: {json.dumps(timestamp)}",
                f"updated: {json.dumps(timestamp)}",
                f"youtube_url: {json.dumps(canonical_url, ensure_ascii=False)}",
                f"youtube_id: {video_id}",
                f"discord_thread_id: '{thread_id}'",
                f"source_count: {source_count}",
                f"transcript_status: {transcript_status}",
                f"tags: {json.dumps(clean_tags, ensure_ascii=False)}",
                "---",
            )
        )
        content = (
            f"{frontmatter}\n\n# {_markdown_heading(clean_title)}\n\n{clean_body}\n"
        ).encode("utf-8")
        try:
            output_fd = os.open(
                output_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=articles_fd,
            )
        except FileExistsError as exc:
            raise FileExistsError(f"note already exists: {output}") from exc

        opened = os.fstat(output_fd)
        created_identity = (opened.st_dev, opened.st_ino)
        _assert_chain_unchanged(chain_links)
        offset = 0
        while offset < len(content):
            offset += os.write(output_fd, content[offset:])
        os.fsync(output_fd)
        os.fchmod(output_fd, 0o644)
        os.fsync(output_fd)

        current = os.stat(output_name, dir_fd=articles_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != created_identity:
            raise RuntimeError("canonical output changed during publishing")
        _assert_chain_unchanged(chain_links)
        os.fsync(articles_fd)
    except Exception:
        if created_identity is not None and articles_fd >= 0:
            try:
                current = os.stat(
                    output_name,
                    dir_fd=articles_fd,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) == created_identity:
                    os.unlink(output_name, dir_fd=articles_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        if output_fd >= 0:
            os.close(output_fd)
        _close_fds(chain_fds)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a Hermes Wiki draft note")
    parser.add_argument("--request-file", required=True)
    parser.add_argument("--body-file", required=True)
    args = parser.parse_args()

    request = json.loads(
        _read_secure_file(args.request_file, max_bytes=MAX_REQUEST_BYTES)
    )
    if not isinstance(request, dict):
        raise ValueError("request file must contain a JSON object")
    output = publish_note(
        vault=request["vault"],
        ledger=request["ledger"],
        video_id=request["video_id"],
        title=request["title"],
        description=request["description"],
        canonical_url=request["canonical_url"],
        thread_id=request["thread_id"],
        source_count=request["source_count"],
        transcript_status=request["transcript_status"],
        tags=request.get("tags", []),
        body=_read_secure_file(args.body_file, max_bytes=MAX_BODY_BYTES),
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
