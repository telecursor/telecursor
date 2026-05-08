"""Persists per-chat agent session state to a JSON file."""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock
from typing import Optional

DEFAULT_PATH = Path.home() / ".local" / "share" / "telecursor" / "state.json"


@dataclass
class ChatState:
    chat_id: int
    session_id: Optional[str] = None
    workspace: Optional[str] = None
    last_active: float = field(default_factory=time.time)


class SessionStore:
    """Thread-safe JSON-backed map of telegram chat_id -> ChatState."""

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._chats: dict[int, ChatState] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            for cid_str, data in raw.get("chats", {}).items():
                cid = int(cid_str)
                self._chats[cid] = ChatState(
                    chat_id=cid,
                    session_id=data.get("session_id"),
                    workspace=data.get("workspace"),
                    last_active=data.get("last_active", time.time()),
                )
        except (json.JSONDecodeError, ValueError, KeyError):
            # Corrupt state file shouldn't crash the daemon — start fresh.
            self._chats = {}

    def _save_locked(self) -> None:
        payload = {
            "chats": {
                str(c.chat_id): {
                    "session_id": c.session_id,
                    "workspace": c.workspace,
                    "last_active": c.last_active,
                }
                for c in self._chats.values()
            }
        }
        tmp = tempfile.NamedTemporaryFile(
            mode="w", dir=self.path.parent, delete=False, suffix=".tmp"
        )
        try:
            json.dump(payload, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, self.path)
        except Exception:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise

    def get(self, chat_id: int) -> ChatState:
        with self._lock:
            if chat_id not in self._chats:
                self._chats[chat_id] = ChatState(chat_id=chat_id)
            return ChatState(**asdict(self._chats[chat_id]))

    def update(self, chat_id: int, **kwargs) -> ChatState:
        with self._lock:
            state = self._chats.setdefault(chat_id, ChatState(chat_id=chat_id))
            for k, v in kwargs.items():
                if not hasattr(state, k):
                    raise AttributeError(f"ChatState has no field {k!r}")
                setattr(state, k, v)
            state.last_active = time.time()
            self._save_locked()
            return ChatState(**asdict(state))

    def reset(self, chat_id: int) -> None:
        with self._lock:
            if chat_id in self._chats:
                self._chats[chat_id].session_id = None
                self._chats[chat_id].last_active = time.time()
                self._save_locked()
