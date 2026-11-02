# VMTools-Web

基于无头 **Minecraft Console Client (MCC)** 与 **Mineflayer** 双引擎的 Minecraft Bot 一站式远程管理平台。无需 SSH 登录服务器，在浏览器中即可完成多实例管理、Web 终端、自动化物流、多机器人协作建造等全部操作。

> 本平台是 VMTools v3 项目的服务端部分，运行在 Linux 服务器上（Ubuntu 20.04+ / Debian 11+）。
>
> 仓库：[GitHub `venuyu045/VMTools-Web`](https://github.com/venuyu045/VMTools-Web) ｜ 镜像：[Gitee `de-spider/vmtools-next`](https://gitee.com/de-spider/vmtools-next)
> （历史名称 `vmtools-next` 保留在服务名、部署目录与 Gitee 镜像上，不影响使用。）

## 主要特性

- **双引擎多实例管理**：MCC（MCP 协议）与 Mineflayer（WebSocket Bridge）两套 bot 引擎，独立目录/端口/日志，支持同时管理最多 20 个实例；Web 终端（xterm.js + PTY）、在线文件管理、账号配置。
- **自动化物流**：仓库扫描（含潜影盒递归、Servux 直读容器）、路径点、取放点与任务模板配置，Logistics Runner 自动化取放任务。
- **自动化建造**：Litematica 投影解析、材料清单计算、自动分层建造、缺失方块验证与补放。
- **地图画多 Bot 协作**：大型像素画列式/分区两种建造模式，多机器人排内并行、减员容错、断点续建、进度落盘。
- **战斗与挂机**：可配置攻击半径（最高 64 格）、守护/挂机模式、玩家预警与追踪。
- **任务中心**：定时任务调度、任务模板、批量执行。
- **BlueMap 监控**：轮询地图 API，精确检测机器人与玩家上下线状态。
- **即时通知**：实例上下线、崩溃、自动重连事件通过 QQ 机器人实时推送。
- **组织官网**：内置「理想乡」组织主页（介绍 / 成员 / 基地 / 相册），成员头像自动使用其 MC ID 的正版皮肤，自带管理后台；入口在**顶栏「理想乡官网」**。
- **时装系统（VMFashion）**：配套的纯客户端时装 mod（[VMFashion-mod](https://github.com/venuyu045/VMFashion-mod)）的云端衣柜与**时装包级下发** —— 网页换装、游戏内实时生效，别人穿的时装没装也能自动下载（sha256 校验 + Ed25519 验签 + 安全解压）；站点管理员另有独立的**时装管理端**（目录 / 资产 / 时装包）。入口在**顶栏「时装中心」**：点进去是独立子应用，侧边栏只保留时装内容（时装商店 / 我的衣柜 / 时装管理）。详见 [时装系统](#时装系统vmfashion)。
- **插件系统**：插件启停与在线配置管理。
- **安全**：JWT 认证、角色权限（site_admin / admin / user）、操作审计日志、登录限流。

## 技术栈

| 层级 | 技术选型 |
| :--- | :--- |
| **后端** | Python 3.11+ · FastAPI · SQLAlchemy · SQLite（生产可换 PostgreSQL） |
| **Bot 引擎（Node）** | Node.js 20+ · Mineflayer · 自研 baritone-path 寻路（A* + 分块） |
| **实时通信** | python-socketio (WebSocket) |
| **前端** | Vue 3 · TypeScript · Vite · Element Plus |
| **终端** | @xterm/xterm + PTY |
| **图表 / 3D** | ECharts · Three.js |
| **部署** | Docker · Docker Compose · Systemd · Nginx |

## 快速开始（Docker，推荐）

```bash
# 1. 克隆项目（GitHub 主仓库；国内可用 Gitee 镜像）
git clone https://github.com/venuyu045/VMTools-Web.git
# 或：git clone https://gitee.com/de-spider/vmtools-next.git
cd VMTools-Web

# 2. 准备环境变量（VMT_SECRET_KEY 可留占位值，start.sh 会自动生成）
cp .env.example .env
vim .env

# 3. 一键启动（或直接 docker compose up -d --build）
chmod +x start.sh
./start.sh
```

访问 `http://<服务器IP>:8080` 即可使用。

> 🔑 **`start.sh` 会自动生成密钥与口令并回写 `.env`**：首次运行若无 `.env` 会先 `cp .env.example .env`；随后
> - `VMT_SECRET_KEY` **恰好等于占位串** `change-me-to-a-random-string-at-least-32-chars` → 自动 `openssl rand -hex 32` 生成并 `sed` 回写 `.env`；
> - `SITE_ADMIN_PASSWORD` **为空或仍为 `change-me`** → 自动 `openssl rand -base64 18` 生成并回写 `.env`，同时在启动日志里打印一次（请立即登录后修改）。
>
> ⚠️ 注意 `VMT_SECRET_KEY` 的兜底只匹配那一个占位串：若你把它**留空**（不写这一行），脚本不会生成，最终由后端 `load_config()` 的强校验**拒绝启动**并提示用 `openssl rand -hex 32` 生成 —— 这是有意的失败而非静默降级。

### 环境变量说明（`.env`）

| 变量 | 描述 | 默认值 |
| :--- | :--- | :--- |
| `VMT_SECRET_KEY` | **必须修改。** JWT 签名密钥（≥32 位随机串）。**缺失 / 仍是默认值 / 长度 <32 / 占位值 → 服务直接拒绝启动**（`RuntimeError`，不是告警） | - |
| `VMT_PORT` | Web 服务端口 | 8080 |
| `SITE_ADMIN_GAME_ID` | 初始管理员游戏 ID | admin |
| `SITE_ADMIN_PASSWORD` | 初始管理员密码。**留空 = 后端随机生成**（`uuid4().hex[:12]`）并写入 `config/.site_admin_password`（`chmod 600`），日志只提示路径；`debug` 模式才打印明文。⚠️ **切勿留 `change-me`** | - |
| `MCC_RUNTIME_PATH` | MCC 程序目录（存放 MinecraftClient.exe/dll），挂载为只读 | /opt/vmtools/mcc-runtime |
| `MCC_INSTANCE_ROOT` | MCC 实例数据目录 | /opt/vmtools/mcc-instances |
| `MCC_PORT_RANGE` | MCP 协议端口分配范围 | 33333-33352 |

> ⚠️ **命名铁律：后端配置项必须写成 `VMT_<SECTION>__<KEY>`，即「双下划线」分隔嵌套层级。**
> - ✅ 正确：`VMT_SERVER__HOST` / `VMT_SERVER__PORT` / `VMT_SERVER__DATABASE_URL` / `VMT_SERVER__SECRET_KEY` / `VMT_MCC__MAX_INSTANCES`
> - ❌ 错误：`VMT_SERVER_HOST`（单下划线）→ **静默失效**：不报错、也不生效，服务仍按 `config.yaml` 默认值启动
>
> 原因：`config.py` 的 `_apply_env_overrides` 用 `.split("__")` 切分路径，且要求层级数 ≥2；单下划线切不出层级，整条被 `continue` 跳过。**这是 2026-10-20 审查的严重项 S4**，原 systemd unit 三行全中招。
> 说明：`VMT_SECRET_KEY` / `VMT_PORT` / `SITE_ADMIN_*` / `MCC_*` 是**顶层**变量（只有一段），由 `docker-compose.yml` 显式映射成 `VMT_SERVER__SECRET_KEY` 等嵌套变量后注入容器，所以它们本身不含 `__` 是正常的。

## 非 Docker 部署（裸机 / systemd）

```bash
# 后端（入口 backend/main.py，从 backend/ 目录启动）
cd backend
pip install -e .
uvicorn main:app --host 0.0.0.0 --port 8080

# 前端构建产物由后端托管，如改过前端需要重新构建
cd ../frontend && npm install && npm run build
```

裸机部署的配置走 YAML：复制 `backend/config/config.yaml.example` 为 `backend/config/config.yaml` 并修改 `server.secret_key`（缺失或为默认值时服务拒绝启动）；可用 `config.dev.yaml` / `config.prod.yaml` 区分环境（`VMT_ENV` 选择），`VMT_` 前缀环境变量优先级最高（如 `VMT_SERVER__SECRET_KEY`）。

Systemd / Nginx / 一键安装脚本见 `backend/deploy/`（`systemd/`、`nginx/`、`install.sh`）。

> **⚠️ SQLite 路径警告**：默认 `database_url: "sqlite:///vmtools-next.db"` 是**相对于启动 uvicorn 时的当前目录**的。请固定用一个启动目录，或生产环境直接在 `config.yaml` 里把 `server.database_url` 改为绝对路径，否则换个目录启动就会新建空库，看起来像配置和实例数据"丢失"。详见 `docs/server-ops-guide.md`。

## 开发指南

```bash
# 后端
cd backend
pip install -e ".[dev]"
pytest tests/ -q                    # 运行测试
python -m vmtools_next.main         # 开发服务器（热重载）

# 前端
cd frontend
npm run dev                         # 开发服务器
npm run build                       # 生产构建 → 输出到 backend/static/（随 git 提交）

# Mineflayer bot 侧
cd mineflayer-bots
npm install
```

> 前端构建产物直接进 git（`backend/static/`），改完前端必须重新 `npm run build` 后提交，服务器拉取即生效。

## 时装系统（VMFashion）

给纯客户端时装 mod [VMFashion](https://github.com/venuyu045/VMFashion-mod) 提供**云端衣柜**与**时装包级下发**。
服务器（Minecraft 服务端）不需要装任何东西；玩家装 mod 后填上本站的 `apiBaseUrl` 即可。

### 能力

| 能力 | 说明 |
| :--- | :--- |
| **时装中心** | 顶栏「时装中心」入口 → 独立子应用，侧边栏只保留时装内容：**时装商店**（浏览全部目录、加入衣柜）、**我的衣柜**（收藏管理 / 挑选穿戴）、**时装管理**（仅 site_admin）；主站侧边栏不再出现时装入口，底部「返回主站」一键回主站 |
| **账号绑定** | 游戏内 `/vmfashion bind` 得到 6 位绑定码 → 本站「时装绑定」页输入 → 一个网站账号可绑多个 MC 账号 |
| **双路认证** | 正版走 `sessionserver.mojang.com`，皮肤站走 Yggdrasil（**只信任白名单内的地址**，防 SSRF）；后端用 `hasJoined` 反查并**比对 UUID**，不做密码登录 |
| **时装商店** | `/fashion/store`：目录网格（缩略图 / 槽位 / 所属时装包）、搜索、按槽位筛选、分页、详情大图；卡片「加入衣柜」把时装收进自己的衣柜（已在衣柜则变为「去穿戴 →」深链到衣柜并定位该件时装） |
| **网页衣柜** | `/fashion/wardrobe`：**只显示自己在商店里加入衣柜的时装**（收藏制，不再展示全站目录）；按槽位（披风/翅膀/头饰/背饰/宠物/光环）挑选、调缩放、保存；支持单件移除与**批量管理（勾选 / 全选 / 删除选中，超过后端 500 上限自动分片提交）**；目录里已删除/停用的条目仍显示并标「已下架」（可删不可穿）；进游戏自动生效，游戏内换装也会实时回传 |
| **时装管理端** | `/fashion/admin`（仅 site_admin）：目录管理（增删改查 / 启用开关 / 排序 / 资产缺失徽标）、资产管理（上传 + 一键填入目录）、时装包（上传 zip 发布 / 下架，409 `pack_in_use` 有专门引导）；上传带进度 / 网速 / ETA 与大小预检 |
| **批量解析** | 进服时 `POST /loadouts/resolve` 一次最多 200 个 UUID，只回 `assetKey` 级元数据（热路径，5 秒缓存） |
| **时装包下发** | `GET /packs` 清单 + `GET /packs/{id}/{version}/download` 整包 zip：200 直出、`Content-Length` 精确、**不重定向**、可选 Ed25519 签名；客户端按 sha256 校验后安全解压 |
| **上传加固** | 服务端先按客户端同一套规则校验 zip（条目数/大小/压缩比、拒绝 `..`、符号链接、加密、ZIP64、可执行文件、Windows 保留名，`pack.json.id` 必须与 packId 一致），坏包进不了清单 |

### 页面与接口

- 前端页面（均在**时装中心** `/fashion/*` 子应用内，需登录；路由 meta `fashionCenter` 决定侧边栏切换）：
  `/fashion/store`（时装商店）、`/fashion/wardrobe`（我的衣柜）、`/fashion/bind`（时装绑定）；
  `/fashion/admin`（时装管理端）—— 仅 site_admin，普通 admin / user 在侧边栏看不到入口，直连也会被后端 403 拦下
- 顶栏入口（`AppHeader`）：**理想乡官网** `/lixiangxiang`（访客可见）、**时装中心** `/fashion/store`（登录后可见）
- 后端路由：`backend/src/vmtools_next/api/routers/fashion.py`，前缀 **`/api/fashion`**
  - 公开：`/auth/challenge`、`/auth/verify`、`/packs`、`/packs/{id}/{version}/download`、`/assets/{assetKey}`
  - mod（Bearer token）：`/loadout`（PUT）、`/loadout/me`、`/loadouts/resolve`、`/catalog`、`/device/*`、`/wardrobe/me`（游戏内衣柜按收藏过滤）
  - 网站（网页 JWT）：`/wardrobe`（GET/POST/DELETE）、`/wardrobe/batch-delete`、`/identities/me`、`/device/approve`
  - 管理端（site_admin）：`/admin/catalog`、`/admin/assets`、`/admin/packs`（上下架）
- 完整契约（含每个端点的字段表、错误码、签名规范、curl 自测）：[`docs/fashion-api.md`](docs/fashion-api.md)

### 服务端配置

全部走 `VMT_FASHION__*` 环境变量（嵌套配置必须**双下划线**，见上文命名铁律），默认值即开箱可用：

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `VMT_FASHION__ENABLED` | `true` | 总开关（关掉后不挂载时装路由） |
| `VMT_FASHION__YGGDRASIL_BASES` | `https://littleskin.cn/api/yggdrasil` | 皮肤站白名单，逗号分隔；**上线新皮肤站必须加进来** |
| `VMT_FASHION__ASSET_DIR` | `data/fashion-assets` | 资产目录（相对仓库根），生产请挂持久卷 |
| `VMT_FASHION__TOKEN_TTL_SECONDS` | `86400` | 发给 mod 的 token 有效期（无 refresh，过期后 mod 重新认证） |
| `VMT_FASHION__RESOLVE_MAX_UUIDS` | `200` | 单次批量解析上限 |
| `VMT_FASHION__PACK_SIGNING_KEY_B64` | 空 | 时装包 Ed25519 私钥（base64）。空 = 清单不带签名 |
| `VMT_FASHION__PACK_SIGNING_KEY_ID` | `vmtools-official` | 签名 keyId（与客户端 `trusted_keys.json` 对应，用于轮换） |

### 数据与迁移

- 表：`fashion_identity`（已验证的 MC 身份）、`fashion_catalog`（时装目录）、`fashion_loadout`（穿戴，`(identity_id, slot)` 唯一）、`fashion_asset`（资产，按 sha256 去重；**时装包清单从 `.zip` 资产行派生，无需新表**）、`fashion_wardrobe`（我的衣柜收藏，`(user_id, cosmetic_id)` 唯一；`cosmetic_id` 故意不做外键，删目录不清空玩家衣柜）
- 迁移：`backend/alembic/versions/20261028_fashion.py`、`20261101_fashion_wardrobe.py`（`alembic upgrade head`；`init_db()` 的 `create_all` 也会建表）
- 资产目录：`data/fashion-assets/`（见该目录下 README）

### 发布时装包（管理员）

```bash
BASE=https://your-host/api/fashion
TOKEN=<site_admin 的登录 token>

# 1) 发布一个时装包（zip 根目录必须有 pack.json，且 pack.json.id == pack_id）
curl -s -X POST $BASE/admin/packs -H "Authorization: Bearer $TOKEN" \
  -F "file=@wings-1.2.0.zip" -F "pack_id=wings" -F "version=1.2.0"

# 2) 登记目录条目（cosmeticId 用全局 ID <packId>:<localId>，与 mod 上报的一致）
curl -s -X POST $BASE/admin/catalog -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"cosmeticId":"wings:angel","packId":"wings","name":"天使之翼","category":"wings","sortOrder":10}'

# 3) 验证清单与下载（200 直出、Content-Length == bytes、无 Location）
curl -s "$BASE/packs" | jq '.packs[0]'
curl -sI "$BASE/packs/wings/1.2.0/download" | grep -iE 'HTTP/|content-length|content-type|location'

# 4) 下架（只从清单移除，玩家本地已下载的包不受影响）
curl -s -X DELETE $BASE/admin/packs/wings/1.2.0 -H "Authorization: Bearer $TOKEN"
```

### 注意事项

1. **多 worker 部署**：challenge / 设备码 / 限流都在**单进程内存**里，`--workers > 1` 时 mod 的
   challenge→verify 可能落到不同进程 → `challenge_not_found`。请用单 worker 或 nginx `ip_hash` 粘性会话。
2. **反向代理**：后端已开 `proxy_headers` + `forwarded_allow_ips="*"`，challenge 的来源 IP 绑定依赖它；
   ⚠️ 因此后端端口**只能**对反代网段开放，否则任何人都能伪造 `X-Forwarded-For`。
3. **nginx 上传上限**：资产 ≤64MB、时装包 ≤128MB，nginx 默认 `client_max_body_size 50m` 会先 413。
4. **签名与严格模式**：`cryptography` 已随部署依赖安装（`pip install -e ".[signing]"`，Docker 为 `.[dev,signing]`）；
   配置 `VMT_FASHION__PACK_SIGNING_KEY_B64` 后清单即带 `signature`。**在确认签名生效之前不要**让玩家在客户端开
   `requireSignature`，否则会拒绝全部服务端包。
5. **测试**：`pytest tests/test_fashion_*.py -q`（**250 条用例 + 42 subtests**，覆盖认证 / 契约 / 包级下发 /
   zip 安全校验 / Ed25519 真签真验 / TestClient 管理端 401·403 / HEAD 下载头 / 衣柜收藏制与 `/wardrobe/me`）。

## 项目结构

```
VMTools-Web/
├── backend/                      # Python FastAPI 后端
│   ├── src/vmtools_next/
│   │   ├── api/routers/          # REST API 路由（auth/mcc_instances/logistics/build_map_art/lixiangxiang/fashion…）
│   │   ├── core/                 # 核心业务逻辑
│   │   │   ├── mcc_process_manager.py     # MCC 进程守护与终端管理
│   │   │   ├── mineflayer_process_manager.py # Mineflayer 进程守护
│   │   │   ├── logistics_runner.py        # 物流自动化引擎
│   │   │   ├── build_state_machine.py     # 建造状态机
│   │   │   ├── map_art_coordinator.py     # 地图画单 Bot 协调
│   │   │   ├── multi_bot_coordinator.py   # 地图画多 Bot 列式协作
│   │   │   ├── warehouse_scanner.py       # 仓库扫描器（含 Servux 直读）
│   │   │   ├── bluemap_monitor.py         # BlueMap 在线状态监控
│   │   │   └── task_engine.py             # 任务中心引擎
│   │   ├── adapters/             # 适配器层（mcc / mineflayer / qqbot / litematica）
│   │   └── data/                 # SQLAlchemy 数据模型
│   ├── config/                   # YAML 配置（config.yaml 为本地配置，gitignored）
│   ├── deploy/                   # 部署脚本（install.sh / nginx / systemd / Dockerfile）
│   ├── static/                   # 前端构建产物（含 item-icons 1231 张）
│   └── tests/                    # 单元测试
├── frontend/                     # Vue 3 + TypeScript 前端
│   └── src/{views,components,api}/
├── mineflayer-bots/              # Mineflayer bot 进程（Node.js）
│   ├── bot_main.js               # 进程入口（WS Bridge 连接平台）
│   ├── handlers/                 # 行为模块（移动/方块/背包/战斗/睡觉/Servux…）
│   └── baritone-path/            # 自研寻路（A* + executor + world 分类）
├── docs/                         # 运维与部署文档
├── .env.example                  # 环境变量模板
├── docker-compose.yml            # Docker 编排
└── start.sh                      # 一键启动脚本
```

## 相关文档

- [服务器运维手册](docs/server-ops-guide.md)
- [MCC 部署指南](docs/mcc-deployment-guide.md)

## 许可证

本项目遵循 **MIT** 许可证开源，详见 [LICENSE](LICENSE) 文件。
