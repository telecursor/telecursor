"""Telegram <-> Cursor Agent CLI bridge daemon."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Optional

from dotenv import load_dotenv
from telegram import BotCommand, MessageEntity as TgMessageEntity, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegramify_markdown import telegramify
from telegramify_markdown.content import ContentType

from .agent_runner import run_agent
from .session_store import SessionStore
from .transcript_browser import (
    ChatSummary,
    find_chat_by_prefix,
    list_chats,
)

logger = logging.getLogger("telecursor.daemon")

MAX_TG_MSG_LEN: Final[int] = 4000  # leave headroom under Telegram's 4096 limit
ATTACH_CONFIRM_TTL_SEC: Final[float] = 120.0  # how long /attach pending state survives


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _to_tg_entities(entities) -> list[TgMessageEntity]:
    """Convert telegramify-markdown's MessageEntity dataclass to PTB MessageEntity."""
    if not entities:
        return []
    return [
        TgMessageEntity(
            type=e.type,
            offset=e.offset,
            length=e.length,
            url=e.url,
            language=e.language,
            custom_emoji_id=e.custom_emoji_id,
        )
        for e in entities
    ]


class Bridge:
    def __init__(self) -> None:
        load_dotenv()
        self.bot_token = self._require_env("TELEGRAM_BOT_TOKEN")
        self.allowed_ids = self._parse_ids(os.environ.get("TG_ALLOWED_USER_IDS", ""))
        if not self.allowed_ids:
            raise SystemExit(
                "TG_ALLOWED_USER_IDS is empty. Refusing to run an "
                "open-to-the-world agent bot."
            )
        self.default_workspace = os.environ.get(
            "DEFAULT_WORKSPACE", str(Path.home())
        )
        self.model = os.environ.get("AGENT_MODEL") or None
        self.timeout = float(os.environ.get("AGENT_TIMEOUT_SEC", "600"))
        self.store = SessionStore()
        self._chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        # In-memory only: chat_id -> {"target": <chat_uuid>, "expires_at": <epoch>}.
        # Lost on daemon restart; that's intentional for a confirmation flow.
        self._pending_attach: dict[int, dict] = {}

    @staticmethod
    def _require_env(name: str) -> str:
        val = os.environ.get(name)
        if not val:
            raise SystemExit(f"Missing required env var: {name}")
        return val

    @staticmethod
    def _parse_ids(raw: str) -> set[int]:
        out: set[int] = set()
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.add(int(part))
            except ValueError:
                logger.warning(
                    "Ignoring non-numeric TG_ALLOWED_USER_IDS entry: %r", part
                )
        return out

    def _is_allowed(self, update: Update) -> bool:
        user = update.effective_user
        return bool(user and user.id in self.allowed_ids)

    async def _refuse(self, update: Update) -> None:
        if update.effective_chat:
            await update.effective_chat.send_message("Sorry, this bot is restricted.")
        u = update.effective_user
        logger.warning(
            "Refused message from unauthorized user %s (@%s)",
            getattr(u, "id", "?"),
            getattr(u, "username", "?"),
        )

    # ---------- command handlers ----------

    async def cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await self._refuse(update)
            return
        chat_id = update.effective_chat.id
        state = self.store.get(chat_id)
        ws = state.workspace or self.default_workspace
        await update.effective_chat.send_message(
            "Hi. I forward your messages to a Cursor agent.\n\n"
            f"Workspace: `{ws}`\n\n"
            "Commands:\n"
            "  /cd <path> — set workspace for this chat (resets session)\n"
            "  /pwd — show current workspace\n"
            "  /reset — clear session, start a new conversation\n"
            "  /list [N] — show recent Cursor chats from this workspace\n"
            "  /attach <id-prefix> — point this chat at an existing Cursor chat (requires YES confirmation)\n"
            "  /cancel — cancel a pending /attach\n"
            "  /status — show workspace and session id\n\n"
            "After every reply I'll include the Cursor session id so you can "
            "resume the conversation in a terminal with `agent --resume <id>`.",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_cd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await self._refuse(update)
            return
        chat_id = update.effective_chat.id
        if not ctx.args:
            await update.effective_chat.send_message("Usage: /cd <path>")
            return
        raw = " ".join(ctx.args).strip()
        new_ws = str(Path(raw).expanduser().resolve())
        if not Path(new_ws).is_dir():
            await update.effective_chat.send_message(f"Not a directory: {new_ws}")
            return
        self.store.update(chat_id, workspace=new_ws, session_id=None)
        await update.effective_chat.send_message(
            f"Workspace set to `{new_ws}` (session reset).",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_pwd(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await self._refuse(update)
            return
        chat_id = update.effective_chat.id
        state = self.store.get(chat_id)
        ws = state.workspace or self.default_workspace
        await update.effective_chat.send_message(
            f"Workspace: `{ws}`", parse_mode=ParseMode.MARKDOWN
        )

    async def cmd_reset(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await self._refuse(update)
            return
        chat_id = update.effective_chat.id
        self.store.reset(chat_id)
        await update.effective_chat.send_message("Session cleared.")

    async def cmd_list(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await self._refuse(update)
            return
        chat_id = update.effective_chat.id
        state = self.store.get(chat_id)
        workspace = state.workspace or self.default_workspace
        try:
            limit = int(ctx.args[0]) if ctx.args else 10
        except ValueError:
            limit = 10
        limit = max(1, min(50, limit))
        summaries = list_chats(workspace, limit=limit)
        if not summaries:
            await update.effective_chat.send_message(
                f"No transcripts found for workspace <code>{workspace}</code>. "
                "Have you used Cursor in this workspace yet?",
                parse_mode=ParseMode.HTML,
            )
            return
        lines = [f"<b>Recent chats in</b> <code>{workspace}</code>"]
        for s in summaries:
            ts = datetime.fromtimestamp(s.mtime, tz=timezone.utc).astimezone()
            stamp = ts.strftime("%b %d %H:%M")
            short_id = s.chat_id[:8]
            lines.append(
                f"<code>{short_id}</code>  {stamp}  <i>{_html_escape(s.title)}</i>"
            )
        lines.append("\nAttach with <code>/attach &lt;id-prefix&gt;</code>.")
        await update.effective_chat.send_message(
            "\n".join(lines), parse_mode=ParseMode.HTML
        )

    async def cmd_attach(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await self._refuse(update)
            return
        chat_id = update.effective_chat.id
        state = self.store.get(chat_id)
        workspace = state.workspace or self.default_workspace
        logger.info("cmd_attach chat=%s args=%r workspace=%s", chat_id, ctx.args, workspace)
        if not ctx.args:
            await update.effective_chat.send_message(
                "Usage: /attach <chat-id-or-prefix>\n"
                "Use /list to see candidates."
            )
            return
        prefix = ctx.args[0].strip()
        match, ambiguous = find_chat_by_prefix(workspace, prefix)
        logger.info(
            "cmd_attach prefix=%r match=%s ambiguous=%d",
            prefix, match.chat_id if match else None, len(ambiguous),
        )
        if match is None and ambiguous:
            preview = "\n".join(
                f"  {s.chat_id[:8]}  {_html_escape(s.title)}"
                for s in ambiguous[:5]
            )
            await update.effective_chat.send_message(
                f"Ambiguous prefix. {len(ambiguous)} chats start with "
                f"<code>{_html_escape(prefix)}</code>:\n<pre>{preview}</pre>"
                "\nUse a longer prefix.",
                parse_mode=ParseMode.HTML,
            )
            return
        if match is None:
            await update.effective_chat.send_message(
                f"No chat starts with <code>{_html_escape(prefix)}</code> in "
                f"<code>{workspace}</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        # Stage pending; confirmation by replying YES.
        self._pending_attach[chat_id] = {
            "target": match.chat_id,
            "transcript_path": str(match.transcript_path),
            "expires_at": time.time() + ATTACH_CONFIRM_TTL_SEC,
        }
        ts = datetime.fromtimestamp(match.mtime, tz=timezone.utc).astimezone()
        stamp = ts.strftime("%Y-%m-%d %H:%M")
        warning = (
            f"<b>Confirm attach</b>\n\n"
            f"Target: <code>{match.chat_id}</code>\n"
            f"Last activity: {stamp}\n"
            f"Lines in transcript: {match.line_count}\n"
            f"Title: <i>{_html_escape(match.title)}</i>\n\n"
            f"<b>Heads-up:</b> running the agent against this chat will "
            f"<b>append a turn</b> to it (visible in Cursor IDE), and the local "
            f"transcript file will be rewritten. The bridge will save a "
            f"<code>.bak.&lt;timestamp&gt;</code> copy beforehand as a safety net.\n\n"
            f"Reply <b>YES</b> within {int(ATTACH_CONFIRM_TTL_SEC)}s to confirm, "
            f"or anything else to cancel."
        )
        await update.effective_chat.send_message(warning, parse_mode=ParseMode.HTML)

    async def cmd_cancel(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await self._refuse(update)
            return
        chat_id = update.effective_chat.id
        if self._pending_attach.pop(chat_id, None):
            await update.effective_chat.send_message("Pending attach cancelled.")
        else:
            await update.effective_chat.send_message("Nothing to cancel.")

    async def cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await self._refuse(update)
            return
        chat_id = update.effective_chat.id
        state = self.store.get(chat_id)
        ws = state.workspace or self.default_workspace
        sid = state.session_id or "(none)"
        await update.effective_chat.send_message(
            f"Workspace: `{ws}`\nSession: `{sid}`",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ---------- main message path ----------

    async def on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await self._refuse(update)
            return
        chat = update.effective_chat
        msg = update.effective_message
        text = (msg.text or "").strip()
        if not text:
            return
        chat_id = chat.id
        logger.info(
            "on_message chat=%s text=%r",
            chat_id, text[:80] + ("..." if len(text) > 80 else ""),
        )

        # Fallback for slash-commands that arrived without a bot_command
        # MessageEntity (commonly happens when the user pastes the line, or
        # with some clients). python-telegram-bot's CommandHandler relies on
        # the entity, so it would silently skip these and we'd misroute the
        # text to the agent.
        if text.startswith("/"):
            first, _, rest = text[1:].partition(" ")
            cmd_name = first.split("@")[0].strip().lower()
            handler = self._fallback_handler_for(cmd_name)
            if handler is not None:
                logger.info(
                    "fallback-command dispatch: cmd=%s args=%r", cmd_name, rest
                )
                ctx.args = rest.split() if rest else []
                await handler(update, ctx)
                return

        # Pending /attach confirmation: short-circuit before forwarding to agent.
        pending = self._pending_attach.get(chat_id)
        if pending is not None:
            if time.time() > pending["expires_at"]:
                self._pending_attach.pop(chat_id, None)
                await chat.send_message(
                    "Confirmation timed out. Run /attach again if you want to retry."
                )
                return
            if text.strip().upper() == "YES":
                target = pending["target"]
                transcript_path = Path(pending["transcript_path"])
                self._pending_attach.pop(chat_id, None)
                backup_msg = self._backup_transcript(transcript_path)
                self.store.update(chat_id, session_id=target)
                await chat.send_message(
                    f"Attached to chat <code>{target}</code>. {backup_msg}\n"
                    "Send your next message to continue that conversation.",
                    parse_mode=ParseMode.HTML,
                )
                return
            self._pending_attach.pop(chat_id, None)
            await chat.send_message(
                "Attach cancelled (reply was not exactly 'YES'). "
                "Continuing with the previous session."
            )
            # Fall through and treat the message as a normal prompt.

        state = self.store.get(chat_id)
        workspace = state.workspace or self.default_workspace

        lock = self._chat_locks[chat_id]
        if lock.locked():
            await chat.send_message(
                "Still working on the previous message in this chat — queued."
            )

        async with lock:
            placeholder = await chat.send_message("Thinking...")
            typing_task = asyncio.create_task(self._keep_typing(chat_id, ctx))
            try:
                result = await run_agent(
                    text,
                    workspace=workspace,
                    session_id=state.session_id,
                    model=self.model,
                    timeout_sec=self.timeout,
                )
            finally:
                typing_task.cancel()

            if result.session_id and result.session_id != state.session_id:
                self.store.update(chat_id, session_id=result.session_id)

            await self._send_formatted_response(
                chat_id, placeholder.message_id, result.text, ctx
            )
            await self._send_session_footer(chat_id, result.session_id, ctx)
            if not result.ok:
                logger.error(
                    "Agent failed for chat=%s exit=%s stderr=%s",
                    chat_id, result.exit_code, result.raw_stderr[:500],
                )

    @staticmethod
    async def _keep_typing(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            while True:
                await ctx.bot.send_chat_action(
                    chat_id=chat_id, action=ChatAction.TYPING
                )
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            return

    async def _send_formatted_response(
        self,
        chat_id: int,
        placeholder_msg_id: int,
        text: str,
        ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Render the agent's markdown via telegramify and send.

        On any error we fall back to plain-text via _send_long_message so we
        never lose the response.
        """
        if not text:
            text = "(empty response)"

        try:
            items = await telegramify(text, max_message_length=4090)
        except Exception as e:
            logger.warning("telegramify() raised %r; falling back to plain text", e)
            await self._send_long_message(chat_id, placeholder_msg_id, text, ctx)
            return

        if not items:
            await self._send_long_message(chat_id, placeholder_msg_id, text, ctx)
            return

        placeholder_used = False
        for item in items:
            try:
                if item.content_type == ContentType.TEXT:
                    entities = _to_tg_entities(item.entities) or None
                    if not placeholder_used:
                        await ctx.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=placeholder_msg_id,
                            text=item.text,
                            entities=entities,
                        )
                        placeholder_used = True
                    else:
                        await ctx.bot.send_message(
                            chat_id=chat_id,
                            text=item.text,
                            entities=entities,
                        )
                elif item.content_type == ContentType.FILE:
                    if not placeholder_used:
                        try:
                            await ctx.bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=placeholder_msg_id,
                                text=f"(attached: {item.file_name})",
                            )
                            placeholder_used = True
                        except Exception:
                            pass
                    cap_entities = _to_tg_entities(item.caption_entities) or None
                    await ctx.bot.send_document(
                        chat_id=chat_id,
                        document=item.file_data,
                        filename=item.file_name,
                        caption=item.caption_text or None,
                        caption_entities=cap_entities,
                    )
                elif item.content_type == ContentType.PHOTO:
                    if not placeholder_used:
                        try:
                            await ctx.bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=placeholder_msg_id,
                                text="(attached image below)",
                            )
                            placeholder_used = True
                        except Exception:
                            pass
                    cap_entities = _to_tg_entities(item.caption_entities) or None
                    await ctx.bot.send_photo(
                        chat_id=chat_id,
                        photo=item.file_data,
                        caption=item.caption_text or None,
                        caption_entities=cap_entities,
                    )
            except Exception as e:
                logger.warning(
                    "send item failed (%s); falling back to plain text for the rest",
                    e,
                )
                await self._send_long_message(chat_id, placeholder_msg_id, text, ctx)
                return

        if not placeholder_used:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=placeholder_msg_id,
                    text="(response sent as attachments above)",
                )
            except Exception:
                pass

    @staticmethod
    async def _send_long_message(
        chat_id: int,
        placeholder_msg_id: int,
        text: str,
        ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not text:
            text = "(empty response)"
        first = text[:MAX_TG_MSG_LEN]
        try:
            await ctx.bot.edit_message_text(
                chat_id=chat_id,
                message_id=placeholder_msg_id,
                text=first,
            )
        except Exception as e:
            logger.warning("edit_message_text failed (%s); sending fresh", e)
            await ctx.bot.send_message(chat_id=chat_id, text=first)

        rest = text[MAX_TG_MSG_LEN:]
        while rest:
            chunk = rest[:MAX_TG_MSG_LEN]
            rest = rest[MAX_TG_MSG_LEN:]
            await ctx.bot.send_message(chat_id=chat_id, text=chunk)

    @staticmethod
    def _backup_transcript(transcript_path: Path) -> str:
        """Copy <id>.jsonl to <id>.jsonl.bak.<epoch> for safety. Returns user-facing msg."""
        if not transcript_path.is_file():
            return "(no existing local transcript to back up)"
        try:
            ts = int(time.time())
            backup = transcript_path.with_suffix(
                transcript_path.suffix + f".bak.{ts}"
            )
            shutil.copy2(transcript_path, backup)
            return f"Backup saved at <code>{backup.name}</code>."
        except OSError as e:
            logger.warning("transcript backup failed: %s", e)
            return f"(backup failed: {e})"

    @staticmethod
    async def _send_session_footer(
        chat_id: int,
        session_id: Optional[str],
        ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not session_id:
            return
        try:
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=f"<i>cursor session:</i> <code>{session_id}</code>",
                parse_mode=ParseMode.HTML,
                disable_notification=True,
            )
        except Exception as e:
            logger.warning("session footer send failed: %s", e)

    def _fallback_handler_for(self, cmd_name: str):
        """Map a (lowercased) command name to its handler, or None."""
        handler_map = {
            "start": self.cmd_start,
            "help": self.cmd_start,
            "cd": self.cmd_cd,
            "pwd": self.cmd_pwd,
            "reset": self.cmd_reset,
            "list": self.cmd_list,
            "attach": self.cmd_attach,
            "cancel": self.cmd_cancel,
            "status": self.cmd_status,
        }
        return handler_map.get(cmd_name)

    @staticmethod
    async def _post_init(app: Application) -> None:
        """Register commands so Telegram shows them in the chat-input autocomplete."""
        commands = [
            BotCommand("start", "Welcome and command list"),
            BotCommand("cd", "Set workspace directory for this chat"),
            BotCommand("pwd", "Show current workspace"),
            BotCommand("reset", "Clear conversation, start fresh"),
            BotCommand("list", "Show recent Cursor chats in this workspace"),
            BotCommand("attach", "Attach to an existing Cursor chat"),
            BotCommand("cancel", "Cancel a pending /attach"),
            BotCommand("status", "Show workspace and session id"),
        ]
        try:
            await app.bot.set_my_commands(commands)
            logger.info("registered %d bot commands with Telegram", len(commands))
        except Exception as e:
            logger.warning("set_my_commands failed: %s", e)

    def build(self) -> Application:
        app = (
            ApplicationBuilder()
            .token(self.bot_token)
            .post_init(self._post_init)
            .build()
        )
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_start))
        app.add_handler(CommandHandler("cd", self.cmd_cd))
        app.add_handler(CommandHandler("pwd", self.cmd_pwd))
        app.add_handler(CommandHandler("reset", self.cmd_reset))
        app.add_handler(CommandHandler("list", self.cmd_list))
        app.add_handler(CommandHandler("attach", self.cmd_attach))
        app.add_handler(CommandHandler("cancel", self.cmd_cancel))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_message)
        )
        return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bridge = Bridge()
    app = bridge.build()
    logger.info(
        "Starting Telegram <-> Cursor bridge for %d allowed user(s)",
        len(bridge.allowed_ids),
    )
    app.run_polling(
        allowed_updates=Update.ALL_TYPES, drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
