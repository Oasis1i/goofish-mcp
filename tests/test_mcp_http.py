import secrets

import pytest
from starlette.testclient import TestClient

from goofish_mcp.agent_ops import AgentOps
from goofish_mcp.mcp_http import create_app


@pytest.fixture
def http(monkeypatch, tmp_path):
    read, write = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    monkeypatch.setenv("GOOFISH_MCP_TOKEN", read)
    monkeypatch.setenv("GOOFISH_MCP_WRITE_TOKEN", write)
    monkeypatch.setenv("GOOFISH_STATE_DIR", str(tmp_path))
    with TestClient(create_app(AgentOps()), base_url="http://127.0.0.1:8000") as client:
        yield client, read, write


def rpc(client, token, method, params=None):
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", headers=headers,
                       json={"jsonrpc": "2.0", "id": 1, "method": method,
                             "params": params or {}})


def test_requires_distinct_configured_tokens(monkeypatch):
    monkeypatch.delenv("GOOFISH_MCP_TOKEN", raising=False)
    monkeypatch.delenv("GOOFISH_MCP_WRITE_TOKEN", raising=False)
    with pytest.raises(ValueError):
        create_app()
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("GOOFISH_MCP_TOKEN", token)
    with pytest.raises(ValueError):
        create_app()
    monkeypatch.setenv("GOOFISH_MCP_WRITE_TOKEN", token)
    with pytest.raises(ValueError):
        create_app()


def test_unauthorized_every_request(http):
    client, _, _ = http
    for token in (None, secrets.token_urlsafe(32)):
        assert rpc(client, token, "tools/list").status_code == 401
    assert client.get("/mcp").status_code == 401


def test_only_high_level_tools_exposed(http):
    client, read, _ = http
    result = rpc(client, read, "tools/list")
    assert result.status_code == 200
    tools = {t["name"]: t for t in result.json()["result"]["tools"]}
    assert set(tools) == {"inbox_pending", "daily_stats", "reply", "escalate", "mark_handled"}
    assert set(tools["reply"]["inputSchema"]["properties"]) == {"cid", "text", "dry_run"}


def test_permissions_are_checked_on_each_call(http):
    client, read, write = http
    for token in (read, write):
        for name in ("inbox_pending", "daily_stats"):
            result = rpc(client, token, "tools/call", {"name": name})
            assert not result.json()["result"].get("isError")
    for name, args in (("reply", {"cid": "c", "text": "在的", "dry_run": True}),
                       ("escalate", {"cid": "c", "reason": "人工"}),
                       ("mark_handled", {"cid": "c"})):
        denied = rpc(client, read, "tools/call", {"name": name, "arguments": args})
        assert denied.json()["result"]["isError"]
        allowed = rpc(client, write, "tools/call", {"name": name, "arguments": args})
        assert not allowed.json()["result"].get("isError")
        denied_again = rpc(client, read, "tools/call", {"name": name, "arguments": args})
        assert denied_again.json()["result"]["isError"]
