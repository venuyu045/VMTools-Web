"""Pydantic schemas for the fashion (时装) API.

对外 JSON 一律 **camelCase**（mod 是 Kotlin/Java 写的，字段名按 mod 侧约定），
Python 侧保持 snake_case：用 ``alias`` + ``populate_by_name`` 两全。

错误响应统一为::

    {"detail": {"code": "challenge_expired", "message": "..."}}
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class FashionSchema(BaseModel):
    """所有 fashion schema 的基类：允许用字段名或别名（camelCase）赋值。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


# ── 认证 ────────────────────────────────────────────────────────────────

class ChallengeRequest(FashionSchema):
    mc_uuid: str = Field(..., alias="mcUuid", description="玩家 UUID（带/不带连字符均可）")
    mc_name: str = Field(..., alias="mcName", description="玩家名（3-16 位）")
    auth_server: str = Field(
        default="", alias="authServer",
        description="皮肤站 base URL；正版玩家留空。白名单外的地址会被忽略",
    )


class ChallengeResponse(FashionSchema):
    server_id: str = Field(..., alias="serverId")
    expires_in: int = Field(..., alias="expiresIn")
    auth_type: str = Field(..., alias="authType", description="mojang | yggdrasil")


class VerifyRequest(FashionSchema):
    mc_uuid: str = Field(..., alias="mcUuid")
    mc_name: str = Field(..., alias="mcName")
    auth_server: str = Field(default="", alias="authServer")
    server_id: str = Field(..., alias="serverId")


class IdentityPayload(FashionSchema):
    mc_uuid: str = Field(..., alias="mcUuid")
    mc_name: str = Field(..., alias="mcName")
    verified: bool = True


class VerifyResponse(FashionSchema):
    token: str
    expires_in: int = Field(..., alias="expiresIn")
    auth_type: str = Field(default="mojang", alias="authType")
    identity: IdentityPayload


class IdentityOut(FashionSchema):
    id: str
    mc_uuid: str = Field(..., alias="mcUuid")
    mc_name: str = Field(..., alias="mcName")
    auth_type: str = Field(..., alias="authType")
    auth_server: str = Field(default="", alias="authServer")
    verified: bool = True
    verified_at: Optional[str] = Field(default=None, alias="verifiedAt")
    user_id: Optional[str] = Field(default=None, alias="userId")
    created_at: Optional[str] = Field(default=None, alias="createdAt")
    last_seen_at: Optional[str] = Field(default=None, alias="lastSeenAt")


class IdentityListResponse(FashionSchema):
    items: list[IdentityOut]


# ── 目录 ────────────────────────────────────────────────────────────────

class CatalogItem(FashionSchema):
    cosmetic_id: str = Field(..., alias="cosmeticId")
    name: str = ""
    category: str = "other"
    asset_key: str = Field(default="", alias="assetKey")
    sha256: str = ""
    bytes: int = 0
    pack_id: str = Field(default="", alias="packId")
    sort_order: int = Field(default=0, alias="sortOrder")


class CatalogResponse(FashionSchema):
    items: list[CatalogItem]


# ── 穿戴配置 ────────────────────────────────────────────────────────────

class LoadoutItem(FashionSchema):
    slot: str = Field(..., description="cape|wings|hat|back|pet|aura|other")
    cosmetic_id: str = Field(..., alias="cosmeticId")
    # 省略 = 沿用该槽位已保存的 scale（mod 不保存缩放）；显式传 null 也按省略处理
    scale: Optional[float] = None


class LoadoutPutRequest(FashionSchema):
    items: list[LoadoutItem] = Field(default_factory=list)


class LoadoutWarning(FashionSchema):
    """某一条穿戴被跳过（时装不存在 / 已下架）—— 其余条目照常写入。"""

    slot: str
    cosmetic_id: str = Field(..., alias="cosmeticId")
    code: str = Field(..., description="unknown_cosmetic | cosmetic_disabled | not_owned")


