# 芒果块（Mango Block）代币购买时装 —— 前后端契约

> 版本：v1.0（2026-11-05）
> 范围：`vmtools-next` 时装中心网页前端 + FastAPI 后端
> 关联：`docs/fashion-api.md`（mod 契约，本次**不改** mod 侧任何既有字段/端点语义）

## 1. 目标与名词

- **芒果块**：站内虚拟代币，单位「块」，**整数**，1 芒果块 = 1。不允许小数、不允许负数余额。
- 时装不再一律免费「加入衣柜」：目录里的每件时装可以标价（单位：芒果块），玩家需用芒果块**购买**后才能获得（自动加入「我的衣柜」），随后网页衣柜 / 游戏内 mod 才能穿戴。
- **充值通道本期只保留接口，不实现支付**：所有充值入口明确返回「暂未开放」，接口形状先定死，后续接支付只改实现、不改前端契约。
- 本期唯一的入金途径是**管理端发放/调整**（`POST /admin/wallet/adjust`），用于运营与联调。

### 1.1 兼容性与不破坏既有行为

- 目录条目默认 `priceMango = 0`（免费），`purchasable = true`。**不设价的时装行为与上线前完全一致**：仍可免费「加入衣柜」。
- 上线时已存在于 `fashion_wardrobe` 的历史收藏，一律视为**已拥有**（`source = legacy_wardrobe`，价格 0），不会追溯收费、不会突然不能穿。
- mod 侧接口 `GET /catalog`、`GET /wardrobe/me`、`PUT /loadout`、`POST /loadouts/resolve` 的**响应字段保持原样**（新字段一律可选、有默认值，老客户端忽略未知字段）。
- `GET /catalog` 不变（它带进程内缓存，且是 mod 契约）。商店页改用新的 `GET /store/items` 聚合接口。

## 2. 数据模型（新增 3 表 + 目录加 2 列）

| 表 / 列 | 说明 |
| --- | --- |
| `fashion_catalog.price_mango` INTEGER NOT NULL DEFAULT 0 | 价格（芒果块），0 = 免费 |
| `fashion_catalog.purchasable` BOOLEAN NOT NULL DEFAULT 1 | false = 商店不售卖（仅管理端发放），仍可穿戴 |
| `fashion_wallet` | `user_id` PK/FK `users.id`；`balance`、`total_earned`、`total_spent`、`updated_at` |
| `fashion_ownership` | `id` PK；`user_id` + `cosmetic_id` **唯一约束**；`price_paid`、`source`、`order_no`、`acquired_at` |
| `fashion_mango_transaction` | `id` PK；`user_id`、`delta`（正入负出）、`balance_after`、`type`、`cosmetic_id`、`note`、`idempotency_key`（唯一，可空）、`operator_user_id`、`created_at` |

`source` 枚举：`purchase` / `free_claim` / `legacy_wardrobe` / `admin_grant` / `migration`
`type` 枚举：`purchase` / `refund` / `admin_adjust` / `recharge` / `grant`

**获得时装的链路**：购买 / 免费领取 / 管理端发放 → 写 `fashion_ownership` → 自动写 `fashion_wardrobe`（规范 ID） → mod 的 `GET /wardrobe/me` 可见 → 可穿戴。

**防绕过**：`POST /wardrobe`（加入衣柜）对「`priceMango > 0` 且未拥有」的时装返回 403 `purchase_required`；已拥有或免费的照旧。`PUT /loadout` 对「付费且未拥有且不在衣柜」的条目 skip 并写入 `warnings`（`code = not_owned`），不整条失败。

**所有权键（重要安全约定）**：`fashion_ownership.cosmetic_id` 与 `fashion_wardrobe.cosmetic_id`
一律存**规范化 ID**（`_owner_key()`：经目录快照把任意形态映射到目录条目的 canonical ID）。
判断「是否已拥有 / 是否已在衣柜」时用规范化键**精确相等**，绝不做「裸 `localId`
别名集合求交」—— 目录里可能同时存在简单 ID 条目 `crown`（1 芒果块）与
`packX:crown` / `packY:crown`（各 500 芒果块），求交会让买下便宜的 `crown` 后把
所有同名全局 ID 都判成已拥有（计费绕过）。历史衣柜回填时同样先规范化再写入，
`source = legacy_wardrobe`。

