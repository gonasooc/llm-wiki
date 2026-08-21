import importlib.util
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = (
    ROOT
    / ".hermes"
    / "skills"
    / "hermes-wiki"
    / "scripts"
    / "publish_note.py"
)
CONFIG_LOCK = threading.Lock()


BODY = """## 들어가며

이 곡을 이해하기 위한 출발점입니다. [1]

## 작품과 녹음

작품과 녹음의 특징입니다. [1]

## 배경과 맥락

발표 당시의 배경입니다. [2]

## 듣기의 포인트

직접 들으며 확인할 지점입니다.

## 크레딧과 검증

확인된 크레딧입니다. [2]

## 함께 탐색할 것

이어 들을 작품입니다.

## 출처와 불확실성

- 불확실한 사항 없음

## Sources

[1] https://www.youtube.com/watch?v=afSgBNwmZrQ — Official source
[2] https://example.com/interview — Interview
"""


def _load_module():
    spec = importlib.util.spec_from_file_location("hermes_wiki_publish", PUBLISHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ledger(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "ledger.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [
                    {
                        "id": 1,
                        "url": "https://www.youtube.com/watch?v=afSgBNwmZrQ",
                        "title": "Official source",
                    },
                    {
                        "id": 2,
                        "url": "https://example.com/interview",
                        "title": "Interview",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _configure(vault: Path) -> Path:
    with CONFIG_LOCK:
        home = vault.parent / ".test-hermes-home"
        config = home / "plugin-data" / "hermes-wiki-router" / "publishing.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps({"vault_path": str(vault.absolute())}),
            encoding="utf-8",
        )
        os.environ["HERMES_HOME"] = str(home)
    return config


def _publish(
    module,
    vault,
    *,
    title="A Song",
    body=BODY,
    now=None,
    source_count=2,
    ledger=None,
):
    _configure(Path(vault))
    return module.publish_note(
        vault=vault,
        ledger=ledger or _ledger(Path(vault)),
        video_id="afSgBNwmZrQ",
        title=title,
        description="음악 블로그 초안",
        canonical_url="https://youtu.be/afSgBNwmZrQ",
        thread_id="1540169358423105609",
        source_count=source_count,
        transcript_status="language-not-verified",
        tags=["music"],
        body=body,
        now=now,
    )


def test_publish_note_creates_safe_draft_with_frontmatter(tmp_path):
    module = _load_module()
    now = datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc)
    _configure(tmp_path)

    output = module.publish_note(
        vault=tmp_path,
        ledger=_ledger(tmp_path),
        video_id="afSgBNwmZrQ",
        title="../../나쁜:제목 / A Song",
        description="곡과 녹음의 맥락을 정리한 초안",
        canonical_url="https://www.youtube.com/watch?v=afSgBNwmZrQ",
        thread_id="1540169358423105609",
        source_count=2,
        transcript_status="unavailable",
        tags=["music", "음악", "music"],
        body=BODY,
        now=now,
    )

    assert output.parent == tmp_path / "articles"
    assert output.name == "afSgBNwmZrQ.md"
    text = output.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert 'title: "../../나쁜:제목 / A Song"' in text
    assert "status: draft" in text
    assert "draft: true" in text
    assert "youtube_id: afSgBNwmZrQ" in text
    assert "discord_thread_id: '1540169358423105609'" in text
    assert "source_count: 2" in text
    assert "transcript_status: unavailable" in text
    assert 'tags: ["music", "음악"]' in text
    assert 'created: "2026-08-21T04:00:00+00:00"' in text
    assert 'updated: "2026-08-21T04:00:00+00:00"' in text
    assert BODY.strip() in text


def test_publish_note_refuses_duplicate_without_update(tmp_path):
    module = _load_module()
    _publish(module, tmp_path)

    with pytest.raises(FileExistsError):
        _publish(module, tmp_path)


def test_concurrent_different_titles_create_exactly_one_note(tmp_path, monkeypatch):
    module = _load_module()
    ledger = _ledger(tmp_path)
    real_open = module.os.open
    barrier = threading.Barrier(2)

    def synchronized_open(path, flags, *args, **kwargs):
        if path == "afSgBNwmZrQ.md" and flags & os.O_EXCL:
            barrier.wait(timeout=2)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", synchronized_open)

    def create(title):
        try:
            return _publish(module, tmp_path, title=title, ledger=ledger)
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ["Title One", "Title Two"]))

    assert sum(result is not None for result in results) == 1
    assert [path.name for path in (tmp_path / "articles").glob("*.md")] == [
        "afSgBNwmZrQ.md"
    ]