class LoadoutPutResponse(FashionSchema):
    ok: bool = True
    updated_at: str = Field(..., alias="updatedAt")
    # 可选：被跳过的条目（老客户端忽略未知字段）
    warnings: list[LoadoutWarning] = Field(default_factory=list)


class LoadoutItemOut(FashionSchema):
    slot: str
    cosmetic_id: str = Field(..., alias="cosmeticId")
    scale: float = 1.0
    asset_key: str = Field(default="", alias="assetKey")
    sha256: str = ""
    bytes: int = 0
    name: str = ""
    category: str = "other"
    missing: bool = False
    # 包级下发：客户端据此知道这件时装属于哪个包、什么版本（mod 契约 §3）
    pack_id: str = Field(default="", alias="packId")
    pack_version: str = Field(default="", alias="packVersion")


class LoadoutResponse(FashionSchema):
    items: list[LoadoutItemOut]


# ── 我的衣柜（收藏） ────────────────────────────────────────────────────

class WardrobeItemOut(FashionSchema):
    """衣柜条目：目录元数据 + 加入时间。

    ``missing=True`` 表示目录里已删除或已停用 —— 条目**仍会返回**（玩家才删得掉），
    此时 name 等字段取目录残留值，目录整条没了就是空值。
    """

    cosmetic_id: str = Field(..., alias="cosmeticId")
    name: str = ""
    category: str = "other"
    pack_id: str = Field(default="", alias="packId")
    asset_key: str = Field(default="", alias="assetKey")
    sha256: str = ""
    bytes: int = 0
    sort_order: int = Field(default=0, alias="sortOrder")
    added_at: Optional[str] = Field(default=None, alias="addedAt")
    missing: bool = False


class WardrobeListResponse(FashionSchema):
    items: list[WardrobeItemOut]


class WardrobeAddRequest(FashionSchema):
    # 缺省空串而不是必填：让「空 cosmeticId」落到服务层的 invalid_cosmetic_id(400)，
    # 而不是 FastAPI 的 422 —— 前端只需要处理一种错误形状。
    cosmetic_id: str = Field(default="", alias="cosmeticId")


class WardrobeAddResponse(FashionSchema):
    ok: bool = True
    # False = 之前就在衣柜里（幂等重复添加，仍返回 200 和该条目）
    added: bool = False
    item: WardrobeItemOut


class WardrobeRemoveResponse(FashionSchema):
    ok: bool = True
    # False = 本来就不在衣柜里（删除是幂等的）
    removed: bool = False


class WardrobeBatchDeleteRequest(FashionSchema):
    cosmetic_ids: list[str] = Field(default_factory=list, alias="cosmeticIds")


class WardrobeBatchDeleteResponse(FashionSchema):
    ok: bool = True
    # 真正删掉的条数（不存在的 id 静默忽略，不计入）
    removed: int = 0


class WardrobeMeItemOut(FashionSchema):
    """mod 端用的精简条目（契约见 docs/fashion-api.md §5.5）。

    只给 mod 真正需要的 5 个字段：mod 拿它跟本地已装时装包比对，决定衣柜里
    「哪几件已下载、哪几件点穿上时要先下发」。assetKey / sha256 / bytes 那些
    资产元数据 mod 走 `/loadouts/resolve` 与包级下发，这里不重复下发。
    """

    cosmetic_id: str = Field(..., alias="cosmeticId")
    name: str = ""
    category: str = "other"
    pack_id: str = Field(default="", alias="packId")
    # 目录里已删除/已停用：mod 会显示但禁止穿戴（点了也只会被后端 skip）
    missing: bool = False


class WardrobeMeResponse(FashionSchema):
    """``GET /wardrobe/me`` 的响应。

    ``linked=False`` 表示这个 MC 身份还没绑定网站账号 —— 此时 ``items`` 一定是空，
    **但客户端不能把它当成"衣柜是空的"**：mod 会退回「显示本地已装全部时装」，
    否则没绑定的玩家打开衣柜会看到一片空白（还以为模组坏了）。
    """

    linked: bool = False
    items: list[WardrobeMeItemOut] = Field(default_factory=list)