金额列（`balance` / `total_earned` / `total_spent` / `price_paid` / `delta` /
`balance_after` / `price_mango`）全部用 `BigInteger`，避免 PostgreSQL 的 32 位
`INTEGER` 在累计发放/消费后溢出；布尔默认值用 `sa.true()`（方言无关，PG 编译为
`true`、SQLite 为 `1`）。

## 3. 后端端点（前缀 `/api/fashion`）

鉴权：除标注外均为**网页登录 JWT**（`get_current_user`），管理端为 `require_site_admin`。
错误响应统一：`{"detail": {"code": "...", "message": "中文提示"}}`。

### 3.1 钱包

#### `GET /wallet` → `WalletOut`
首次访问自动创建余额 0 的钱包。
```json
{
  "balance": 120,
  "currency": "芒果块",
  "totalEarned": 200,
  "totalSpent": 80,
  "updatedAt": "2026-11-05T12:00:00+00:00"
}
```

#### `GET /wallet/transactions?limit=20&offset=0` → `WalletTransactionListResponse`
```json
{
  "items": [
    {
      "id": "uuid", "type": "purchase", "delta": -30,
      "balanceAfter": 90, "amountAbs": 30,
      "cosmeticId": "pack1:cape_dragon", "cosmeticName": "龙纹披风",
      "note": "购买时装", "createdAt": "2026-11-05T12:00:00+00:00"
    }
  ],
  "total": 1, "limit": 20, "offset": 0
}
```
`limit` 范围 1..100，默认 20；`offset >= 0`。按 `createdAt` 倒序。

#### `GET /wallet/recharge/options` → `RechargeOptionsResponse`（**预留**）
```json
{
  "available": false,
  "currency": "芒果块",
  "channels": [],
  "notice": "充值通道即将开放，目前可联系管理员发放芒果块"
}
```

#### `POST /wallet/recharge` → HTTP **501**（**预留，本期不实现支付**）
请求体：
```json
{ "amount": 100, "channel": "", "clientRequestId": "可选幂等键" }
```
响应：`501 {"detail":{"code":"recharge_not_available","message":"充值通道即将开放，敬请期待"}}`
> 后续接入支付时：校验 `amount`、创建订单、支付回调成功后调用 `fashion_service.credit_wallet(...)` 入账并写 `type=recharge` 流水，把 `available` 置 true、`channels` 填充即可，前端契约不变。

### 3.2 商店

#### `GET /store/items?category=&pack_id=` → `StoreItemsResponse`
商店页**一次拿全**（目录 + 价格 + 拥有状态 + 余额 + 衣柜状态 + 充值开关），避免 N+1。
```json
{
  "currency": "芒果块",
  "wallet": { "balance": 120, "currency": "芒果块", "totalEarned": 200, "totalSpent": 80, "updatedAt": "..." },
  "rechargeAvailable": false,
  "items": [
    {
      "cosmeticId": "pack1:cape_dragon", "name": "龙纹披风", "category": "cape",
      "assetKey": "packs/pack1/cape_dragon.png", "sha256": "...", "bytes": 12345,
      "packId": "pack1", "sortOrder": 0,
      "priceMango": 30, "purchasable": true,
      "owned": false, "inWardrobe": false
    }
  ]
}
```
只返回 `enabled = true` 的目录条目，按 `sortOrder, cosmeticId` 升序。

#### `POST /store/purchase` → `PurchaseResponse`
请求：`{"cosmeticId":"pack1:cape_dragon","clientRequestId":"可选，<=64字符"}`

成功（含幂等重复购买）：
```json
{
  "ok": true,
  "purchased": true,
  "alreadyOwned": false,
  "free": false,
  "priceMango": 30,
  "balance": 90,
  "cosmeticId": "pack1:cape_dragon",
  "item": { "...StoreItem 形状..." },
  "transactionId": "uuid 或空串"
}
```
- `purchased=false` + `alreadyOwned=true`：之前已拥有，**不再扣款**，幂等返回 200，并确保该时装在衣柜里。
- `free=true`：价格 0 的免费领取，不扣款、不写负流水（`transactionId` 为空串）。
- 购买成功后**自动加入衣柜**（幂等，规范 ID）。