def test_articles_symlink_swap_fails_closed(tmp_path, monkeypatch):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    module = _load_module()
    _ledger(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    real_open = module.os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        fd = real_open(path, flags, *args, **kwargs)
        if path == "afSgBNwmZrQ.md" and flags & os.O_EXCL:
            articles = tmp_path / "articles"
            moved = tmp_path / "articles-original"
            articles.rename(moved)
            articles.symlink_to(outside, target_is_directory=True)
            swapped = True
        return fd

    monkeypatch.setattr(module.os, "open", swapping_open)

    with pytest.raises(RuntimeError, match="directory chain changed"):
        _publish(module, tmp_path, ledger=tmp_path / "ledger.json")

    assert swapped is True
    assert list(outside.iterdir()) == []
    assert not (tmp_path / "articles-original" / "afSgBNwmZrQ.md").exists()


def test_vault_ancestor_symlink_is_rejected(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    module = _load_module()
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="publishing configuration is invalid"):
        _publish(module, linked / "vault", ledger=_ledger(tmp_path))


def test_ledger_symlink_is_rejected(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    module = _load_module()
    ledger = _ledger(tmp_path / "real-ledger")
    linked = tmp_path / "ledger-link.json"
    linked.symlink_to(ledger)

    with pytest.raises(ValueError, match="citation ledger"):
        _publish(module, tmp_path / "vault", ledger=linked)


def test_markdown_title_is_escaped_in_h1(tmp_path):
    module = _load_module()
    output = _publish(
        module,
        tmp_path,
        title="[Click](https://evil.example)<b>",
    )

    h1 = next(
        line for line in output.read_text(encoding="utf-8").splitlines() if line.startswith("# ")
    )
    assert "[Click](" not in h1
    assert "<b>" not in h1
    assert "&lt;b&gt;" in h1


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"video_id": "bad"}, "invalid YouTube video ID"),
        ({"canonical_url": "https://example.com/afSgBNwmZrQ"}, "canonical URL"),
        ({"canonical_url": "http://youtu.be/afSgBNwmZrQ"}, "canonical URL"),
        ({"thread_id": "thread/1"}, "invalid Discord thread ID"),
        ({"title": "bad\ntitle"}, "control or line-breaking"),
        ({"title": "bad\u2028## injected"}, "control or line-breaking"),
        ({"description": "bad\u0085description"}, "control characters"),
        ({"body": "## 들어가며\n불완전"}, "missing required headings"),
        ({"transcript_status": "guessed-ko"}, "invalid transcript_status"),
    ],
)
def test_publish_note_rejects_invalid_inputs(tmp_path, overrides, message):
    module = _load_module()
    _configure(tmp_path)
    kwargs = {
        "vault": tmp_path,
        "ledger": _ledger(tmp_path),
        "video_id": "afSgBNwmZrQ",
        "title": "A Song",
        "description": "음악 블로그 초안",
        "canonical_url": "https://youtu.be/afSgBNwmZrQ",
        "thread_id": "1540169358423105609",
        "source_count": 2,
        "transcript_status": "unavailable",
        "tags": ["music"],
        "body": BODY,
    }
    kwargs.update(overrides)

    with pytest.raises(ValueError, match=message):
        module.publish_note(**kwargs)


