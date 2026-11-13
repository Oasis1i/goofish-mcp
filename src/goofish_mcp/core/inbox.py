"""本地收件箱。SQLite 按 message_id 去重，默认放在 ~/.goofish-cli。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from goofish_mcp.core.errors import GoofishError

DEFAULT_DAILY_CAP = 15
DEFAULT_COOLDOWN_SECONDS = 20 * 60


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

            CREATE TABLE IF NOT EXISTS agent_escalations (
                id INTEGER PRIMARY KEY,
                cid TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at REAL NOT NULL,
                day TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_handoffs (
                cid TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reply_policy (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                daily_cap INTEGER NOT NULL,
                cooldown_seconds INTEGER NOT NULL,
                day TEXT NOT NULL,
                sent_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS buyer_cooldown (
                buyer_id TEXT PRIMARY KEY,
                last_sent_at REAL NOT NULL
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

    def conversation(self, cid: str) -> dict[str, Any] | None:
        cid = _norm(cid)
        if not cid:
            return None
        conv = self._conn.execute(
            "SELECT * FROM conversations WHERE cid = ?",
            (cid,),
        ).fetchone()
        if conv is None:
            return None
        rows = self._conn.execute(
            """
            SELECT * FROM messages
            WHERE cid = ? AND handled = 0
            ORDER BY COALESCE(created_at, inserted_at), row_id
            """,
            (cid,),
        ).fetchall()
        messages = [_message_row(row) for row in rows]
        buyer = [item for item in messages if not item.get("from_self")]
        return {
            "cid": cid,
            "session_type": conv["session_type"],
            "handled": bool(conv["handled"]),
            "item_id": conv["item_id"],
            "peer_user_id": conv["peer_user_id"],
            "peer_nick": conv["peer_nick"],
            "messages": messages,
            "merged_text": "\n".join(item["send_message"] for item in buyer if item["send_message"]),
        }

    def is_escalated(self, cid: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM agent_handoffs WHERE cid = ?", (_norm(cid),)
        ).fetchone() is not None

    def escalate(self, cid: str, reason: str) -> dict[str, Any]:
        cid = _norm(cid)
        reason = reason.strip()
        if not cid or not reason:
            raise GoofishError("cid 和 reason 不能为空")
        now = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT INTO agent_escalations (cid, reason, created_at, day) VALUES (?, ?, ?, ?)",
                (cid, reason, now, time.strftime("%Y-%m-%d", time.localtime(now))),
            )
            self._conn.execute(
                "INSERT INTO agent_handoffs (cid, reason, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(cid) DO UPDATE SET reason=excluded.reason, "
                "created_at=excluded.created_at", (cid, reason, now),
            )
        return {"ok": True, "cid": cid, "escalated": True, "reason": reason, "ts": now}

    def recent_messages(self, cid: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE cid = ? "
            "ORDER BY COALESCE(created_at, inserted_at) DESC, row_id DESC LIMIT ?",
            (_norm(cid), limit),
        ).fetchall()
        return [_message_row(row) for row in reversed(rows)]

    def local_item(self, cid: str, item_id: str) -> dict[str, Any]:
        item = {"item_id": item_id}
        rows = self._conn.execute(
            "SELECT raw_json FROM messages WHERE cid = ? ORDER BY row_id", (_norm(cid),)
        ).fetchall()
        for row in rows:
            event = json.loads(row["raw_json"])
            if str(event.get("item_id") or "") != item_id:
                continue
            for key in ("title", "price", "item_title", "item_price"):
                if event.get(key) is not None:
                    item[key] = event[key]
        return item

    def escalation_count(self, day: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM agent_escalations WHERE day = ?", (day,)
        ).fetchone()[0]

    def limits(self, *, today: str | None = None) -> dict[str, int]:
        today = today or time.strftime("%Y-%m-%d")
        row = self._ensure_policy(today)
        return {
            "daily_cap": int(row["daily_cap"]),
            "cooldown_seconds": int(row["cooldown_seconds"]),
        }

    def set_limits(
        self,
        *,
        daily_cap: int | None = None,
        cooldown_seconds: int | None = None,
        today: str | None = None,
    ) -> dict[str, int]:
        today = today or time.strftime("%Y-%m-%d")
        self._ensure_policy(today)
        if daily_cap is not None:
            if daily_cap < 0 or daily_cap > 100:
                raise GoofishError("daily-cap 必须为 0–100")
            self._conn.execute(
                "UPDATE reply_policy SET daily_cap = ? WHERE id = 1",
                (daily_cap,),
            )
        if cooldown_seconds is not None:
            if cooldown_seconds < 0 or cooldown_seconds > 24 * 3600:
                raise GoofishError("cooldown-seconds 必须为 0–86400")
            self._conn.execute(
                "UPDATE reply_policy SET cooldown_seconds = ? WHERE id = 1",
                (cooldown_seconds,),
            )
        self._conn.commit()
        return self.limits(today=today)

    def quota_view(self, buyer_id: str, *, now: float, today: str) -> dict[str, Any]:
        row = self._ensure_policy(today)
        sent = int(row["sent_count"]) if row["day"] == today else 0
        cooldown = int(row["cooldown_seconds"])
        last = self._last_sent(buyer_id)
        remaining = 0.0
        if last is not None and cooldown > 0 and now - last < cooldown:
            remaining = cooldown - (now - last)
        return {
            "daily_sent": sent,
            "daily_cap": int(row["daily_cap"]),
            "cooldown_seconds": cooldown,
            "cooldown_remaining": remaining,
            "last_sent_at": last,
        }

    def reserve_send(self, buyer_id: str, *, now: float, today: str) -> dict[str, Any]:
        """占一次每日额度和买家冷却。失败不改计数。调用方发送失败时要 release_send。"""
        buyer_id = _norm(buyer_id)
        with self._conn:
            row = self._conn.execute("SELECT * FROM reply_policy WHERE id = 1").fetchone()
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO reply_policy (id, daily_cap, cooldown_seconds, day, sent_count)
                    VALUES (1, ?, ?, ?, 0)
                    """,
                    (DEFAULT_DAILY_CAP, DEFAULT_COOLDOWN_SECONDS, today),
                )
                row = self._conn.execute("SELECT * FROM reply_policy WHERE id = 1").fetchone()
            sent = int(row["sent_count"]) if row["day"] == today else 0
            cap = int(row["daily_cap"])
            cooldown = int(row["cooldown_seconds"])
            last = self._last_sent(buyer_id)
            if last is not None and cooldown > 0 and now - last < cooldown:
                return {
                    "ok": False,
                    "reason": "cooldown",
                    "daily_sent": sent,
                    "daily_cap": cap,
                    "cooldown_remaining": cooldown - (now - last),
                }
            if sent >= cap:
                return {
                    "ok": False,
                    "reason": "daily_cap",
                    "daily_sent": sent,
                    "daily_cap": cap,
                    "cooldown_remaining": 0.0,
                }
            self._conn.execute(
                "UPDATE reply_policy SET day = ?, sent_count = ? WHERE id = 1",
                (today, sent + 1),
            )
            self._conn.execute(
                """
                INSERT INTO buyer_cooldown (buyer_id, last_sent_at) VALUES (?, ?)
                ON CONFLICT(buyer_id) DO UPDATE SET last_sent_at = excluded.last_sent_at
                """,
                (buyer_id, now),
            )
        return {
            "ok": True,
            "reason": "reserved",
            "daily_sent": sent + 1,
            "daily_cap": cap,
            "previous_last": last,
            "today": today,
            "buyer_id": buyer_id,
        }

    def release_send(self, reservation: dict[str, Any]) -> None:
        if not reservation.get("ok"):
            return
        buyer_id = reservation["buyer_id"]
        today = reservation["today"]
        previous = reservation.get("previous_last")
        with self._conn:
            row = self._conn.execute(
                "SELECT day, sent_count FROM reply_policy WHERE id = 1"
            ).fetchone()
            if row is not None and row["day"] == today:
                self._conn.execute(
                    "UPDATE reply_policy SET sent_count = ? WHERE id = 1",
                    (max(0, int(row["sent_count"]) - 1),),
                )
            if previous is None:
                self._conn.execute("DELETE FROM buyer_cooldown WHERE buyer_id = ?", (buyer_id,))
            else:
                self._conn.execute(
                    "UPDATE buyer_cooldown SET last_sent_at = ? WHERE buyer_id = ?",
                    (previous, buyer_id),
                )

    def _ensure_policy(self, today: str) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM reply_policy WHERE id = 1").fetchone()
        if row is None:
            self._conn.execute(
                """
                INSERT INTO reply_policy (id, daily_cap, cooldown_seconds, day, sent_count)
                VALUES (1, ?, ?, ?, 0)
                """,
                (DEFAULT_DAILY_CAP, DEFAULT_COOLDOWN_SECONDS, today),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM reply_policy WHERE id = 1").fetchone()
        return row

    def _last_sent(self, buyer_id: str) -> float | None:
        row = self._conn.execute(
            "SELECT last_sent_at FROM buyer_cooldown WHERE buyer_id = ?",
            (_norm(buyer_id),),
        ).fetchone()
        if row is None:
            return None
        return float(row["last_sent_at"])


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