错误码：
| code | HTTP | 说明 |
| --- | --- | --- |
| `invalid_cosmetic_id` | 400 | 空 / 超长 / 非法字符 |
| `cosmetic_not_found` | 404 | 目录里没有或已下架 |
| `not_purchasable` | 409 | 管理端设了「不可售卖」且**尚未拥有**（已拥有时仍幂等 200，见下） |
| `insufficient_mango` | 402 | 余额不足，message 里带还差多少（并发下按最新余额计算） |
| `invalid_client_request_id` | 400 | 幂等键超长 / 含非法字符 |
| `idempotency_key_conflict` | 409 | 同一 `clientRequestId` 被用于购买另一件时装 |
| `purchase_rate_limited` | 429 | 购买接口限流（20 次 / 60s / 用户），带 `Retry-After: 60` |
| `conflict` | 409 | 并发写入冲突，重试即可 |

> 请求体字段类型异常（`priceMango` / `delta` 传字符串、bool、小数等）由 FastAPI
> `StrictInt` 拦成标准 **422**（`detail` 是数组），前端 `fashionErrorMessage()` 已有
> 中文兜底；业务层只处理「整数但越界」的情况（400 `invalid_price` / `invalid_amount`）。

**幂等与并发**：
- `fashion_ownership` 的 `(user_id, cosmetic_id)` 唯一约束兜住重复购买；
- 扣款用条件 UPDATE `... WHERE balance >= price`（原子，防并发超扣）。并发买**同一件**
  且余额只够一次时，输家不会拿到 402，而是按「已拥有」幂等返回 200；买**不同件**时
  输家才是 402 `insufficient_mango`；
- `clientRequestId` 落库时带 `user_id` 前缀（全局唯一索引 → 用户内唯一），**不同用户
  可以使用同一个 key 互不干扰**；
- 带 `clientRequestId` 重复提交同一 key 返回**与首次交易一致**的幂等响应：
  `purchased=false, alreadyOwned=true`，且 `priceMango` / `free` / `balance` /
  `transactionId` 都从首次流水恢复（免费领取的重复响应 `free` 仍为 `true`，不会自相矛盾）；
- 所有「已拥有」判定都基于**规范化 cosmeticId 精确比较**（见 §2 所有权键），不使用
  `localId` 别名求交 —— 否则「便宜的简单 ID 条目」会把所有同名 `packX:localId` 的
  昂贵时装误判成已拥有，直接绕过计费。

#### `GET /store/orders?limit=20&offset=0` → `PurchaseOrderListResponse`
```json
{
  "items": [
    { "orderNo": "...", "cosmeticId": "...", "name": "龙纹披风", "category": "cape",
      "packId": "pack1", "pricePaid": 30, "source": "purchase",
      "acquiredAt": "...", "missing": false }
  ],
  "total": 1, "limit": 20, "offset": 0
}
```

### 3.3 管理端（site_admin）

#### `POST /admin/wallet/adjust`
请求：`{"userId":"...","delta":100,"note":"活动奖励"}`
响应：`{"ok":true,"userId":"...","balance":100,"delta":100,"transactionId":"..."}`
- `delta` 为**整数**，范围 ±1..1000000，不能为 0；扣减后余额不得为负（否则 400 `insufficient_mango`）。
- 写 `type=admin_adjust` 流水，记录 `operator_user_id`（审计）。
- 错误码：`invalid_amount`(400)、`user_not_found`(404)、`insufficient_mango`(400)。

#### `GET /admin/wallet/users?keyword=&limit=20&offset=0` → `AdminWalletUserListResponse`
```json
{
  "items": [
    { "userId": "...", "gameId": "...", "displayName": "...", "role": "user",
      "balance": 120, "totalEarned": 200, "totalSpent": 80 }
  ],
  "total": 1, "limit": 20, "offset": 0
}
```

#### 目录定价（既有端点扩展）
- `GET /admin/catalog`、`POST /admin/catalog`、`PUT /admin/catalog/{id}` 的条目新增两个字段：
  - `priceMango: number`（0 = 免费，范围 0..1_000_000）
  - `purchasable: boolean`
- `POST/PUT` 的 payload 支持部分更新，只传 `priceMango` 也能改价。
- 错误码：`invalid_price`(400)。