def test_publish_note_rejects_citation_and_heading_mismatches(tmp_path):
    module = _load_module()
    ledger = _ledger(tmp_path)

    with pytest.raises(ValueError, match="source_count"):
        _publish(module, tmp_path, ledger=ledger, source_count=1)

    fake_source = BODY.replace(
        "[2] https://example.com/interview — Interview",
        "[2] https://evil.example/fabricated — Fabricated",
    )
    with pytest.raises(ValueError, match="Sources block"):
        _publish(module, tmp_path, ledger=ledger, body=fake_source)

    unknown_citation = BODY.replace("출발점입니다. [1]", "출발점입니다. [99]")
    with pytest.raises(ValueError, match="unknown inline citation"):
        _publish(module, tmp_path, ledger=ledger, body=unknown_citation)

    duplicated = BODY.replace("## 작품과 녹음", "## 들어가며\n\n중복\n\n## 작품과 녹음")
    with pytest.raises(ValueError, match="exactly once"):
        _publish(module, tmp_path, ledger=ledger, body=duplicated)

    reordered = BODY.replace("## 들어가며", "## TEMP", 1).replace(
        "## 작품과 녹음", "## 들어가며", 1
    ).replace("## TEMP", "## 작품과 녹음", 1)
    with pytest.raises(ValueError, match="required order"):
        _publish(module, tmp_path, ledger=ledger, body=reordered)

    extra_blank = BODY.replace("## Sources\n\n", "## Sources\n\n\n")
    with pytest.raises(ValueError, match="exactly match"):
        _publish(module, tmp_path, ledger=ledger, body=extra_blank)


@pytest.mark.parametrize(
    ("url", "title"),
    [
        ("http://example.com/plain", "Plain title"),
        ("https://user:pass@example.com/private", "Private title"),
    ],
)
def test_publish_note_rejects_unsafe_ledger_metadata(tmp_path, url, title):
    module = _load_module()
    ledger = tmp_path / "unsafe-ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [
                    {
                        "id": 1,
                        "url": "https://youtu.be/afSgBNwmZrQ",
                        "title": "Official source",
                    },
                    {"id": 2, "url": url, "title": title},
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="citation ledger source"):
        _publish(module, tmp_path / "vault", ledger=ledger)


def test_publish_note_escapes_markdown_active_source_title(tmp_path):
    module = _load_module()
    title = "<img src=x onerror=alert(1)>"
    ledger = tmp_path / "active-title-ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [
                    {
                        "id": 1,
                        "url": "https://www.youtube.com/watch?v=afSgBNwmZrQ",
                        "title": "Official source",
                    },
                    {"id": 2, "url": "https://example.com/page", "title": title},
                ],
            }
        ),
        encoding="utf-8",
    )
    body = BODY.replace("Interview", title).replace(
        "https://example.com/interview", "https://example.com/page"
    )

    output = _publish(module, tmp_path / "vault", ledger=ledger, body=body)

    text = output.read_text(encoding="utf-8")
    assert title not in text
    assert r"&lt;img src=x onerror=alert\(1\)&gt;" in text


def test_requested_vault_must_match_profile_configuration(tmp_path):
    module = _load_module()
    configured = tmp_path / "configured-vault"
    _configure(configured)

    with pytest.raises(ValueError, match="does not match"):
        module.publish_note(
            vault=tmp_path / "other-vault",
            ledger=_ledger(tmp_path),
            video_id="afSgBNwmZrQ",
            title="A Song",
            description="draft",
            canonical_url="https://youtu.be/afSgBNwmZrQ",
            thread_id="1540169358423105609",
            source_count=2,
            transcript_status="unavailable",
            tags=["music"],
            body=BODY,
        )


def test_cli_request_file_handles_leading_hyphen_values(tmp_path):
    vault = tmp_path / "vault"
    _configure(vault)
    ledger = _ledger(tmp_path)
    body = tmp_path / "body.md"
    body.write_text(BODY, encoding="utf-8")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "vault": str(vault),
                "ledger": str(ledger),
                "video_id": "afSgBNwmZrQ",
                "title": "- remix",
                "description": "- draft description",
                "canonical_url": "https://youtu.be/afSgBNwmZrQ",
                "thread_id": "1540169358423105609",
                "source_count": 2,
                "transcript_status": "unavailable",
                "tags": ["-music"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(PUBLISHER),
            "--request-file",
            str(request),
            "--body-file",
            str(body),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).is_file()