class ResolvedItem(FashionSchema):
    """热路径返回项：只有 assetKey 级别元数据，不含资产内容。"""


    slot: str
    cosmetic_id: str = Field(..., alias="cosmeticId")
    asset_key: str = Field(default="", alias="assetKey")
    sha256: str = ""
    bytes: int = 0
    scale: float = 1.0
    # 包级下发（mod 契约 §3）：客户端靠它判断要不要整包下载
    pack_id: str = Field(default="", alias="packId")
    pack_version: str = Field(default="", alias="packVersion")


class ResolvedLoadout(FashionSchema):
    items: list[ResolvedItem]


class ResolveRequest(FashionSchema):
    mc_uuids: list[str] = Field(default_factory=list, alias="mcUuids")


class ResolveResponse(FashionSchema):
    loadouts: dict[str, ResolvedLoadout] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)


# ── 设备码（网页绑定） ──────────────────────────────────────────────────

class DeviceStartResponse(FashionSchema):
    user_code: str = Field(..., alias="userCode")
    device_code: str = Field(..., alias="deviceCode")
    expires_in: int = Field(..., alias="expiresIn")
    interval: int = 3


class DeviceApproveRequest(FashionSchema):
    user_code: str = Field(default="", alias="userCode")
    device_code: str = Field(default="", alias="deviceCode")


class DeviceApproveResponse(FashionSchema):
    ok: bool = True
    identity_id: str = Field(..., alias="identityId")
    mc_uuid: str = Field(default="", alias="mcUuid")
    mc_name: str = Field(default="", alias="mcName")


class DeviceStatusResponse(FashionSchema):
    status: str
    user_code: str = Field(default="", alias="userCode")
    bound_user_id: Optional[str] = Field(default=None, alias="boundUserId")


# ── 管理端 ──────────────────────────────────────────────────────────────

class CatalogUpsertRequest(FashionSchema):
    cosmetic_id: str = Field(default="", alias="cosmeticId")
    pack_id: str = Field(default="", alias="packId")
    name: str = ""
    category: str = "other"
    asset_key: str = Field(default="", alias="assetKey")
    sha256: str = ""
    bytes: Optional[int] = None
    enabled: Optional[bool] = None
    sort_order: Optional[int] = Field(default=None, alias="sortOrder")
    # 芒果块定价（v1.1）。None = 部分更新时不改；0 = 免费。
    # StrictInt：拒绝 bool / 字符串被 pydantic 静默转成数字（审计 P3-2）。
    price_mango: Optional[StrictInt] = Field(default=None, alias="priceMango")
    purchasable: Optional[bool] = None


class CatalogAdminItem(FashionSchema):
    cosmetic_id: str = Field(..., alias="cosmeticId")
    pack_id: str = Field(default="", alias="packId")
    name: str = ""
    category: str = "other"
    asset_key: str = Field(default="", alias="assetKey")
    sha256: str = ""
    bytes: int = 0
    enabled: bool = True
    sort_order: int = Field(default=0, alias="sortOrder")
    price_mango: int = Field(default=0, alias="priceMango")
    purchasable: bool = True


class CatalogAdminResponse(FashionSchema):
    items: list[CatalogAdminItem]


class AssetOut(FashionSchema):
    id: str
    pack_id: str = Field(default="", alias="packId")
    version: str = "1"
    filename: str = ""
    sha256: str = ""
    bytes: int = 0
    storage_path: str = Field(default="", alias="storagePath")
    created_at: Optional[str] = Field(default=None, alias="createdAt")


class AssetListResponse(FashionSchema):
    items: list[AssetOut]


class AssetUploadResponse(FashionSchema):
    ok: bool = True
    asset: AssetOut
    cosmetic_id: str = Field(default="", alias="cosmeticId")


# ── 包级下发（mod 契约：GET /packs + GET /packs/{id}/{version}/download） ──