#### `POST /admin/wallet/grant-cosmetic`（补充：管理端直接发放时装）
请求：`{"userId":"...","cosmeticId":"...","note":"..."}` → 写 ownership + 自动加衣柜，**不扣币**。
用于补发 / 活动赠送 / 联调。错误码同购买。

### 3.4 既有端点的行为变更

| 端点 | 变更 |
| --- | --- |
| `POST /wardrobe` | 付费且未拥有 → 403 `purchase_required`（`message` 里带价格）；其余不变 |
| `PUT /loadout` | 付费且未拥有且不在衣柜 → 该条 skip，`warnings` 增加 `{code:"not_owned"}`；其余不变 |
| `GET /catalog` | **不变**（mod 契约） |
| `GET /wardrobe`、`GET /wardrobe/me` | **不变** |

## 4. 前端改动要求

### 4.1 `frontend/src/api/fashion.ts`
新增类型与 `fashionApi` 方法：`storeItems`、`purchase`、`orders`、`wallet`、`walletTransactions`、`rechargeOptions`、`recharge`、`adminAdjustWallet`、`adminWalletUsers`、`adminGrantCosmetic`；`AdminCatalogItem` / `CatalogUpsertPayload` 增 `priceMango` / `purchasable`。
所有错误提示统一走 `notifyFashionError` / `fashionErrorMessage`（402 要能显示后端中文 message）。

### 4.2 时装商店 `FashionStoreView.vue`
- 数据源从 `fashionApi.catalog()` 换成 `fashionApi.storeItems()`（一次拿余额 / 价格 / 拥有 / 衣柜状态）。
- 页头显示「🥭 芒果块余额 N」+「充值」按钮（弹窗内容来自 `rechargeOptions`，明确「即将开放」，并提供 `recharge` 占位调用）。
- 卡片与详情弹窗按状态渲染动作：
  - 免费（`priceMango===0`）：「加入衣柜」（原逻辑）
  - 付费 + 未拥有：「🥭 30 购买」按钮 → 二次确认弹窗（显示价格 / 购买后余额 / 余额不足提示）→ `purchase()`
  - 付费 + 已拥有：「已拥有 ✓」+「去穿戴」（若不在衣柜则点购买会自动补回衣柜）
  - 不可售卖（`purchasable===false`）：「暂不可购买」禁用态
- 购买成功后：更新余额、拥有集合、衣柜集合，弹成功提示；余额不足时给出「去充值」引导（占位）。
- 新增「购买记录」抽屉/弹窗（`orders`）+「收支明细」抽屉（`walletTransactions`）。
- 所有新增 UI **必须适配移动端**（≤768px）：不固定宽度、可滚动、按钮触控区 ≥44px、弹窗 `width="92vw"` 或 CSS 覆盖、不依赖 hover、不出现横向溢出。

### 4.3 时装管理 `FashionAdminView.vue`
- 目录表格新增「价格（芒果块）」「可购买」列，编辑表单新增对应输入（数字最小 0、上限 1_000_000，布尔开关）。
- 新增「芒果块管理」区块：用户搜索（`adminWalletUsers`）、余额展示、发放/扣减（`adminAdjustWallet`，带备注、二次确认）、发放时装（`adminGrantCosmetic`）。

### 4.4 侧边栏 `FashionSidebar.vue`
- 增加「芒果块」余额卡片（可选，读 `wallet()`），移动端折叠态也能看到余额数字。

### 4.5 预览窗口 bug 修复 + 移动端适配
按独立审查报告逐条修复（商店详情弹窗、衣柜页预览、管理端资产预览、绑定页预览等），重点：关闭/ESC/遮罩、状态残留、图片竞态、`el-image` 预览层级、滚动锁定、移动端弹窗尺寸与滚动、触控区、安全区域、100vh。

## 5. 部署 / 迁移

1. `cd backend && .venv/bin/alembic upgrade head`（新迁移 `20261105_fashion_mango`，
   `down_revision = "20261103_fashion_asset_idx"`）。
