"""本地收件箱、断线补洞和「重连不回放历史」。不连接闲鱼。"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from types import SimpleNamespace

from goofish_mcp.core.inbox import Inbox, event_from_history, resolve_inbox_path
from goofish_mcp.core.ws import collect_session_cids, register, run_forever


def _buyer(cid: str = "test-cid", message_id: str = "m1", text: str = "在吗", created_at: int = 2000) -> dict:
    return {
        "event": "message",
        "cid": cid,
        "message_id": message_id,
        "send_user_id": "buyer-1",
        "send_user_name": "买家",
        "send_message": text,
        "session_type": 1,
        "item_id": "1045171414271",
        "created_at": created_at,
        "from_self": False,
        "peer_user_id": "buyer-1",
        "peer_nick": "买家",
    }


def test_inbox_path_is_under_state_dir_and_overridable(monkeypatch, tmp_path):
    monkeypatch.delenv("GOOFISH_INBOX_PATH", raising=False)
    monkeypatch.setenv("GOOFISH_STATE_DIR", str(tmp_path))
    assert resolve_inbox_path() == tmp_path / "inbox.sqlite"
    monkeypatch.setenv("GOOFISH_INBOX_PATH", str(tmp_path / "custom.sqlite"))
    assert resolve_inbox_path() == tmp_path / "custom.sqlite"
    assert resolve_inbox_path(tmp_path / "explicit.sqlite") == tmp_path / "explicit.sqlite"


def test_dedup_groups_pending_and_skips_own_messages(tmp_path):
    with Inbox(tmp_path / "inbox.sqlite") as store:
        assert store.append_event(_buyer(text="在吗", message_id="m1", created_at=1000), source="watch")
        assert store.append_event(_buyer(text="还在吗", message_id="m2", created_at=2000), source="watch")
        assert not store.append_event(_buyer(text="在吗", message_id="m1", created_at=1000), source="watch")
        assert store.append_event(
            {**_buyer(message_id="mine", text="在的", created_at=1500), "from_self": True, "send_user_id": "seller-1"},
            source="watch",
        )
        assert store.append_event(
            {**_buyer(cid="other", message_id="m9", text="你好"), "from_self": True, "send_user_id": "seller-1"},
            source="watch",
        )
        no_id = _buyer(message_id="", text="第一条无 id")
        no_id.pop("message_id")
        assert store.append_event(no_id, source="watch")
        assert store.append_event({**no_id, "send_message": "第二条无 id"}, source="watch")
        pending = store.pending()

    assert pending["total_conversations"] == 1
    group = pending["conversations"][0]
    assert group["cid"] == "test-cid"
    assert group["merged_text"] == "在吗\n还在吗\n第一条无 id\n第二条无 id"
    assert any(item["from_self"] for item in group["messages"])
    assert group["peer_user_id"] == "buyer-1"


def test_mark_handled_then_only_newer_messages_reopen(tmp_path):
    path = tmp_path / "inbox.sqlite"
    with Inbox(path) as store:
        store.append_event(_buyer(message_id="old", created_at=1000, text="旧"), source="watch")
        marked = store.mark_handled("test-cid@goofish", now_ms=1500)
        assert marked["handled"] is True
        assert marked["marked_messages"] == 1
        assert store.pending()["total_conversations"] == 0
        assert store.append_event(_buyer(message_id="older", created_at=1200, text="补洞旧消息"), source="history")
        assert store.pending()["total_conversations"] == 0
        assert store.append_event(_buyer(message_id="new", created_at=2000, text="新的"), source="watch")
        pending = store.pending()
    assert pending["total_conversations"] == 1
    assert pending["conversations"][0]["merged_text"] == "新的"


def test_register_ackdiff_uses_now_and_watch_reconnect_does_not_read_history():
    sent: list[dict] = []

    class WS:
        async def send(self, raw: str) -> None:
            sent.append(json.loads(raw))

    before = int(time.time() * 1000) * 1000
    asyncio.run(register(WS(), SimpleNamespace(device_id="dev-1"), "token"))
    after = int(time.time() * 1000) * 1000
    ack = next(frame for frame in sent if frame["lwp"] == "/r/SyncStatus/ackDiff")
    pts = ack["body"][0]["pts"]
    assert pts != 0
    assert before <= pts <= after

    reconnect = inspect.getsource(run_forever)
    assert "read_history" not in reconnect
    assert "list_user_messages" not in reconnect
    assert "collect_session_cids" not in reconnect
    # pts=0 只属于短时会话发现，不在 watch 的重连循环里。
    assert '"pts": 0' in inspect.getsource(collect_session_cids)


def test_collect_appends_watch_events_and_dedups(monkeypatch, tmp_path, capsys):
    from goofish_mcp.commands.message import collect as mod

    path = tmp_path / "inbox.sqlite"
    payload = {
        "sessionId": "test-cid",
        "messageId": "test-msg.PNM",
        "createAt": 1776770736198,
        "operation": {
            "content": {"contentType": 1, "text": {"text": "在吗"}},
            "senderInfo": {"senderUserId": "buyer-1"},
            "sessionInfo": {"sessionType": 1, "extensions": {"itemId": "1045171414271"}},
        },
    }
    frame = {"lwp": "/s/sync", "body": {"syncPushPackage": {"data": [{"data": json.dumps(payload)}]}}}
    notice = {"lwp": "/s/sync", "body": {"syncPushPackage": {"data": [{"data": json.dumps({
        "1": "test-cid@goofish", "2": 1, "3": "notice.PNM", "4": "1776770736064",
    })}]}}}

    async def fake_run(_session, handler=None, **_kwargs):
        await handler(frame, None)
        await handler(frame, None)
        await handler(notice, None)

    monkeypatch.setattr(mod, "run_forever", fake_run)
    monkeypatch.setattr(mod.Session, "load", lambda: SimpleNamespace(unb="seller-1", tracknick=""))
    result = mod.collect(inbox=str(path))
    assert result["ok"] is True
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 3
    assert json.loads(out[0])["from_self"] is False
    assert json.loads(out[2])["event"] == "new_msg"
    with Inbox(path) as store:
        pending = store.pending()
    assert pending["total_messages"] == 1
    assert pending["conversations"][0]["messages"][0]["message_id"] == "test-msg.PNM"
    assert pending["conversations"][0]["item_id"] == "1045171414271"


def test_backfill_recovers_gap_without_touching_system_chats(monkeypatch, tmp_path):
    from goofish_mcp.commands.message import backfill as mod

    path = tmp_path / "inbox.sqlite"
    with Inbox(path) as store:
        store.append_event(_buyer(message_id="already", text="已经在队列", created_at=1000), source="watch")

    seen: dict = {}

    def fake_list_chats(**kwargs):
        seen.update(kwargs)
        return {
            "sessions": [
                {
                    "session_id": "test-cid",
                    "session_type": 1,
                    "ts": 3000,
                    "item_id": "1045171414271",
                    "peer_user_id": "buyer-1",
                    "peer_nick": "买家",
                },
                {"session_id": "sys-cid", "session_type": 3, "ts": 99999, "peer_user_id": "", "peer_nick": ""},
            ]
        }

    def fake_history(cid, limit_per_page=20, limit=0, timeout=30.0):
        assert cid == "test-cid"
        assert limit == 20
        return [
            {
                "cid": cid,
                "message_id": "already",
                "created_at": 1000,
                "session_type": 1,
                "send_user_id": "buyer-1",
                "send_user_name": "买家",
                "message": {"contentType": 1, "text": {"text": "已经在队列"}},
            },
            {
                "cid": cid,
                "message_id": "missed",
                "created_at": 2000,
                "session_type": 1,
                "send_user_id": "buyer-1",
                "send_user_name": "买家",
                "message": {"contentType": 1, "text": {"text": "睡着时发的"}},
            },
            {
                "cid": cid,
                "message_id": "mine",
                "created_at": 2500,
                "session_type": 1,
                "send_user_id": "seller-1",
                "send_user_name": "我",
                "message": {"contentType": 1, "text": {"text": "在的"}},
            },
        ]

    monkeypatch.setattr(mod, "list_chats", fake_list_chats)
    monkeypatch.setattr(mod, "history", fake_history)
    monkeypatch.setattr(mod.Session, "load", lambda: SimpleNamespace(unb="seller-1"))
    result = mod.backfill(inbox=str(path))
    assert seen["watch_secs"] == 0
    assert result["chats"] == 1
    assert result["inserted"] == 2
    assert result["duplicates"] == 1
    assert result["reconnect_replays_history"] is False
    with Inbox(path) as store:
        pending = store.pending()
    assert pending["conversations"][0]["merged_text"] == "已经在队列\n睡着时发的"
    assert any(item["from_self"] and item["message_id"] == "mine" for item in pending["conversations"][0]["messages"])


def test_backfill_rejects_unbounded_history():
    import pytest

    from goofish_mcp.commands.message.backfill import backfill
    from goofish_mcp.core.errors import GoofishError

    with pytest.raises(GoofishError, match="per-chat-limit"):
        backfill(per_chat_limit=0)


def test_pending_and_mark_handled_commands(tmp_path):
    from goofish_mcp.commands.message.mark_handled import mark_handled
    from goofish_mcp.commands.message.pending import pending

    path = str(tmp_path / "inbox.sqlite")
    with Inbox(path) as store:
        store.append_event(_buyer(), source="watch")
    listed = pending(inbox=path)
    assert listed["total_messages"] == 1
    done = mark_handled("test-cid", inbox=path)
    assert done["handled"] is True
    assert pending(inbox=path)["total_conversations"] == 0


def test_history_event_marks_self_messages():
    event = event_from_history(
        {
            "cid": "test-cid@goofish",
            "message_id": "mine",
            "created_at": 10,
            "send_user_id": "seller-1",
            "send_user_name": "我",
            "message": {"contentType": 1, "text": {"text": "在的"}},
        },
        my_user_id="seller-1",
        session_type=1,
        peer_user_id="seller-1",
    )
    assert event["from_self"] is True
    assert event["cid"] == "test-cid"
    assert event["peer_user_id"] == ""
    assert event["send_message"] == "在的"