class PackItem(FashionSchema):
    """一条包清单。

    ``publishedAt`` 必须是**毫秒时间戳 number**（mod 的 ``PackCatalog`` 用
    ``getAsLong()`` 读取；给 ISO-8601 字符串会静默变成 0）。
    """

    id: str
    version: str
    sha256: str
    bytes: int
    url: str = ""
    published_at: Optional[int] = Field(default=None, alias="publishedAt")
    # 可选签名（Ed25519，标准 base64）；未配置私钥时不输出这两个字段
    signature: Optional[str] = None
    signing_key_id: Optional[str] = Field(default=None, alias="signingKeyId")


class PacksResponse(FashionSchema):
    packs: list[PackItem] = Field(default_factory=list)


class PackUploadResponse(FashionSchema):
    ok: bool = True
    pack: PackItem


# ── 芒果块钱包 / 代币购买（v1.1）─────────────────────────────────────────
#
# 契约见 docs/fashion-mango-wallet-api.md。所有响应字段 camelCase（与 mod 侧
# 现有契约一致），错误响应仍是 ``{"detail": {"code","message"}}``。
# 充值通道本期只保留接口形状：``/wallet/recharge`` 固定返回 501。

MANGO_CURRENCY_NAME = "芒果块"


class WalletOut(FashionSchema):
    """当前用户的芒果块钱包（首次访问自动创建，余额 0）。"""

    balance: int = 0
    currency: str = MANGO_CURRENCY_NAME
    total_earned: int = Field(default=0, alias="totalEarned")
    total_spent: int = Field(default=0, alias="totalSpent")
    updated_at: Optional[str] = Field(default=None, alias="updatedAt")


class WalletTransactionItemOut(FashionSchema):
    """一条收支流水（按时间倒序）。"""

    id: str
    # purchase | refund | admin_adjust | recharge | grant
    type: str = "purchase"
    delta: int = 0
    balance_after: int = Field(default=0, alias="balanceAfter")
    amount_abs: int = Field(default=0, alias="amountAbs")
    cosmetic_id: str = Field(default="", alias="cosmeticId")
    cosmetic_name: str = Field(default="", alias="cosmeticName")
    note: str = ""
    created_at: Optional[str] = Field(default=None, alias="createdAt")


class WalletTransactionListResponse(FashionSchema):
    items: list[WalletTransactionItemOut] = Field(default_factory=list)
    total: int = 0
    limit: int = 20
    offset: int = 0


class RechargeChannel(FashionSchema):
    """一个充值渠道（本期恒为空数组；接入支付后由后端填充）。"""

    id: str
    name: str = ""
    description: str = ""
    enabled: bool = True


class RechargeOptionsResponse(FashionSchema):
    """``GET /wallet/recharge/options``：充值入口的全部展示信息。"""

    available: bool = False
    currency: str = MANGO_CURRENCY_NAME
    channels: list[RechargeChannel] = Field(default_factory=list)
    notice: str = "充值通道即将开放，目前可联系管理员发放芒果块"


class RechargeRequest(FashionSchema):
    """``POST /wallet/recharge`` 请求（本期返回 501，形状先定死）。"""

    amount: int = 0
    channel: str = ""
    # 幂等键：同一用户重复提交同一 key 只会入账一次（接入支付后生效）
    client_request_id: Optional[str] = Field(default=None, alias="clientRequestId")


class RechargeResponse(FashionSchema):
    """充值成功时的响应形状（本期不会返回，供后续接入支付时复用）。"""

    ok: bool = True
    balance: int = 0
    transaction_id: str = Field(default="", alias="transactionId")
    order_no: str = Field(default="", alias="orderNo")


class StoreItem(FashionSchema):
    """商店条目：目录元数据 + 定价 + 当前用户的拥有/衣柜状态。

    故意**不复用** ``CatalogItem``（那是 mod 契约 ``GET /catalog`` 的响应）：
    /catalog 带进程内缓存，混入用户维度字段会串号；商店页用独立的
    ``GET /store/items`` 聚合接口。
    """

    cosmetic_id: str = Field(..., alias="cosmeticId")
    name: str = ""
    category: str = "other"
    asset_key: str = Field(default="", alias="assetKey")
    sha256: str = ""
    bytes: int = 0
    pack_id: str = Field(default="", alias="packId")
    sort_order: int = Field(default=0, alias="sortOrder")
    price_mango: int = Field(default=0, alias="priceMango")
    purchasable: bool = True
    owned: bool = False
    in_wardrobe: bool = Field(default=False, alias="inWardrobe")