2. 该迁移负责：给 `fashion_catalog` **补 `price_mango` / `purchasable` 两列**、建 3 张新表、
   把历史 `fashion_wardrobe` 回填成 `legacy_wardrobe` 所有权并建余额 0 的钱包。
   ⚠️ `init_db()` 的 `Base.metadata.create_all(checkfirst=True)` **只建缺失的表，不给已存在的表加列**
   （2026-11-01 事故 §六.106 同款教训）—— 所以**老库必须跑一次 alembic**，不能只靠重启。
   `db.py::_run_lightweight_migrations` 里另加了幂等 `ALTER TABLE` 兜底，覆盖「开发机直接跑
   create_all、没跑 alembic」的场景；服务层 `ensure_legacy_ownership()` 再兜一层懒回填。
3. `cd frontend && npm run build`（产物直出 `backend/static`，由后端托管；**禁止**
   `rm -rf backend/static/`，里面有 `item-icons/` `/fonts/` 等非构建产物）。
4. 后端 Python 代码改动需重启进程；静态产物改动刷新页面即可生效。
5. 验收：`sqlite3 <db> ".schema fashion_catalog"` 应能看到 `price_mango` / `purchasable`，
   `GET /api/fashion/wallet` 返回余额 0，`GET /api/fashion/store/items` 正常返回。

## 5.1 本期明确不做 / 已知限制

- **不接支付**：`POST /wallet/recharge` 固定 501。运营入金走 `POST /admin/wallet/adjust`。
- **不做后端缩略图端点**：生产环境没有 Pillow / sharp，引入图像处理依赖风险大于收益。
  移动端改用「降低每页条数 + `loading=lazy` + `fetchpriority=low`」控制同时解码的原图数量；
  单资产上限仍是 64MB，超大图仍可能占内存，后续可单独排期做缩略图服务。
- **不改资产路由对 `enabled=false` 的 404 行为**：那是既有契约（下架资产不再公开）。
  前端对停用 / `missing` 条目不渲染预览图，改为「已下架」占位，避免误报「文件丢失」。
- **不追溯收费**：上线前已加入衣柜的时装自动继承所有权；管理端后续给某件时装设价，
  不影响此前已获得它的玩家。
- **GET 会触发一次懒初始化**（有意的工程取舍）：首次 `GET /wallet` / `/store/items` /
  `/store/orders` 会自动建余额 0 的钱包、并把历史衣柜回填成所有权（之后不再写）。
  这是为了兜住「没跑 alembic 迁移的开发机」；若未来接了只读副本，需把这两步移到
  启动迁移 / 后台任务。
- **分页与用户列表走 Python 层过滤**：`list_transactions` / `list_orders` /
  `admin_wallet_users` 目前是全表取回再排序切片。站点用户/流水规模小时无感；
  规模变大后应下推到 SQL（`ORDER BY ... LIMIT/OFFSET` + `COUNT(*)` + `LIKE`）。
  管理端 `keyword` 已限长 64 字符，避免 CPU 放大。
- **PostgreSQL**：迁移与 `db.py` 兜底已做方言兼容（`sa.true()`、PRAGMA 只在 SQLite
  分支执行、金额列 `BigInteger`），但本仓测试全部跑在 SQLite 上，正式切 PG 前
  仍需在 PG 实跑一次 `alembic upgrade head` + `init_db()` 验证。

## 6. 测试

- `backend/tests/test_fashion_mango.py`：**50 个用例**，真实 SQLite + FastAPI TestClient +
  真 alembic `Operations`。覆盖余额、免费领取、付费购买扣款、402 余额不足、幂等重复购买、
  幂等键跨用户隔离、别名碰撞不得绕过计费（P0 回归）、并发同件只扣一次 / 并发不同件不超扣、
  管理端发放/扣减、定价与 `StrictInt`、防绕过（wardrobe / loadout）、真实
  `require_site_admin` 403、流水、购买记录、充值占位 501、历史衣柜回填、
  Alembic 迁移 upgrade/downgrade/幂等/轻量兜底。
- 全量时装测试：`cd backend && .venv/bin/pytest tests/test_fashion_*.py -q`
  → **324 passed, 49 subtests passed**（既有 274 个 + 新增 50 个）。
- 全量后端套件在带 `VMT_SERVER__SECRET_KEY` 时有 12 个**与本次改动无关**的基线失败
  （phase1 / litematica / bluemap / mcc_remote 的 asyncio event loop 与数据隔离问题），
  用 `--ignore=tests/test_fashion_mango.py` 对比可确认失败集合完全一致。
