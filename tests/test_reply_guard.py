"""回复护栏。发送函数被替换，不连接闲鱼，也不改全局限流。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from goofish_mcp.core.audit_log import redact
from goofish_mcp.core.contact_scan import keyword_file, load_keywords, scan_text
from goofish_mcp.core.errors import GoofishError
from goofish_mcp.core.inbox import DEFAULT_COOLDOWN_SECONDS, DEFAULT_DAILY_CAP, Inbox
from goofish_mcp.core.limiter import DEFAULT_WRITE_RPM
from goofish_mcp.core.reply_guard import classify_intent, decide


def _keywords() -> str:
    return "\n".join(
        [
            "### 平台类",
            "| 原词 | 常见变体 |",
            "| --- | --- |",
            "| 测试外联词 | 测试变体 |",
            "### 诱导动作类",
            "- 加我 / 加一下",
            "### 身份类",
            "- 老板 / 哥",
            "## 合规替换话术库",
            "| 方便 | 方便加我 |",
        ]
    )


def test_keyword_loader_uses_the_risk_guard_reference_and_skips_gray_words():
    loaded = load_keywords(_keywords())
    assert loaded[0] == "测试外联词" or "测试外联词" in loaded
    assert "加我" in loaded
    assert "老板" not in loaded
    assert "方便" not in loaded
    assert "哥" not in loaded

    real = load_keywords(keyword_file().read_text(encoding="utf-8"))
    assert "微信" in real
    assert "VX" in real or "Vx" in real
    assert "加我" in real
    assert "方便" not in real


def test_scan_hits_contact_words_phone_and_payment_but_not_a_normal_reply(tmp_path):
    path = tmp_path / "words.md"
    path.write_text(_keywords(), encoding="utf-8")
    assert scan_text("在的，今天下午寄出", path=path) == []
    assert scan_text("1500元可以", path=path) == []
    assert scan_text("这里有测试外联词", path=path)[0]["word"] == "测试外联词"
    assert scan_text("13800138000", path=path)[0]["word"] == "手机号"
    assert scan_text("支付宝转我", path=path)[0]["word"] == "支付宝"
    assert any(hit["word"] == "QQ号" for hit in scan_text("QQ 12345678", path=path))
    assert scan_text("直接转过去", path=path)[0]["kind"] == "combo"
    real = scan_text("加个微信聊")
    assert real and real[0]["word"] == "微信"


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("我要投诉你", "complaint"),
        ("申请仅退款", "refund"),
        ("收到就坏了", "after_sales"),
        ("能便宜点吗", "bargain"),
        ("在吗，还在吗", ""),
        ("包邮吗", ""),
    ],
)
def test_classify_buyer_intent(text, intent):
    assert classify_intent(text) == intent


def _base(**overrides):
    ctx = {
        "text": "在的，还在的",
        "buyer_text": "在吗",
        "session_type": 1,
        "from_self": False,
        "send_user_id": "buyer-1",
        "my_user_id": "seller-1",
        "toid": "buyer-1",
        "dry_run": False,
        "daily_sent": 0,
        "daily_cap": 15,
        "cooldown_remaining": 0,
    }
    ctx.update(overrides)
    return ctx


def test_decide_blocks_unsafe_replies_and_escalates_sensitive_buyers(tmp_path):
    path = tmp_path / "words.md"
    path.write_text(_keywords(), encoding="utf-8")
    assert decide(**_base(from_self=True))["reason"] == "own_message"
    assert decide(**_base(send_user_id="seller-1", buyer_text=""))["reason"] == "own_message"
    assert decide(**_base(toid="seller-1"))["reason"] == "recipient_is_self"
    assert decide(**_base(session_type=3))["reason"] == "non_human_session"
    assert decide(**_base(session_type=0))["reason"] == "non_human_session"
    assert decide(**_base(session_type=None))["reason"] == "non_human_session"
    assert decide(**_base(buyer_text=""))["reason"] == "missing_context"
    assert decide(**_base(buyer_text="能便宜点吗"))["reason"] == "bargain"
    assert decide(**_base(buyer_text="我要退款"))["reason"] == "refund"
    assert decide(**_base(buyer_text="我要投诉"))["action"] == "needs_human"
    assert decide(**_base(buyer_text="收到就坏了"))["reason"] == "after_sales"
    blocked = decide(**_base(text="加个测试外联词", keyword_path=path))
    assert blocked["action"] == "block"
    assert blocked["side"] == "draft"
    buyer = decide(**_base(buyer_text="我的测试外联词是这个", keyword_path=path))
    assert buyer["action"] == "needs_human"
    assert buyer["side"] == "buyer"
    assert decide(**_base(dry_run=True))["action"] == "dry_run"
    assert decide(**_base(daily_sent=15))["reason"] == "daily_cap"
    assert decide(**_base(cooldown_remaining=10))["reason"] == "cooldown"
    assert decide(**_base())["action"] == "send"


def test_default_cap_stays_15_and_global_write_limit_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOFISH_REPLY_DAILY_CAP", raising=False)
    monkeypatch.delenv("GOOFISH_REPLY_COOLDOWN_SECONDS", raising=False)
    with Inbox(tmp_path / "inbox.sqlite") as store:
        limits = store.limits(today="2026-11-11")
    assert limits["daily_cap"] == DEFAULT_DAILY_CAP == 15
    assert limits["cooldown_seconds"] == DEFAULT_COOLDOWN_SECONDS == 1200
    assert DEFAULT_WRITE_RPM == 1


def test_guarded_send_persists_cap_cooldown_and_audit(tmp_path, monkeypatch):
    from goofish_mcp.commands.message.reply import send_guarded

    monkeypatch.delenv("GOOFISH_REPLY_DAILY_CAP", raising=False)
    monkeypatch.delenv("GOOFISH_REPLY_COOLDOWN_SECONDS", raising=False)
    path = tmp_path / "inbox.sqlite"
    audit = tmp_path / "audit.jsonl"
    calls: list[tuple] = []

    def fake_send(cid, toid, text, **_kwargs):
        calls.append((cid, toid, text))
        return {"ok": True, "mid": "mid-1", "message_id": "srv-1"}

    common = {
        "my_user_id": "seller-1",
        "cid": "test-cid",
        "toid": "buyer-1",
        "text": "在的，还在的",
        "buyer_text": "在吗",
        "session_type": 1,
        "inbox": str(path),
        "audit_log": str(audit),
        "sender": fake_send,
        "daily_cap": 1,
        "cooldown_seconds": 100,
        "today": "2026-11-11",
    }
    dry = send_guarded(**common, dry_run=True, now=1_000)
    assert dry["action"] == "dry_run"
    assert calls == []

    sent = send_guarded(**common, now=1_000)
    assert sent["action"] == "sent"
    assert sent["sent"] is True
    assert calls == [("test-cid", "buyer-1", "在的，还在的")]

    cooled = send_guarded(**common, now=1_010)
    assert cooled["reason"] == "cooldown"
    assert len(calls) == 1

    later = send_guarded(**common, now=1_200)
    assert later["reason"] == "daily_cap"

    next_day = send_guarded(**{**common, "today": "2026-11-12"}, now=1_200 + 86_400)
    assert next_day["action"] == "sent"
    assert len(calls) == 2

    with Inbox(path) as store:
        assert store.limits(today="2026-11-12")["daily_cap"] == 1
        assert store.quota_view("buyer-1", now=1_200 + 86_400, today="2026-11-12")["daily_sent"] == 1

    reasons = [json.loads(line)["reason"] for line in audit.read_text(encoding="utf-8").splitlines()]
    assert reasons == ["would_send", "sent", "cooldown", "daily_cap", "sent"]
    assert all(item["draft"] == "在的，还在的" for item in (json.loads(line) for line in audit.read_text().splitlines()))


def test_failed_send_does_not_keep_the_daily_slot(tmp_path, monkeypatch):
    from goofish_mcp.commands.message.reply import send_guarded

    monkeypatch.delenv("GOOFISH_REPLY_DAILY_CAP", raising=False)
    monkeypatch.delenv("GOOFISH_REPLY_COOLDOWN_SECONDS", raising=False)
    path = tmp_path / "inbox.sqlite"

    def boom(_cid, _toid, _text, **_kwargs):
        raise GoofishError("发送未被服务端接受")

    with pytest.raises(GoofishError, match="未被服务端接受"):
        send_guarded(
            my_user_id="seller-1",
            cid="test-cid",
            toid="buyer-1",
            text="在的",
            buyer_text="在吗",
            session_type=1,
            daily_cap=1,
            cooldown_seconds=0,
            inbox=str(path),
            audit_log=str(tmp_path / "audit.jsonl"),
            now=50,
            today="2026-11-11",
            sender=boom,
        )
    with Inbox(path) as store:
        assert store.quota_view("buyer-1", now=60, today="2026-11-11")["daily_sent"] == 0


def test_reply_reads_inbox_context_and_does_not_call_send_for_system_or_bargain(tmp_path, monkeypatch):
    from goofish_mcp.commands.message.reply import send_guarded

    monkeypatch.delenv("GOOFISH_REPLY_DAILY_CAP", raising=False)
    monkeypatch.delenv("GOOFISH_REPLY_COOLDOWN_SECONDS", raising=False)
    path = tmp_path / "inbox.sqlite"
    calls: list[str] = []

    def fake_send(*_args, **_kwargs):
        calls.append("sent")
        return {"mid": "m", "message_id": "srv"}

    with Inbox(path) as store:
        store.append_event(
            {
                "event": "message",
                "cid": "sys-cid",
                "message_id": "sys-1",
                "send_user_id": "notice",
                "send_message": "系统通知",
                "session_type": 23,
                "from_self": False,
                "created_at": 10,
            },
            source="watch",
        )
        store.append_event(
            {
                "event": "message",
                "cid": "test-cid",
                "message_id": "b-1",
                "send_user_id": "buyer-1",
                "send_message": "能便宜点吗",
                "session_type": 1,
                "from_self": False,
                "created_at": 20,
                "peer_user_id": "buyer-1",
            },
            source="watch",
        )
    system = send_guarded(
        my_user_id="seller-1",
        cid="sys-cid",
        toid="notice",
        text="收到",
        inbox=str(path),
        audit_log=str(tmp_path / "audit.jsonl"),
        sender=fake_send,
        now=30,
        today="2026-11-11",
        cooldown_seconds=0,
    )
    bargain = send_guarded(
        my_user_id="seller-1",
        cid="test-cid",
        toid="buyer-1",
        text="看描述哦",
        inbox=str(path),
        audit_log=str(tmp_path / "audit.jsonl"),
        sender=fake_send,
        now=30,
        today="2026-11-11",
        cooldown_seconds=0,
    )
    assert system["reason"] == "non_human_session"
    assert bargain["reason"] == "bargain"
    assert bargain["action"] == "needs_human"
    assert calls == []


def test_reply_command_loads_session_without_sending(monkeypatch, tmp_path):
    from goofish_mcp.commands.message import reply as mod

    monkeypatch.delenv("GOOFISH_REPLY_DAILY_CAP", raising=False)
    monkeypatch.setattr(mod.Session, "load", lambda: SimpleNamespace(unb="seller-1"))
    called = []
    monkeypatch.setattr(mod, "send_message", lambda *args, **kwargs: called.append(args))
    result = mod.reply(
        "test-cid",
        "buyer-1",
        "加个微信",
        buyer_text="在吗",
        session_type=1,
        inbox=str(tmp_path / "inbox.sqlite"),
        audit_log=str(tmp_path / "audit.jsonl"),
        cooldown_seconds=0,
    )
    assert result["action"] == "block"
    assert result["matched"] == "微信"
    assert called == []
    saved = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8"))
    assert saved["reason"] == "external_contact"
    assert saved["matched"] == "微信"


def test_audit_redacts_long_numbers():
    assert redact("电话13800138000谢谢") == "电话###########谢谢"