class StoreItemsResponse(FashionSchema):
    currency: str = MANGO_CURRENCY_NAME
    wallet: WalletOut
    recharge_available: bool = Field(default=False, alias="rechargeAvailable")
    items: list[StoreItem] = Field(default_factory=list)


class PurchaseRequest(FashionSchema):
    cosmetic_id: str = Field(default="", alias="cosmeticId")
    # 可选幂等键（<=64 字符）：网络重试 / 用户双击时不会重复扣款
    client_request_id: Optional[str] = Field(default=None, alias="clientRequestId")


class PurchaseResponse(FashionSchema):
    ok: bool = True
    # True = 本次真的获得了时装；False = 之前已拥有（幂等，不扣款）
    purchased: bool = False
    already_owned: bool = Field(default=False, alias="alreadyOwned")
    # True = 免费领取（价格 0），不扣款
    free: bool = False
    price_mango: int = Field(default=0, alias="priceMango")
    balance: int = 0
    cosmetic_id: str = Field(default="", alias="cosmeticId")
    item: Optional[StoreItem] = None
    # 扣款流水 id（免费领取 / 已拥有时为空串）
    transaction_id: str = Field(default="", alias="transactionId")


class PurchaseOrderItemOut(FashionSchema):
    """「购买记录」条目（所有权维度，删衣柜不会消失）。"""

    order_no: str = Field(default="", alias="orderNo")
    cosmetic_id: str = Field(..., alias="cosmeticId")
    name: str = ""
    category: str = "other"
    pack_id: str = Field(default="", alias="packId")
    price_paid: int = Field(default=0, alias="pricePaid")
    # purchase | free_claim | legacy_wardrobe | admin_grant | migration
    source: str = "purchase"
    note: str = ""
    acquired_at: Optional[str] = Field(default=None, alias="acquiredAt")
    # 目录已删除 / 已停用
    missing: bool = False


class PurchaseOrderListResponse(FashionSchema):
    items: list[PurchaseOrderItemOut] = Field(default_factory=list)
    total: int = 0
    limit: int = 20
    offset: int = 0


class AdminWalletAdjustRequest(FashionSchema):
    user_id: str = Field(default="", alias="userId")
    # 正整数 = 发放，负整数 = 扣回；不能为 0。StrictInt 拒绝 bool / 字符串。
    delta: StrictInt = 0
    note: str = ""


class AdminWalletAdjustResponse(FashionSchema):
    ok: bool = True
    user_id: str = Field(default="", alias="userId")
    balance: int = 0
    delta: int = 0
    transaction_id: str = Field(default="", alias="transactionId")


class AdminWalletUserItem(FashionSchema):
    user_id: str = Field(..., alias="userId")
    game_id: str = Field(default="", alias="gameId")
    display_name: str = Field(default="", alias="displayName")
    role: str = ""
    balance: int = 0
    total_earned: int = Field(default=0, alias="totalEarned")
    total_spent: int = Field(default=0, alias="totalSpent")


class AdminWalletUserListResponse(FashionSchema):
    items: list[AdminWalletUserItem] = Field(default_factory=list)
    total: int = 0
    limit: int = 20
    offset: int = 0


class AdminGrantCosmeticRequest(FashionSchema):
    user_id: str = Field(default="", alias="userId")
    cosmetic_id: str = Field(default="", alias="cosmeticId")
    note: str = ""


class AdminGrantCosmeticResponse(FashionSchema):
    ok: bool = True
    user_id: str = Field(default="", alias="userId")
    cosmetic_id: str = Field(default="", alias="cosmeticId")
    # False = 该用户之前已拥有（幂等，不重复写入）
    granted: bool = False
    item: Optional[StoreItem] = None
