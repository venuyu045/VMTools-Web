# 标点中继 API（`/api/ping`）—— 后端视角

> 状态：**v1.2.1**（2026-11-03）｜实现：`backend/src/vmtools_next/api/routers/ping.py` + `backend/src/vmtools_next/core/ping_relay.py` + `core/fashion_auth.py`
> 客户端契约全文（房间算法、轮盘、渲染、错误处理）见 VMPing-mod 仓库的 `docs/协议-v1.md`（v1.1）；
> 本文只记录**后端**侧行为与本次 v1.2 变更，不搬运客户端协议。

## 1. 端点

| 方法 | 路径 | 鉴权 | 成功 |
|---|---|---|---|
| POST | `/api/ping/auth/challenge` | 无 | 200：一次性 `serverId`（TTL 120s，绑定来源 IP） |
| POST | `/api/ping/auth/verify` | 无 | 200：签发 `aud="ping"` 的 token（默认 24h） |
| POST | `/api/ping/rooms/{roomId}/pings` | Bearer | **201**：`{id, seq, serverTime, expiresAt}` |
| GET | `/api/ping/rooms/{roomId}/pings?since=&timeout=&dimension=` | Bearer | 200：长轮询（最多等 25s） |

错误体统一 `{"detail": {"code": "...", "message": "..."}}`；429 / 503 额外带 `Retry-After`（秒）。
`X-VMPing-Version` 只记日志，不做版本强校验。

## 2. token 受众（v1.2 变更）

| 调用方 | token | `/api/ping` 业务路由 | 时装路由 `/api/fashion/*` |
|---|---|---|---|
| 独立 VMPing 0.1.0 | `aud="ping"`（`/api/ping/auth/verify` 签发） | ✅ 接受 | ❌ 401 |
| VMFashion 2.0 合订版 | `aud="fashion"`（时装认证 `/api/fashion/auth/verify` 签发） | ✅ 接受（v1.2 起） | ✅ 接受 |
| 其它受众（如 `aud="viewer"`）/ 无效 / 过期 | — | ❌ 401 `bad_token` | ❌ 401 |

要点：

- **只放宽了一处**：`/api/ping` 的两个业务路由（发标点 / 长轮询）同时接受 `aud ∈ {ping, fashion}`。
  合订版把标点功能并进时装 mod 后，客户端复用时装认证拿到的 JWT，不再调 ping 自己的 challenge/verify。
- **认证接口行为不变**：`/api/ping/auth/challenge` + `/verify` 仍签发 `aud="ping"`，独立 VMPing 0.1.0
  客户端照原样认证；`serverId` 仍记录用途，ping 的 `serverId` 换不到 fashion token（反之亦然）。
- **时装路由不变**：仍然只接受 `aud="fashion"`（或网页 JWT），ping token 调时装接口依旧 401 `invalid_token`。
- 受众隔离靠 JWT `aud`，签名密钥相同；中继不落库，principal 直接取 claims 里的 `mcUuid` / `mcName`。

实现：

- `core/fashion_auth.py` 新增 `decode_token_any(token, *, audiences, settings=None, secret=None)`：
  按顺序尝试每个受众的 `decode_token`，第一个成功即返回；全部失败统一抛
  `FashionAuthError("invalid_token", "token 无效或已过期", 401)`。
  `server_misconfigured` 这类与 token 无关的错误**原样上抛**——多受众尝试不会把
  「服务端没配密钥」吞成 401。既有 `decode_token` 的签名与行为未变。
- `api/routers/ping.py`：`TOKEN_AUDIENCES = (AUDIENCE_PING, AUDIENCE_FASHION)` 供业务路由使用；
  `TOKEN_AUDIENCE = AUDIENCE_PING` 仍用于 challenge/verify 的签发与 `serverId` 消费。

## 3. 错误码（业务路由）

| code | HTTP | 场景 |
|---|---|---|
| `unauthorized` | 401 | 缺少 `Authorization: Bearer` |
| `bad_token` | 401 | 签名错 / 过期 / 受众不在 `{ping, fashion}` / claims 缺有效 `mcUuid` |
| `server_error` | 500 | 服务端未配置 JWT 签名密钥（`server_misconfigured`） |
| `bad_request` | 400 | `roomId` 非法、坐标非有限或越界、`kind` 不在白名单、字段超长 |
| `rate_limited` | 429 | 每玩家 5 条 / 3s（带 `Retry-After`） |
| `room_full` | 429 | 房间存活标点达上限（200） |
| `server_busy` | 503 | 全局长轮询并发打满（带 `Retry-After`） |

