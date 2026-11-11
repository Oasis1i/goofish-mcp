"""message reply — 带护栏的文本回复。

跳过自己的消息和非真人会话，限制同一买家的冷却和每日封顶，扫描外联词，
并把售后、退款、议价、投诉留给人工。`--dry-run` 只记决定，不发送。
真正发送仍走 message send 的限流和熔断，这里不放宽。
"""

from __future__ import annotations

import os
import time
from typing import Any

from goofish_mcp.commands.message.send import send as send_message
from goofish_mcp.core import Session, Strategy, command
from goofish_mcp.core.audit_log import append_audit, redact
from goofish_mcp.core.errors import GoofishError
from goofish_mcp.core.inbox import Inbox
from goofish_mcp.core.reply_guard import decide


def _norm(value: Any) -> str:
    return str(value or "").removesuffix("@goofish").strip()


def _configured_int(explicit: int, env_name: str, *, label: str) -> int | None:
    if explicit >= 0:
        return explicit
    raw = os.environ.get(env_name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise GoofishError(f"{label} 不是整数") from exc


@command(
    namespace="message",
    name="reply",
    description="带护栏地回复一条消息：冷却、每日上限、外联词，售后/退款/议价/投诉转人工",
    strategy=Strategy.COOKIE,
    columns=["action", "reason", "ok", "sent", "daily_sent", "daily_cap"],
    write=True,
)
def reply(
    cid: str,
    toid: str,
    text: str,
    *,
    buyer_text: str = "",
    session_type: int = -1,
    send_user_id: str = "",
    from_self: bool = False,
    dry_run: bool = False,
    daily_cap: int = -1,
    cooldown_seconds: int = -1,
    inbox: str = "",
    audit_log: str = "",
) -> dict[str, Any]:
    session = Session.load()
    return send_guarded(
        my_user_id=session.unb,
        cid=cid,
        toid=toid,
        text=text,
        buyer_text=buyer_text,
        session_type=session_type,
        send_user_id=send_user_id,
        from_self=from_self,
        dry_run=dry_run,
        daily_cap=daily_cap,
        cooldown_seconds=cooldown_seconds,
        inbox=inbox,
        audit_log=audit_log,
    )


def send_guarded(
    *,
    my_user_id: str,
    cid: str,
    toid: str,
    text: str,
    buyer_text: str = "",
    session_type: int = -1,
    send_user_id: str = "",
    from_self: bool = False,
    dry_run: bool = False,
    daily_cap: int = -1,
    cooldown_seconds: int = -1,
    inbox: str = "",
    audit_log: str = "",
    now: float | None = None,
    today: str | None = None,
    sender: Any = None,
) -> dict[str, Any]:
    if not str(text or "").strip():
        raise GoofishError("回复正文不能为空")
    cid = _norm(cid)
    toid = _norm(toid)
    if not cid or not toid:
        raise GoofishError("cid 和 toid 不能为空")

    stamp = time.time() if now is None else now
    day = today or time.strftime("%Y-%m-%d", time.localtime(stamp))
    store = Inbox(inbox)
    try:
        cap_override = _configured_int(daily_cap, "GOOFISH_REPLY_DAILY_CAP", label="daily-cap")
        cooldown_override = _configured_int(
            cooldown_seconds, "GOOFISH_REPLY_COOLDOWN_SECONDS", label="cooldown-seconds"
        )
        if cap_override is not None or cooldown_override is not None:
            store.set_limits(
                daily_cap=cap_override,
                cooldown_seconds=cooldown_override,
                today=day,
            )
        resolved_type = session_type
        resolved_buyer = buyer_text
        resolved_sender = send_user_id
        if resolved_type < 0 or not resolved_buyer:
            convo = store.conversation(cid)
            if convo is not None:
                if resolved_type < 0 and convo.get("session_type") is not None:
                    resolved_type = int(convo["session_type"])
                if not resolved_buyer:
                    resolved_buyer = str(convo.get("merged_text") or "")
                if not resolved_sender:
                    buyers = [item for item in convo["messages"] if not item.get("from_self")]
                    if buyers:
                        resolved_sender = str(buyers[-1].get("send_user_id") or "")
        quota = store.quota_view(toid, now=stamp, today=day)
        decision = decide(
            text=text,
            buyer_text=resolved_buyer,
            session_type=None if resolved_type < 0 else resolved_type,
            from_self=from_self,
            send_user_id=resolved_sender,
            my_user_id=my_user_id,
            toid=toid,
            dry_run=dry_run,
            daily_sent=int(quota["daily_sent"]),
            daily_cap=int(quota["daily_cap"]),
            cooldown_remaining=float(quota["cooldown_remaining"]),
        )
        result = {
            **decision,
            "cid": cid,
            "toid": toid,
            "session_type": None if resolved_type < 0 else resolved_type,
        }
        if decision["action"] != "send":
            _audit(result, text=text, buyer_text=resolved_buyer, audit_log=audit_log)
            return result

        reservation = store.reserve_send(toid, now=stamp, today=day)
        if not reservation["ok"]:
            result = {
                **result,
                "action": "skip",
                "reason": reservation["reason"],
                "ok": False,
                "sent": False,
                "daily_sent": reservation["daily_sent"],
                "daily_cap": reservation["daily_cap"],
                "cooldown_remaining": reservation.get("cooldown_remaining", 0),
            }
            _audit(result, text=text, buyer_text=resolved_buyer, audit_log=audit_log)
            return result
        result["daily_sent"] = reservation["daily_sent"]
        send = sender or send_message
        try:
            sent = send(cid, toid, text)
        except Exception as exc:
            store.release_send(reservation)
            result = {
                **result,
                "action": "error",
                "reason": "send_failed",
                "ok": False,
                "sent": False,
                "daily_sent": max(0, int(reservation["daily_sent"]) - 1),
                "error": str(exc),
            }
            _audit(result, text=text, buyer_text=resolved_buyer, audit_log=audit_log)
            raise
        result = {
            **result,
            "action": "sent",
            "reason": "sent",
            "ok": True,
            "sent": True,
            "mid": sent.get("mid", ""),
            "message_id": sent.get("message_id", ""),
        }
        _audit(result, text=text, buyer_text=resolved_buyer, audit_log=audit_log)
        return result
    finally:
        store.close()


def _audit(result: dict[str, Any], *, text: str, buyer_text: str, audit_log: str) -> None:
    append_audit(
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "action": result.get("action"),
            "reason": result.get("reason"),
            "cid": result.get("cid", ""),
            "toid": result.get("toid", ""),
            "dry_run": bool(result.get("dry_run")),
            "matched": result.get("matched", ""),
            "side": result.get("side", ""),
            "session_type": result.get("session_type"),
            "daily_sent": result.get("daily_sent"),
            "daily_cap": result.get("daily_cap"),
            "draft": redact(text),
            "buyer_text": redact(buyer_text),
            "error": result.get("error", ""),
        },
        audit_log or None,
    )
