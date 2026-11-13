"""Task-level agent API. Context and policy come from server-side local state."""

from __future__ import annotations

import time
from functools import wraps
from typing import Any

from filelock import FileLock

from goofish_mcp.commands.message.reply import send_guarded
from goofish_mcp.core import Session
from goofish_mcp.core.audit_log import append_audit
from goofish_mcp.core.errors import GoofishError
from goofish_mcp.core.inbox import Inbox, resolve_inbox_path


def _serialized(func):
    @wraps(func)
    def call(self, *args, **kwargs):
        # Keep handoff checks and sends ordered, including across HTTP workers.
        with self._lock:
            return func(self, *args, **kwargs)
    return call


class AgentOps:
    """One account's operations; paths are configured by the server, never by tools."""

    def __init__(self, inbox: str = "", audit_log: str = ""):
        self.inbox = inbox
        self.audit_log = audit_log
        path = resolve_inbox_path(inbox)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(path) + ".agent.lock", timeout=300, mode=0o600)

    def inbox_pending(self, history_limit: int = 10) -> dict[str, Any]:
        if not 0 <= history_limit <= 100:
            raise GoofishError("history_limit 必须为 0–100")
        with Inbox(self.inbox) as store:
            result = store.pending()
            groups = [c for c in result["conversations"] if not store.is_escalated(c["cid"])]
            for conv in groups:
                conv["item"] = store.local_item(conv["cid"], conv["item_id"])
                conv["recent_history"] = store.recent_messages(conv["cid"], history_limit)
            return {
                "conversations": groups,
                "total_conversations": len(groups),
                "total_messages": sum(
                    not m.get("from_self") for c in groups for m in c["messages"]
                ),
            }

    @_serialized
    def reply(self, cid: str, text: str, dry_run: bool = False) -> dict[str, Any]:
        with Inbox(self.inbox) as store:
            conv = store.conversation(cid)
            quota = store.quota_view("", now=time.time(), today=time.strftime("%Y-%m-%d"))
            reason = ""
            if store.is_escalated(cid):
                reason = "escalated"
            elif conv is None or conv["handled"]:
                reason = "no_pending_conversation"
            buyers = [] if conv is None else [
                m for m in conv["messages"] if not m.get("from_self")
            ]
            if not reason and not buyers:
                reason = "own_message"
            peer = "" if conv is None else conv["peer_user_id"]
            if not peer and buyers:
                peer = buyers[-1]["send_user_id"]
            if not reason and not peer:
                reason = "missing_recipient"
            if reason:
                return {"ok": False, "cid": cid, "action": "skip", "reason": reason,
                        "sent": False, "dry_run": dry_run, **quota}
        session = Session.load()
        result = send_guarded(
            my_user_id=session.unb, cid=cid, toid=peer, text=text,
            inbox=self.inbox, audit_log=self.audit_log, dry_run=dry_run,
        )
        if result["action"] == "needs_human" and not dry_run:
            self.escalate(cid, result["reason"])
        return result

    @_serialized
    def escalate(self, cid: str, reason: str) -> dict[str, Any]:
        with Inbox(self.inbox) as store:
            result = store.escalate(cid, reason)
        append_audit({**result, "action": "escalate"}, self.audit_log or None)
        return result

    @_serialized
    def mark_handled(self, cid: str) -> dict[str, Any]:
        with Inbox(self.inbox) as store:
            return {"ok": True, **store.mark_handled(cid)}

    def daily_stats(self) -> dict[str, Any]:
        day = time.strftime("%Y-%m-%d")
        with Inbox(self.inbox) as store:
            quota = store.quota_view("", now=time.time(), today=day)
            escalations = store.escalation_count(day)
        return {"day": day, "daily_sent": quota["daily_sent"],
                "daily_cap": quota["daily_cap"], "cooldown_seconds": quota["cooldown_seconds"],
                "pending_conversations": self.inbox_pending(0)["total_conversations"],
                "escalations": escalations}
