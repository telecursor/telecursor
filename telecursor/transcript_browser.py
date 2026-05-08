"""Read past Cursor chat transcripts from disk.

Cursor stores per-workspace agent transcripts at:

    ~/.cursor/projects/<workspace-slug>/agent-transcripts/<chat-uuid>/<chat-uuid>.jsonl

The slug is the absolute workspace path with leading slash stripped and
remaining `/` replaced with `-`. This is undocumented and may change between
Cursor releases; the bridge treats this best-effort.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CURSOR_PROJECTS = Path.home() / ".cursor" / "projects"

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
USER_QUERY_TAG_RE = re.compile(
    r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL
)


@dataclass
class ChatSummary:
    chat_id: str
    transcript_path: Path
    mtime: float
    line_count: int
    title: str  # short preview of first user message


def slugify_workspace(workspace: str) -> str:
    """Convert /Users/foo/bar -> Users-foo-bar (Cursor's project-dir naming)."""
    p = Path(workspace).expanduser().resolve()
    s = str(p).lstrip("/")
    return s.replace("/", "-")


def transcripts_dir(workspace: str) -> Path:
    return CURSOR_PROJECTS / slugify_workspace(workspace) / "agent-transcripts"


def _extract_first_user_text(path: Path, max_lines: int = 20) -> str:
    """Read first user message from a transcript .jsonl, stripping wrapper tags."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("role") != "user":
                    continue
                content = obj.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            m = USER_QUERY_TAG_RE.search(text)
                            if m:
                                text = m.group(1)
                            return text.strip()
                elif isinstance(content, str):
                    return content.strip()
    except OSError:
        return ""
    return ""


def _count_lines(path: Path) -> int:
    try:
        with path.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _summarize(chat_dir: Path) -> Optional[ChatSummary]:
    if not UUID_RE.match(chat_dir.name):
        return None
    transcript = chat_dir / f"{chat_dir.name}.jsonl"
    if not transcript.is_file():
        return None
    try:
        mtime = transcript.stat().st_mtime
    except OSError:
        return None
    title = _extract_first_user_text(transcript)
    if not title:
        title = "(no user message yet)"
    title_oneline = " ".join(title.split())
    if len(title_oneline) > 80:
        title_oneline = title_oneline[:77] + "..."
    return ChatSummary(
        chat_id=chat_dir.name,
        transcript_path=transcript,
        mtime=mtime,
        line_count=_count_lines(transcript),
        title=title_oneline,
    )


def list_chats(workspace: str, limit: int = 10) -> list[ChatSummary]:
    """Return up to `limit` chat summaries for the workspace, newest first."""
    base = transcripts_dir(workspace)
    if not base.is_dir():
        return []
    summaries: list[ChatSummary] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        s = _summarize(child)
        if s is not None:
            summaries.append(s)
    summaries.sort(key=lambda s: s.mtime, reverse=True)
    return summaries[:limit]


def find_chat_by_prefix(
    workspace: str, prefix: str
) -> tuple[Optional[ChatSummary], list[ChatSummary]]:
    """Resolve a chat id from a (possibly truncated) prefix.

    Returns (match, ambiguous) where:
      - match is the unique ChatSummary if the prefix matches exactly one chat
      - ambiguous is a list of candidates if more than one matched (match=None)
      - both are None/empty if nothing matched
    """
    prefix = prefix.strip().lower()
    if not prefix:
        return None, []
    base = transcripts_dir(workspace)
    if not base.is_dir():
        return None, []
    matches: list[ChatSummary] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        if not child.name.lower().startswith(prefix):
            continue
        s = _summarize(child)
        if s is not None:
            matches.append(s)
    if len(matches) == 1:
        return matches[0], []
    if len(matches) > 1:
        matches.sort(key=lambda s: s.mtime, reverse=True)
        return None, matches
    return None, []
