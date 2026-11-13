# Agent 账号级 HTTP MCP

这是一个账号一个进程、一个本地 inbox 的入口，仅暴露五个任务工具。
传输使用 MCP Python SDK v1 的 Streamable HTTP，端点为 `/mcp`，不是旧 SSE 端点。
stdio 原入口 `python -m goofish_mcp.mcp_server` 保持原 CLI 工具集合。

## 在 Mac 上运行

在已安装 Python 3.11+ 的 Mac 上，从仓库目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
export GOOFISH_MCP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export GOOFISH_MCP_WRITE_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export GOOFISH_STATE_DIR="$HOME/.goofish-cli"
python -m goofish_mcp.mcp_http
```

默认监听 `127.0.0.1:8000`，用 `GOOFISH_MCP_HOST` / `GOOFISH_MCP_PORT` 修改。
两枚 token 必须都配置、非空、不同且不含空白，否则拒绝启动。请将实际 token
存入自己的密钥管理器，并在重启时注入环境变量；每次重新生成会使旧连接器失效。
不要提交 token 或把它们写进 URL。

每个 HTTP 请求必须带 `Authorization: Bearer <token>`；缺失或错误返回 401。
读 token 可调用 `inbox_pending`、`daily_stats`；写 token 可调用所有五个工具。
`reply` 即使 `dry_run=true` 也要求写 token。读 token 调写工具得到 MCP 工具错误
`write token required`，业务函数不会执行。工具发现可列出五个名称，但不授予调用权限。
入口采用无状态请求，权限不会随连接会话缓存，也不会因曾用过写 token 而升级。

本服务使用预共享 Bearer token，不提供 OAuth 授权流程。客户端需支持 Streamable HTTP
和自定义 Bearer token。SDK 传输参考：[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)。

## 工具及状态

| 工具 | 输入 | 行为 |
| --- | --- | --- |
| `inbox_pending` | `history_limit=10`（0–100） | 返回会话、未处理消息、合并买家正文、商品和最近历史 |
| `daily_stats` | 无 | 本机当天发送量、上限、冷却、自动待处理会话数、人工接管次数 |
| `reply` | `cid`, `text`, `dry_run=false` | 从服务端 inbox 解析对象和上下文，经既有护栏发送 |
| `escalate` | `cid`, `reason` | 持久记录人工接管与时间，不发送消息 |
| `mark_handled` | `cid` | 标记该会话现有消息已处理 |

商品信息只读取本地事件已有的 item_id、title/price/item_title/item_price；历史也只读
本地 inbox，包含已处理消息。这里不做在线补全。inbox 需由现有采集流程在本地填充；
HTTP 不提供 watch、登录、商品发布/删除、底层 message_send 等工具。

reply 不接受调用者传入 buyer_text、session_type、收件人、文件路径或额度覆盖。
沿用 send_guarded 的自身消息检查、真人会话检查、冷却、每日上限、外联扫描与敏感意图转人工；
真正发送仍走原 message send 限流与熔断。额度由本地既有 reply_policy / 管理员环境配置决定。
决策返回 action/reason/sent/daily_sent/daily_cap 等原字段。
发送成功后仍需调用 mark_handled，以便 bot 明确完成处理。

dry_run 不发送、不占额度、不自动接管，但保留既有决定审计与策略初始化行为。
非 dry_run 的 needs_human 会自动调用 escalate。手动或自动接管持久写入 SQLite
agent_handoffs 与 agent_escalations，并追加 JSONL 审计；统计以 SQLite 为准。
同一会话重复接管每次记为一次操作。新消息和 mark_handled 都不会清除人工接管状态，
防止下一轮重新自动回复；本轮不提供恢复自动处理工具，由人工处理接管会话。
旧 `message pending` 仍保留原语义，Agent 应使用 `inbox_pending`。
时间按服务器本机时区计算；同一 inbox 供一个账号使用，避免混用账号。

## 通过 HTTPS 暴露

推荐保持服务监听回环地址，通过隧道转发。请在本机另开终端配置隧道；本文没有部署任何服务。

Cloudflare Tunnel（已安装 cloudflared）：

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

取得公网域名后，将域名加入服务环境变量并重启服务，例如：

```bash
export GOOFISH_MCP_ALLOWED_HOSTS='your-tunnel.example.com'
python -m goofish_mcp.mcp_http
```

建议配置稳定域名的 named tunnel，避免临时域名每次变化。不要把所有 Host 都加入白名单。
若客户端发送 Origin，另设 `GOOFISH_MCP_ALLOWED_ORIGINS` 为客户端实际使用的完整 Origin
（多个值逗号分隔）；默认没有远程 Origin 白名单。Host / Origin 校验继续由 SDK 执行。

Tailscale 可用 `tailscale serve --bg http://127.0.0.1:8000` 暴露给 tailnet；
将它显示的域名加入 GOOFISH_MCP_ALLOWED_HOSTS 后重启。远程 bot 需能进入 tailnet；
若使用公网 Funnel，需在 Tailscale 中启用并按其当前配置要求设置 HTTPS 转发。
隧道必须保留 Authorization 请求头。读写 token 的规则在隧道之后仍然生效。

## 加入 Grok Bot / 同类 Agent

在客户端支持的账号级 MCP 连接器设置中，为该闲鱼账号添加一条 Streamable HTTP 连接：

- URL：`https://your-tunnel.example.com/mcp`（tailnet 客户端也可填对应域名）。
- Token：先使用 GOOFISH_MCP_TOKEN 对应的实际值验证读操作；客户端应发送 Bearer 头。
- 自动回复账号连接器使用 GOOFISH_MCP_WRITE_TOKEN 的实际值，它也可读取 inbox 和统计。

若设置页只接受 URL、没有 token / 自定义请求头，或只支持 OAuth，则它不能直接使用这个入口；
需在 bot 的 MCP 客户端配置中加入 Authorization 请求头，或使用支持预共享 Bearer 的连接器。
这里不假设 Grok Bot 某个版本的具体 UI，也不声称已连接或部署。

建议运营流程：inbox_pending → 判断 → reply（先 dry_run 验证）/ escalate → mark_handled。
不要对 needs_human 或 block 决策尝试绕过护栏。

## 本地验证

```bash
python3 -m pytest -q
python3 -m ruff check src tests
```

新增测试仅使用临时 SQLite、随机生成的临时环境 token、ASGI 本地客户端和 mock 发送器，
不会登录、连接闲鱼或发送真实消息。