认证接口沿用 `fashion_auth` 的细分 code（`invalid_uuid` / `challenge_expired` / `not_joined` /
`uuid_mismatch` / `upstream_unavailable` …），客户端按 HTTP 状态码降级即可。

## 4. 容量默认值（`VMT_PING__*` 可覆盖）

| 项 | 默认 | 说明 |
|---|---|---|
| 每玩家发送 | 5 条 / 3 秒 | 滑动窗口，超限 429 |
| 每房间存活标点 | 200 | 满 → `room_full` |
| 标点 TTL | 20 秒 | 惰性清理 |
| 房间空闲回收 | 60 秒 | 无活动且无等待者即回收（`seq` 随之归零）。生产没有周期任务，除「全局房间数打满」外还在**发送路径**上每 64 次兜底扫一遍（v1.2.1） |
| 全局房间数 | 1000 | |
| 长轮询等待者 / 房间 | 64 | 超出**立即返回 200 空数组** |
| 全局长轮询并发 | 5000 | 超出 503 `server_busy` |
| 认证 challenge | 每 IP 20 次 / 60 秒 | 防刷 |

纯内存单进程：多 worker 部署时同一房间的玩家会落到不同进程，互相收不到标点。

## 5. 验证

### 5.1 单测

```bash
cd backend && .venv/bin/python -m pytest \
  tests/test_ping_relay.py tests/test_ping_api.py tests/test_ping_http.py \
  tests/test_fashion_auth.py tests/test_fashion_http.py -q
```

覆盖：fashion token 发标点 201 / 长轮询 200、ping token 回归、无效 / 过期 / 未知受众 401、
`server_misconfigured` → 500 `server_error`、ping token 调时装接口 401。

### 5.2 生产冒烟（服务器本机，`$TOKEN` 不要贴到公开日志）

```bash
# 1) 用生产 venv 签一个 fashion token（identity_id 任意合法 UUID；中继只用 claims 的 mcUuid/mcName）
cd /opt/vmtools-next/vmtools-next/backend && .venv/bin/python -c "
from vmtools_next.core.fashion_auth import issue_token
print(issue_token('11111111-1111-1111-1111-111111111111',
                  mc_uuid='11111111-1111-1111-1111-111111111111',
                  mc_name='PingTest', audience='fashion')[0])
"

# 2) 发标点 → 期望 201；长轮询 → 期望 200
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://127.0.0.1:8080/api/ping/rooms/0123456789abcdef/pings \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"dimension":"minecraft:overworld","x":1.5,"y":64.0,"z":-2.5,"kind":"default"}'
curl -s -o /dev/null -w '%{http_code}\n' \
  "http://127.0.0.1:8080/api/ping/rooms/0123456789abcdef/pings?since=0&timeout=0" \
  -H "Authorization: Bearer $TOKEN"

# 3) 回归：aud=ping token 发标点同样 201；aud=ping token 调 GET /api/fashion/catalog 仍 401
```

## 6. 变更记录

- **v1.2.1（2026-11-03，第 7 轮审查）**：房间空闲回收补上「发送路径每 64 次兜底扫一遍」
  （此前 `prune()` 只是运维探针、生产从不调用，`room_idle_seconds` 只在全局房间数打满时
  才生效，被丢下的房间会带着过期标点长期驻留内存）。协议/端点/错误码/受众策略**均无变化**。
- **v1.2（2026-11-03）**：`/api/ping` 业务路由接受 `aud ∈ {ping, fashion}`（VMFashion 2.0 合订版复用
  时装 token）；`/auth/challenge` + `/auth/verify` 与独立 VMPing 0.1.0 行为不变；时装路由仍只收
  `aud="fashion"`。新增 `fashion_auth.decode_token_any()`。
- **v1.1（2026-11-02）**：长轮询等待者超限改为立即 200 空数组、服务端回显发送者自己的标点等
  （详见 VMPing-mod 仓库 `docs/协议-v1.md`）。
