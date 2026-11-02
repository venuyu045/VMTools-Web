# VMTools MCC 远程管理平台 — 部署与运维指南

## 概述

MCC（Minecraft Console Client）远程管理是 VMTools-Web（历史名 vmtools-next）的一站式 Web 管理平台。用户无需 SSH 登录服务器，即可在浏览器中完成 MCC 多实例创建、账号配置、启动/停止、实时终端、文件编辑等全部操作。

## ⚠️ Minecraft 26.3 必读（2026-10-25）

粉丝服已升级 **MC 26.3（协议 777）**。MCC 445/519 默认**进服 1~2 秒被踢**：

```
You were kicked from main: 收到了包含非法玩家移动的数据包
（multiplayer.disconnect.invalid_player_movement）
```

**根因**：26.3 服务端强制「每个客户端 tick 只能发一个位移包」，且计数器**只在收到 serverbound
`client_tick_end` 时重置**。原版客户端自 **1.21.2** 起每 tick 结束都发这个空包；
MCC 的 `PacketPalette261` 里虽然登记了 `ClientTickEnd` 的 ID，但**代码里从来没有发送调用**。

**修复**：给 `Protocol18.cs` 的 `Updater()` 主循环补发（见仓库 `mcc-runtime-patch/`，
含完整构建脚本、部署与回滚步骤）。打好补丁的单文件二进制约 86 MB，
`mcc_version` 建议填 **26.2**（MCC 最高支持 776，26.3 由 Velocity 的 Via 向下翻译）。

**构建环境两个坑**：容器 1.8G 内存必须 `-m:1 -p:RunAnalyzers=false -p:EnableNETAnalyzers=false -p:UseSharedCompilation=false`（否则 Roslyn OOM）；无 libicu 必须 `DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1`。

**⚠️ 账号冲突**：同一 MC 账号不能被两个实例同时登录（`duplicate_login` 顶号）。

## 系统要求

| 组件 | 最低要求 |
|---|---|
| 操作系统 | Ubuntu 20.04+ / Debian 11+ |
| Python | 3.11+ |
| Node.js | 20+（仅构建时） |
| .NET / Mono | 取决于 MCC 构建产物 |
| 磁盘 | 建议 10 GB+（MCC 程序 + 实例数据 + 日志） |
| 内存 | 建议 2 GB+（含 10-20 个 MCC 实例） |

## 快速部署 (Docker Compose)

```yaml
# docker-compose.yml
services:
  vmtools:
    build: .
    ports:
      - "8080:8080"
      - "33333-33352:33333-33352"  # MCC MCP 端口范围
    environment:
      - VMT_SERVER__DATABASE_URL=sqlite:///data/vmtools.db
      - VMT_SERVER__SECRET_KEY=change-me-to-random-64-chars
      - VMT_SERVER__JWT_ALGORITHM=HS256
      - VMT_MCC__INSTANCE_ROOT=/opt/vmtools/mcc-instances
      - VMT_MCC__BINARY_PATH=/opt/vmtools/mcc-runtime/MinecraftClient.dll
      - VMT_MCC__LAUNCH_COMMAND=dotnet,MinecraftClient.dll
      - VMT_MCC__INSTANCE_START_PORT=33333
      - VMT_MCC__INSTANCE_END_PORT=33352
      - VMT_MCC__MAX_INSTANCES=20
      - VMT_MCC__LOG_RETENTION_DAYS=14
      - VMT_SERVER__DEBUG=false
    volumes:
      - vmtools-data:/data
      - /opt/vmtools/mcc-runtime:/opt/vmtools/mcc-runtime:ro
      - /opt/vmtools/mcc-instances:/opt/vmtools/mcc-instances
    restart: unless-stopped

volumes:
  vmtools-data:
```

```bash
# 构建并启动
docker compose up -d --build

# 查看日志
docker compose logs -f vmtools
```

## 环境变量说明

### 服务配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VMT_SERVER__DATABASE_URL` | `sqlite:///vmtools-next.db` | SQLite 数据库路径 |
| `VMT_SERVER__SECRET_KEY` | *(必须设置)* | JWT 签名密钥，至少 32 字符 |
| `VMT_SERVER__JWT_ALGORITHM` | `HS256` | JWT 算法 |
| `VMT_SERVER__DEBUG` | `false` | 开发调试模式 |
| `VMT_SERVER__PORT` | `8080` | HTTP 服务端口 |

### MCC 运行配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VMT_MCC__INSTANCE_ROOT` | `/opt/vmtools/mcc-instances` | 实例根目录 |
| `VMT_MCC__BINARY_PATH` | `/opt/vmtools/mcc-runtime/MinecraftClient.exe` | MCC 程序路径 |
| `VMT_MCC__LAUNCH_COMMAND` | *(留空则自动检测)* | 自定义启动命令，逗号分隔，如 `dotnet,MinecraftClient.dll` |
| `VMT_MCC__INSTANCE_START_PORT` | `33333` | MCP 起始端口 |
| `VMT_MCC__INSTANCE_END_PORT` | `33352` | MCP 结束端口 |
| `VMT_MCC__MAX_INSTANCES` | `20` | 最大实例数 |
| `VMT_MCC__LOG_RETENTION_DAYS` | `14` | 终端日志保留天数 |
| `VMT_MCC__MCP_AUTH_TOKEN_ENV` | `MCC_MCP_AUTH_TOKEN` | MCP 认证 Token 环境变量名 |

