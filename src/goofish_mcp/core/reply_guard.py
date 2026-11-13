"""决定一条客服回复能不能发。只做判断，不连网，也不改额度。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from goofish_mcp.core.contact_scan import scan_text

_INTENTS = (
    ("complaint", ("投诉", "差评", "举报", "骗子", "欺诈", "工商", "曝光")),
    ("refund", ("仅退款", "退款", "退钱", "退货", "退我")),
    ("after_sales", ("售后", "坏了", "损坏", "破损", "质量问题", "描述不符", "不能用", "少发", "漏发", "瑕疵")),
    ("bargain", ("便宜", "砍价", "少点", "最低", "小刀", "刀一下", "优惠", "降价", "能少")),
)


def classify_intent(text: str) -> str:
    """售后、退款、议价、投诉。没命中返回空字符串。"""
    for name, words in _INTENTS:
        if any(word and word in text for word in words):
            return name
    return ""


def decide(
    *,
    text: str,
    buyer_text: str,
    session_type: int | None,
    from_self: bool,
    send_user_id: str,
    my_user_id: str,
    toid: str,
    dry_run: bool,
    daily_sent: int,
    daily_cap: int,
    cooldown_remaining: float,
    keyword_path: Path | None = None,
) -> dict[str, Any]:
    """返回 action: skip / needs_human / block / dry_run / send。"""
    mine = str(my_user_id or "").removesuffix("@goofish").strip()
    sender = str(send_user_id or "").removesuffix("@goofish").strip()
    recipient = str(toid or "").removesuffix("@goofish").strip()

    def finish(action: str, reason: str, **extra: Any) -> dict[str, Any]:
        result = {
            "action": action,
            "reason": reason,
            "ok": action in {"send", "dry_run"},
            "sent": False,
            "dry_run": dry_run,
            "matched": extra.get("matched", ""),
            "side": extra.get("side", ""),
            "daily_sent": daily_sent,
            "daily_cap": daily_cap,
        }
        result.update(extra)
        return result

    if from_self or (sender and mine and sender == mine):
        return finish("skip", "own_message")
    if recipient and mine and recipient == mine:
        return finish("skip", "recipient_is_self")
    if session_type != 1:
        return finish("skip", "non_human_session")
    if not buyer_text.strip():
        return finish("needs_human", "missing_context")

    buyer_hits = scan_text(buyer_text, path=keyword_path)
    if buyer_hits:
        return finish(
            "needs_human",
            "external_contact",
            matched=buyer_hits[0]["word"],
            side="buyer",
        )
    intent = classify_intent(buyer_text)
    if intent:
        return finish("needs_human", intent, matched=intent)

    draft_hits = scan_text(text, path=keyword_path)
    if draft_hits:
        return finish(
            "block",
            "external_contact",
            matched=draft_hits[0]["word"],
            side="draft",
        )
    if cooldown_remaining > 0:
        return finish("skip", "cooldown", cooldown_remaining=cooldown_remaining)
    if daily_sent >= daily_cap:
        return finish("skip", "daily_cap")
    if dry_run:
        return finish("dry_run", "would_send")
    return finish("send", "allowed")
