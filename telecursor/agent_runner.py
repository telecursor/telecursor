"""Async wrapper around the `agent` CLI in headless (-p) mode."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    ok: bool
    text: str
    session_id: Optional[str]
    raw_stdout: str
    raw_stderr: str
    exit_code: int


def _resolve_bin() -> str:
    explicit = os.environ.get("AGENT_BIN")
    if explicit:
        return explicit
    found = shutil.which("agent")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "agent"
    if fallback.exists():
        return str(fallback)
    raise FileNotFoundError(
        "Could not find `agent` binary. Install via "
        "`curl https://cursor.com/install -fsS | bash` or set AGENT_BIN."
    )


def _extract_text_and_session(stdout: str) -> tuple[str, Optional[str]]:
    """Best-effort parse of `agent --output-format json` stdout.

    The on-the-wire JSON shape isn't formally pinned in public docs, so we
    accept both single-document and NDJSON, and try several common key names.
    """
    stdout = stdout.strip()
    if not stdout:
        return "", None

    last_text: Optional[str] = None
    session_id: Optional[str] = None
    parsed_any = False

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        parsed_any = True
        for key in ("session_id", "chatId", "chat_id", "id"):
            v = obj.get(key)
            if isinstance(v, str):
                session_id = v
                break
        for key in ("result", "text", "content", "message"):
            v = obj.get(key)
            if isinstance(v, str):
                last_text = v
                break

    if parsed_any and last_text is not None:
        return last_text, session_id

    try:
        obj = json.loads(stdout)
        if isinstance(obj, dict):
            text = (
                obj.get("result")
                or obj.get("text")
                or obj.get("content")
                or stdout
            )
            sid = (
                obj.get("session_id")
                or obj.get("chatId")
                or obj.get("chat_id")
                or session_id
            )
            return str(text), sid if isinstance(sid, str) else None
    except json.JSONDecodeError:
        pass

    return stdout, session_id


async def run_agent(
    prompt: str,
    *,
    workspace: str,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    timeout_sec: float = 600.0,
) -> AgentResult:
    """Invoke `agent -p` and return parsed result."""
    bin_path = _resolve_bin()

    cmd: list[str] = [
        bin_path,
        "-p",
        "--force",
        "--approve-mcps",
        "--output-format", "json",
        "--workspace", workspace,
    ]
    if session_id:
        cmd.extend(["--resume", session_id])
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)

    logger.info(
        "Launching agent: workspace=%s session=%s prompt_len=%d",
        workspace, session_id, len(prompt),
    )

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,  # critical: agent -p hangs forever waiting on inherited stdin
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace,
    )
    try:
        if timeout_sec:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_sec
            )
        else:
            stdout_b, stderr_b = await proc.communicate()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return AgentResult(
            ok=False,
            text=f"Agent timed out after {timeout_sec:.0f}s.",
            session_id=session_id,
            raw_stdout="",
            raw_stderr="timeout",
            exit_code=-1,
        )

    stdout = stdout_b.decode(errors="replace")
    stderr = stderr_b.decode(errors="replace")
    text, new_sid = _extract_text_and_session(stdout)

    if proc.returncode != 0:
        last_stderr = stderr.strip().splitlines()[-1] if stderr.strip() else "(no stderr)"
        msg = f"Agent exited {proc.returncode}: {last_stderr}"
        if text:
            msg += f"\n\n--- partial output ---\n{text}"
        return AgentResult(
            ok=False,
            text=msg,
            session_id=new_sid or session_id,
            raw_stdout=stdout,
            raw_stderr=stderr,
            exit_code=proc.returncode,
        )

    if not text:
        text = "(agent returned no text)"

    return AgentResult(
        ok=True,
        text=text,
        session_id=new_sid or session_id,
        raw_stdout=stdout,
        raw_stderr=stderr,
        exit_code=0,
    )