## 实例目录结构

```
/opt/vmtools/mcc-instances/
├── bot-alice/                  # 实例 slug
│   ├── MinecraftClient.ini     # MCC 配置文件
│   ├── MinecraftClient.exe     # 或符号链接
│   ├── logs/                   # MCC 运行时日志
│   ├── scripts/                # 用户脚本
│   └── ...                     # 其他 MCC 生成文件
├── bot-bob/
└── bot-charlie/
```

每个实例目录由系统自动创建，包含默认 `MinecraftClient.ini` 模板和符号链接（或复制）的 MCC 程序。

`MinecraftClient.ini` 通过 Web UI 的“账号配置”表单或“文件管理”在线编辑。

## 认证方式配置

| 方式 | INI 设置 |
|---|---|
| 离线模式 | `login=<用户名>`, `password=-` |
| Microsoft | `login=<邮箱>`, `password=<密码>`, `method=microsoft` |
| Mojang | `login=<邮箱>`, `password=<密码>`, `method=mojang` |
| Yggdrasil/皮肤站 | `login=<邮箱>`, `password=<密码>`, `serverip=<认证服务器>` |
| 自定义 | `login=<用户名>`, `serverip=<自定义地址>` |

账号模板（Account Profiles）可以在 Web UI 中预制并复用到多个实例。

## Web UI 功能入口

| 路由 | 功能 |
|---|---|
| `/mcc/instances` | 实例总览（卡片列表） |
| `/mcc/instances/:id/terminal` | 全屏 Web Terminal |
| `/mcc/instances/:id/files` | 全屏文件管理器 |

## 日常运维

### 监控

- Web UI 仪表盘展示 Bot 在线数、任务状态。
- 系统监控页 (`/monitor`) 展示 CPU、内存、请求状态。
- Socket.IO 实时推送 MCC 终端日志、实例状态变更。

### 日志

- 终端日志：每个实例最多保留 500 行内存缓冲 + 14 天数据库记录。
- 操作审计日志记录所有启动/停止/配置/文件/终端操作。
- 审计日志可通过 API 查询：`GET /api/mcc/audit-logs`。

### 备份

推荐备份以下内容：
```bash
# SQLite 数据库
cp data/vmtools.db data/vmtools.db.$(date +%Y%m%d)

# MCC 实例目录
tar -czf instances-$(date +%Y%m%d).tar.gz /opt/vmtools/mcc-instances/
```

### 故障恢复

- 后端重启会自动清理过期 PID，并尝试恢复 `desired_state=running` 的实例。
- MCC 进程异常退出后状态自动变为 `stopped`/`crashed`，可在 Web UI 重新启动。
- 端口冲突时创建实例会返回明确错误，停止冲突实例后重试即可。

## 安全建议

1. **修改默认密钥**：部署前必须设置 `VMT_SERVER__SECRET_KEY` 为随机字符串。
2. **强制 HTTPS**：生产环境前端 Nginx + Let's Encrypt TLS。
3. **防火墙**：MCC MCP 端口（33333-33352）只需对 localhost 开放。
4. **密码脱敏**：API 返回的配置文件会自动隐藏密码/token/secret。
5. **路径隔离**：文件管理器限制在实例目录内，拒绝 `../` 路径穿越。
6. **Socket.IO 鉴权**：所有 WebSocket 连接需要 JWT 认证。
7. **审计不可关闭**：所有关键操作写入审计日志。

## 常见问题

**Q: 启动 MCC 失败，日志显示 "file not found"？**
A: 检查 `VMT_MCC__BINARY_PATH` 路径是否正确，以及容器内是否有对应的运行时（.NET/Mono）。可使用 `VMT_MCC__LAUNCH_COMMAND` 自定义启动命令。

**Q: 多个实例同时启动 MCP 端口冲突？**
A: 系统自动分配端口，通过 `VMT_MCC__INSTANCE_START_PORT` 和 `VMT_MCC__INSTANCE_END_PORT` 设置范围，确保端口数量 >= 最大实例数。

**Q: 日志占满磁盘？**
A: 调整 `VMT_MCC__LOG_RETENTION_DAYS`（默认 14 天），或定期清理 `mcc_terminal_logs` 表。

**Q: 如何迁移到 PostgreSQL？**
A: 修改 `VMT_SERVER__DATABASE_URL` 为 PostgreSQL 连接串，使用 Alembic 生成迁移脚本后部署。

**Q: 忘记管理员密码？**
A: 首次启动自动创建 `admin` 用户（密码从 `VMT_ADMIN_PASSWORD` 环境变量读取）。后续可通过 API 或直接修改 `users` 表重置。
