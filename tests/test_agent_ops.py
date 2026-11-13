from types import SimpleNamespace

import pytest

from goofish_mcp.agent_ops import AgentOps
from goofish_mcp.core.inbox import Inbox


@pytest.fixture
def ops(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOFISH_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("GOOFISH_REPLY_DAILY_CAP", raising=False)
    monkeypatch.delenv("GOOFISH_REPLY_COOLDOWN_SECONDS", raising=False)
    monkeypatch.setattr("goofish_mcp.agent_ops.Session.load",
                        lambda: SimpleNamespace(unb="seller"))
    return AgentOps()


def seed(cid="c", text="在吗", **extra):
    with Inbox() as store:
        store.append_event({"cid": cid, "message_id": f"{cid}-{text}",
                            "send_message": text, "send_user_id": "buyer",
                            "peer_user_id": "buyer", "peer_nick": "买家",
                            "item_id": "item", "title": "本地商品", "price": "20",
                            "session_type": 1, "from_self": False, **extra}, source="watch")


def test_pending_history_and_item_are_local(ops):
    seed(text="之前")
    ops.mark_handled("c")
    seed(text="在吗")
    result = ops.inbox_pending(1)
    conv = result["conversations"][0]
    assert conv["merged_text"] == "在吗"
    assert conv["peer_nick"] == "买家"
    assert conv["item"] == {"item_id": "item", "title": "本地商品", "price": "20"}
    assert len(conv["recent_history"]) == 1
    assert len(ops.inbox_pending()["conversations"][0]["recent_history"]) == 2
    with pytest.raises(Exception, match="history_limit"):
        ops.inbox_pending(-1)


def test_escalation_survives_new_messages_and_mark_handled(ops):
    seed()
    result = ops.escalate("c", "需要人工核实")
    assert result["escalated"]
    seed(text="又一条")
    assert ops.inbox_pending()["total_conversations"] == 0
    assert ops.reply("c", "收到")["reason"] == "escalated"
    assert ops.daily_stats()["escalations"] == 1
    ops.mark_handled("c")
    seed(text="再一条")
    assert ops.inbox_pending()["total_conversations"] == 0


@pytest.mark.parametrize(("buyer", "kind", "draft", "reason"), [
    ("能便宜吗", 1, "在的", "bargain"),
    ("退款", 1, "在的", "refund"),
    ("坏了", 1, "在的", "after_sales"),
    ("投诉", 1, "在的", "complaint"),
    ("在吗", 23, "在的", "non_human_session"),
    ("在吗", 1, "加微信", "external_contact"),
    ("加微信", 1, "在的", "external_contact"),
])
def test_reply_uses_server_context(ops, buyer, kind, draft, reason, monkeypatch):
    seed(text=buyer, session_type=kind)
    monkeypatch.setattr("goofish_mcp.commands.message.reply.send_message",
                        lambda *args: pytest.fail("unsafe send"))
    assert ops.reply("c", draft)["reason"] == reason


def test_reply_dry_run_send_cooldown_and_cap(ops, monkeypatch):
    seed()
    calls = []
    monkeypatch.setattr("goofish_mcp.commands.message.reply.send_message",
                        lambda *args: calls.append(args) or {"mid": "mock"})
    assert ops.reply("c", "在的", dry_run=True)["action"] == "dry_run"
    assert not calls
    assert ops.reply("c", "在的")["sent"]
    assert ops.reply("c", "在的")["reason"] == "cooldown"
    seed(cid="other", peer_user_id="other", send_user_id="other")
    with Inbox() as store:
        store.set_limits(daily_cap=1)
    assert ops.reply("other", "在的")["reason"] == "daily_cap"
    assert ops.daily_stats()["daily_sent"] == 1
    assert len(calls) == 1


def test_self_and_handled_never_send(ops):
    seed(from_self=True, send_user_id="seller")
    assert not ops.reply("c", "在的")["sent"]
    assert not ops.reply("absent", "在的")["sent"]
