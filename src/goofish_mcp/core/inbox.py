"""本地收件箱。SQLite 按 message_id 去重，默认放在 ~/.goofish-cli。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from goofish_mcp.core.errors import GoofishError


def state_dir() -> Path:
    raw = os.environ.get("GOOFISH_STATE_DIR") or str(Path.home() / ".goofish-cli")
    return Path(raw).expanduser()


def resolve_inbox_path(path: str | Path | None = None) -> Path:
    """显式路径 > GOOFISH_INBOX_PATH > $GOOFISH_STATE_DIR/inbox.sqlite。"""
    if path:
        return Path(path).expanduser()
    env = os.environ.get("GOOFISH_INBOX_PATH")
    if env:
        return Path(env).expanduser()
    return state_dir() / "inbox.sqlite"


def resolve_audit_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path).expanduser()
    env = os.environ.get("GOOFISH_AUDIT_LOG")
    if env:
        return Path(env).expanduser()
    return state_dir() / "reply-audit.jsonl"


def _norm(value: Any) -> str:
    return str(value or "").removesuffix("@goofish").strip()


def _created(value: Any) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def message_text(payload: Any) -> tuple[str, int | None]:
    """从 history 的 message 对象取出正文和 contentType。"""
    if not isinstance(payload, dict):
        return str(payload or ""), None
    content_type = _optional_int(payload.get("contentType"))
    text = payload.get("text") or {}
    if isinstance(text, dict) and text.get("text"):
        return str(text["text"]), content_type
    summary = payload.get("summary")
    if summary:
        return str(summary), content_type
    return "", content_type


def event_from_history(
    item: dict[str, Any],
    *,
    my_user_id: str,
    session_type: int | None = None,
    item_id: str = "",
    peer_user_id: str = "",
    peer_nick: str = "",
) -> dict[str, Any]:
    """把 message history 的一条记录收成收件箱事件。"""
    text, content_type = message_text(item.get("message"))
    sender = _norm(item.get("send_user_id"))
    mine = _norm(my_user_id)
    peer = _norm(peer_user_id)
    if peer and mine and peer == mine:
        peer = ""
    event: dict[str, Any] = {
        "event": "message",
        "cid": _norm(item.get("cid")),
        "message_id": str(item.get("message_id") or ""),
        "send_user_id": sender,
        "send_user_name": str(item.get("send_user_name") or ""),
        "send_message": text,
        "item_id": str(item.get("item_id") or item_id or ""),
        "peer_user_id": peer,
        "peer_nick": str(peer_nick or ""),
    }
    if content_type is not None:
        event["content_type"] = content_type
    created = _created(item.get("created_at"))
    if created is not None:
        event["created_at"] = created
    kind = _optional_int(item.get("session_type"))
    if kind is None:
        kind = _optional_int(session_type)
    if kind is not None:
        event["session_type"] = kind
    if sender and mine:
        event["from_self"] = sender == mine
    return event


class Inbox:
    """按会话保存消息。有 message_id 的重复投递会被丢掉。"""

    def __init__(self, path: str | Path | None = None):
        self.path = resolve_inbox_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init()
        os.chmod(self.path, 0o600)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Inbox:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _init(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                row_id INTEGER PRIMARY KEY,
                message_id TEXT,
                cid TEXT NOT NULL,
                event TEXT NOT NULL DEFAULT 'message',
                send_user_id TEXT NOT NULL DEFAULT '',
                send_user_name TEXT NOT NULL DEFAULT '',
                send_message TEXT NOT NULL DEFAULT '',
                content_type INTEGER,
                item_id TEXT NOT NULL DEFAULT '',
                session_type INTEGER,
                created_at INTEGER,
                from_self INTEGER,
                source TEXT NOT NULL,
                handled INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL,
                inserted_at INTEGER NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS messages_message_id
                ON messages(message_id) WHERE message_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS messages_cid ON messages(cid, handled);

            CREATE TABLE IF NOT EXISTS conversations (
                cid TEXT PRIMARY KEY,
                session_type INTEGER,
                item_id TEXT NOT NULL DEFAULT '',
                peer_user_id TEXT NOT NULL DEFAULT '',
                peer_nick TEXT NOT NULL DEFAULT '',
                handled INTEGER NOT NULL DEFAULT 0,
                handled_at INTEGER
            );
            """
        )
        self._conn.commit()

    def append_event(self, event: dict[str, Any], *, source: str) -> bool:
        """写入一条消息。重复的 message_id 返回 False，且不改变已处理状态。"""
        if event.get("event") not in (None, "message"):
            return False
        cid = _norm(event.get("cid"))
        if not cid:
            return False
        message_id = str(event.get("message_id") or "").strip() or None
        created_at = _created(event.get("created_at"))
        session_type = _optional_int(event.get("session_type"))
        from_self = event.get("from_self")
        flag = None if from_self is None else (1 if from_self else 0)
        item_id = str(event.get("item_id") or "")
        peer_user_id = _norm(event.get("peer_user_id"))
        peer_nick = str(event.get("peer_nick") or "")
        content_type = _optional_int(event.get("content_type"))
        now = int(time.time() * 1000)
        raw = dict(event)
        raw["source"] = source

        with self._conn:
            conv = self._conn.execute(
                "SELECT handled, handled_at FROM conversations WHERE cid = ?",
                (cid,),
            ).fetchone()
            reopen = _buyer_reopens(conv, source=source, created_at=created_at, from_self=flag == 1)
            if flag == 1:
                message_handled = 0 if conv is not None and not conv["handled"] else 1
                conv_handled = 1 if conv is None else int(conv["handled"])
                conv_handled_at = now if conv is None else conv["handled_at"]
            elif reopen:
                message_handled = 0
                conv_handled = 0
                conv_handled_at = None
            else:
                message_handled = 1
                conv_handled = 1
                conv_handled_at = conv["handled_at"] if conv is not None else now

            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO messages (
                    message_id, cid, event, send_user_id, send_user_name, send_message,
                    content_type, item_id, session_type, created_at, from_self, source,
                    handled, raw_json, inserted_at
                ) VALUES (?, ?, 'message', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    cid,
                    _norm(event.get("send_user_id")),
                    str(event.get("send_user_name") or ""),
                    str(event.get("send_message") or ""),
                    content_type,
                    item_id,
                    session_type,
                    created_at,
                    flag,
                    source,
                    message_handled,
                    json.dumps(raw, ensure_ascii=False, default=str),
                    now,
                ),
            )
            if cursor.rowcount == 0:
                return False
            if conv is None:
                self._conn.execute(
                    """
                    INSERT INTO conversations (
                        cid, session_type, item_id, peer_user_id, peer_nick, handled, handled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (cid, session_type, item_id, peer_user_id, peer_nick, conv_handled, conv_handled_at),
                )
            elif reopen:
                self._conn.execute(
                    """
                    UPDATE conversations SET
                        handled = 0,
                        handled_at = NULL,
                        session_type = COALESCE(?, session_type),
                        item_id = CASE WHEN ? != '' THEN ? ELSE item_id END,
                        peer_user_id = CASE WHEN ? != '' THEN ? ELSE peer_user_id END,
                        peer_nick = CASE WHEN ? != '' THEN ? ELSE peer_nick END
                    WHERE cid = ?
                    """,
                    (
                        session_type,
                        item_id,
                        item_id,
                        peer_user_id,
                        peer_user_id,
                        peer_nick,
                        peer_nick,
                        cid,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE conversations SET
                        session_type = COALESCE(?, session_type),
                        item_id = CASE WHEN ? != '' THEN ? ELSE item_id END,
                        peer_user_id = CASE WHEN ? != '' THEN ? ELSE peer_user_id END,
                        peer_nick = CASE WHEN ? != '' THEN ? ELSE peer_nick END
                    WHERE cid = ?
                    """,
                    (
                        session_type,
                        item_id,
                        item_id,
                        peer_user_id,
                        peer_user_id,
                        peer_nick,
                        peer_nick,
                        cid,
                    ),
                )
        return True

    def pending(self) -> dict[str, Any]:
        """未处理会话。只包含至少一条买家消息的会话，消息按时间归并。"""
        conversations = self._conn.execute(
            "SELECT * FROM conversations WHERE handled = 0 ORDER BY cid"
        ).fetchall()
        groups: list[dict[str, Any]] = []
        total_messages = 0
        for conv in conversations:
            rows = self._conn.execute(
                """
                SELECT * FROM messages
                WHERE cid = ? AND handled = 0
                ORDER BY COALESCE(created_at, inserted_at), row_id
                """,
                (conv["cid"],),
            ).fetchall()
            messages = [_message_row(row) for row in rows]
            buyer = [item for item in messages if not item.get("from_self")]
            if not buyer:
                continue
            groups.append(
                {
                    "cid": conv["cid"],
                    "session_type": conv["session_type"],
                    "item_id": conv["item_id"],
                    "peer_user_id": conv["peer_user_id"],
                    "peer_nick": conv["peer_nick"],
                    "messages": messages,
                    "merged_text": "\n".join(
                        item["send_message"] for item in buyer if item["send_message"]
                    ),
                }
            )
            total_messages += len(buyer)
        return {
            "conversations": groups,
            "total_conversations": len(groups),
            "total_messages": total_messages,
        }

    def mark_handled(self, cid: str, *, now_ms: int | None = None) -> dict[str, Any]:
        cid = _norm(cid)
        if not cid:
            raise GoofishError("cid 不能为空")
        stamp = int(time.time() * 1000) if now_ms is None else now_ms
        with self._conn:
            existing = self._conn.execute(
                "SELECT cid FROM conversations WHERE cid = ?",
                (cid,),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO conversations (
                        cid, item_id, peer_user_id, peer_nick, handled, handled_at
                    ) VALUES (?, '', '', '', 1, ?)
                    """,
                    (cid, stamp),
                )
                marked = 0
                absent = True
            else:
                self._conn.execute(
                    "UPDATE conversations SET handled = 1, handled_at = ? WHERE cid = ?",
                    (stamp, cid),
                )
                cursor = self._conn.execute(
                    "UPDATE messages SET handled = 1 WHERE cid = ? AND handled = 0",
                    (cid,),
                )
                marked = cursor.rowcount
                absent = False
        return {"cid": cid, "handled": True, "marked_messages": marked, "absent": absent}


def _buyer_reopens(
    conv: sqlite3.Row | None,
    *,
    source: str,
    created_at: int | None,
    from_self: bool,
) -> bool:
    """买家新消息重新打开会话。自己的消息、以及已处理时点之前的历史补洞，不打开。"""
    if from_self:
        return False
    if conv is None or not conv["handled"]:
        return True
    handled_at = conv["handled_at"]
    if source == "watch" and created_at is None:
        return True
    return created_at is not None and (handled_at is None or created_at > handled_at)


def _message_row(row: sqlite3.Row) -> dict[str, Any]:
    from_self = row["from_self"]
    return {
        "message_id": row["message_id"] or "",
        "cid": row["cid"],
        "send_user_id": row["send_user_id"],
        "send_user_name": row["send_user_name"],
        "send_message": row["send_message"],
        "content_type": row["content_type"],
        "item_id": row["item_id"],
        "session_type": row["session_type"],
        "created_at": row["created_at"],
        "from_self": None if from_self is None else bool(from_self),
        "source": row["source"],
    }
