"""时装系统 API（VMFashion Fabric 客户端 mod + 网站绑定/衣柜页）。

接口分组
--------
公开（匿名可读）::

    GET  /api/fashion/packs                     （包清单，mod 自动下发用）
    GET  /api/fashion/packs/{id}/{version}/download （整包 zip 字节流，200 直出）
    GET  /api/fashion/assets/{assetKey}         （单件资产，长缓存）

mod（Bearer fashion token）::

    POST /api/fashion/auth/challenge
    POST /api/fashion/auth/verify
    GET  /api/fashion/catalog
    PUT  /api/fashion/loadout
    GET  /api/fashion/loadout/me
    POST /api/fashion/loadouts/resolve
    POST /api/fashion/device/start               （生成 6 位绑定码）
    GET  /api/fashion/device/status              （轮询绑定结果）
    GET  /api/fashion/wardrobe/me                （拉「我的衣柜」收藏：token 或网页 JWT 都行）

网站（网页 JWT，与 mod token 二选一）::

    POST   /api/fashion/device/approve           （输入 6 位码完成绑定）
    GET    /api/fashion/identities/me
    DELETE /api/fashion/identities/{id}          （解绑）
    PUT    /api/fashion/loadout                  （衣柜页保存，同一套接口）
    GET    /api/fashion/loadout/me
    GET    /api/fashion/wardrobe                 （我的衣柜，user 维度收藏）
    POST   /api/fashion/wardrobe                 （加入衣柜，幂等；付费时装需先购买）
    DELETE /api/fashion/wardrobe/{cosmeticId}    （单件删除）
    POST   /api/fashion/wardrobe/batch-delete    （批量删除）

芒果块钱包 / 商店（网页 JWT；契约见 docs/fashion-mango-wallet-api.md）::

    GET    /api/fashion/wallet
    GET    /api/fashion/wallet/transactions
    GET    /api/fashion/wallet/recharge/options  （充值入口信息，本期 available=false）
    POST   /api/fashion/wallet/recharge          （预留：本期固定 501）
    GET    /api/fashion/store/items              （目录 + 定价 + 拥有/衣柜状态 + 余额）
    POST   /api/fashion/store/purchase           （芒果块购买 / 免费领取）
    GET    /api/fashion/store/orders             （购买记录）

管理端（site_admin）::

    GET/POST      /api/fashion/admin/catalog      （含 priceMango / purchasable）
    PUT/DELETE    /api/fashion/admin/catalog/{cosmeticId}
    GET/POST      /api/fashion/admin/assets
    POST          /api/fashion/admin/packs       （上传/发布整包 zip）
    DELETE        /api/fashion/admin/packs/{id}/{version} （下架）
    GET           /api/fashion/admin/wallet/users
    POST          /api/fashion/admin/wallet/adjust      （发放/扣回芒果块）
    POST          /api/fashion/admin/wallet/grant-cosmetic（直接发放时装）

鉴权：``Authorization: Bearer <token>``。token 可以是 mod 的 fashion token
（``aud=fashion``）或网页登录 JWT（``sub`` = 用户 id）；网页 JWT 通过
``fashion_identity.user_id`` 找到绑定的 MC 身份。

包级下发（``/packs``）与单件资产（``/assets``）都是**公开**的：mod 的清单请求可能
不带 token，``<img src>`` 也带不了 Authorization 头；两者都按内容哈希寻址、本身不含隐私。
"""
from __future__ import annotations

import mimetypes
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from vmtools_next.api.deps import get_current_user, get_db, require_site_admin
from vmtools_next.api.schemas.fashion import (
    AdminGrantCosmeticRequest,
    AdminGrantCosmeticResponse,
    AdminWalletAdjustRequest,
    AdminWalletAdjustResponse,
    AdminWalletUserListResponse,
    AssetListResponse,
    AssetOut,
    AssetUploadResponse,
    CatalogAdminItem,
    CatalogAdminResponse,
    CatalogItem,
    CatalogResponse,
    CatalogUpsertRequest,
    ChallengeRequest,
    ChallengeResponse,
    DeviceApproveRequest,
    DeviceApproveResponse,
    DeviceStartResponse,
    DeviceStatusResponse,
    IdentityListResponse,
    IdentityOut,
    IdentityPayload,
    LoadoutItemOut,
    LoadoutPutRequest,
    LoadoutPutResponse,
    LoadoutWarning,
    LoadoutResponse,
    MANGO_CURRENCY_NAME,
    PackItem,
    PackUploadResponse,
    PacksResponse,
    PurchaseOrderListResponse,
    PurchaseRequest,
    PurchaseResponse,
    RechargeOptionsResponse,
    RechargeRequest,
    RechargeResponse,
    ResolveRequest,
    ResolveResponse,
    StoreItem,
    StoreItemsResponse,
    VerifyRequest,
    VerifyResponse,
    WalletOut,
    WalletTransactionListResponse,
    WardrobeAddRequest,
    WardrobeAddResponse,
    WardrobeBatchDeleteRequest,
    WardrobeBatchDeleteResponse,
    WardrobeItemOut,
    WardrobeListResponse,
    WardrobeMeItemOut,
    WardrobeMeResponse,
    WardrobeRemoveResponse,
)
from vmtools_next.core import fashion_service as svc
from vmtools_next.core.fashion_auth import (
    FashionAuthError,
    approve_device_flow,
    consume_challenge,
    create_challenge,
    decode_token,
    decode_web_token,
    device_flow_status,
    find_device,
    issue_token,
    start_device_flow,
    verify_with_session_server,
)
from vmtools_next.core.fashion_service import FashionServiceError, MAX_ASSET_BYTES
from vmtools_next.data.models.auth import UserModel
from loguru import logger

router = APIRouter(prefix="/api/fashion", tags=["fashion"])
_bearer = HTTPBearer(auto_error=False)

# ── challenge 限流（防被刷爆外呼） ──────────────────────────────────────
_CHALLENGE_HITS: dict[str, list[float]] = {}
_CHALLENGE_WINDOW = 60.0
_CHALLENGE_LIMIT = 20


def _rate_limited(
    message: str, retry_after: int = 60, *, code: str = "rate_limited"
) -> HTTPException:
    """统一的 429：带 ``Retry-After``（mod 的 RetryPolicy 会按它等待，最多 60s）。"""
    return HTTPException(
        status_code=429,
        detail={"code": code, "message": message},
        headers={"Retry-After": str(int(retry_after))},
    )


#: 限流表的读-改-写必须串行。这三个限流器被**同步路由**使用，而 FastAPI 会把同步
#: 路由丢进线程池并发执行：无锁时两个请求可能读到同一份 ``hits`` 快照、各自 append 后
#: 互相覆盖（丢更新）—— 读到同一份快照 → 多个请求同时放行（绕过限流，device/approve
#: 的 10/min 是 6 位绑定码的防枚举控制）；写入互相覆盖 → 已计数的命中被抹掉（后续
#: 合法请求被误拒）。CPython 下这个临界区只有 ~1µs、GIL 5ms 才切一次，现场很难撞上，
#: 但加锁后「放行数 == limit」才是可证明的，成本只是一次无争用 lock。
_RATE_LOCK = threading.Lock()


def _check_rate(
    store: dict[str, list[float]],
    key: str,
    *,
    window: float,
    limit: int,
    message: str,
    code: str = "rate_limited",
) -> None:
    """滑动窗口限流（进程内；多 worker 部署时各进程独立，见 docs §11/§13）。"""
    now = time.monotonic()
    with _RATE_LOCK:
        hits = [t for t in store.get(key, []) if now - t < window]
        if len(hits) >= limit:
            raise _rate_limited(message, int(window), code=code)
        hits.append(now)
        store[key] = hits
        if len(store) > 5000:  # 防内存膨胀：清掉过期 key
            for k in [k for k, v in store.items() if not v or now - v[-1] > window]:
                store.pop(k, None)


def _check_challenge_rate(key: str) -> None:
    _check_rate(
        _CHALLENGE_HITS,
        key,
        window=_CHALLENGE_WINDOW,
        limit=_CHALLENGE_LIMIT,
        message="认证请求过于频繁，请稍后再试",
    )


# ── device/approve 限流（6 位用户码理论上可被枚举，必须限制失败尝试） ──
_DEVICE_APPROVE_HITS: dict[str, list[float]] = {}
_DEVICE_APPROVE_WINDOW = 60.0
_DEVICE_APPROVE_LIMIT = 10


def _check_device_approve_rate(key: str) -> None:
    _check_rate(
        _DEVICE_APPROVE_HITS,
        key,
        window=_DEVICE_APPROVE_WINDOW,
        limit=_DEVICE_APPROVE_LIMIT,
        message="绑定码尝试过于频繁，请稍后再试",
    )


# ── device/start 限流（否则一个 mod 能刷爆内存里的设备码表，挤掉别人的绑定码） ──
_DEVICE_START_HITS: dict[str, list[float]] = {}
_DEVICE_START_WINDOW = 60.0
_DEVICE_START_LIMIT = 10


def _check_device_start_rate(key: str) -> None:
    _check_rate(
        _DEVICE_START_HITS,
        key,
        window=_DEVICE_START_WINDOW,
        limit=_DEVICE_START_LIMIT,
        message="绑定码申请过于频繁，请稍后再试",
    )


# ── 购买限流（每个用户一个滑动窗口；防脚本刷接口 / 放大 DB 写压力）──────
_PURCHASE_HITS: dict[str, list[float]] = {}


def _check_purchase_rate(user_id: str) -> None:
    _check_rate(
        _PURCHASE_HITS,
        user_id,
        window=svc.PURCHASE_RATE_WINDOW,
        limit=svc.PURCHASE_RATE_LIMIT,
        message="购买操作过于频繁，请稍后再试",
        code="purchase_rate_limited",
    )


# ── 统一错误翻译 ────────────────────────────────────────────────────────

def _http_error(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", "error")
    message = getattr(exc, "message", str(exc))
    status_code = int(getattr(exc, "status_code", 400) or 400)
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _validate_catalog_filters(category: Optional[str], pack_id: Optional[str]) -> None:
    """限制 /catalog 查询参数，避免任意组合把进程内缓存刷爆。"""
    if category and category not in svc.CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_category",
                "message": f"category 必须是 {', '.join(svc.CATEGORIES)} 之一",
            },
        )
    if pack_id and len(str(pack_id)) > 64:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_pack_id", "message": "pack_id 长度不能超过 64"},
        )


# ── 鉴权依赖 ────────────────────────────────────────────────────────────

@dataclass
class FashionPrincipal:
    """调用者身份：mod token 直接给 identity；网页 JWT 通过 user 反查绑定。"""

    identity_id: Optional[str]
    user_id: Optional[str]
    via: str  # "mod" | "web"
    mc_uuid: str = ""
    mc_name: str = ""


def get_principal(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: Session = Depends(get_db),
) -> FashionPrincipal:
    """接受 fashion token 或网页 JWT；都不合法则 401。"""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "缺少 Authorization: Bearer token"},
        )
    token = credentials.credentials

    # 1) mod 的 fashion token
    try:
        claims = decode_token(token)
    except FashionAuthError as exc:
        if exc.code == "server_misconfigured":
            raise _http_error(exc)
        claims = None
    if claims:
        identity = svc.get_identity(db, claims.get("sub"))
        if identity is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "identity_not_found", "message": "身份不存在，请重新认证"},
            )
        return FashionPrincipal(
            identity_id=getattr(identity, "id", None),
            user_id=getattr(identity, "user_id", None),
            via="mod",
            mc_uuid=getattr(identity, "mc_uuid", ""),
            mc_name=getattr(identity, "mc_name", ""),
        )

    # 2) 网页登录 JWT
    user_id = decode_web_token(token)
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_token", "message": "token 无效或已过期"},
        )
    # 与 deps.get_current_user 保持一致：token 有效还要查库确认账号仍存在且已批准。
    # 否则被禁用/未批准的账号只要旧 token 未过期就能继续调用时装接口。
    user = db.query(UserModel).filter(UserModel.id == str(user_id)).first()
    if user is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_token", "message": "token 对应的用户不存在"},
        )
    if getattr(user, "status", "") != "approved":
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden", "message": "账号未批准或已被禁用"},
        )
    identities = svc.list_identities_for_user(db, user_id)
    identity = identities[0] if identities else None
    return FashionPrincipal(
        identity_id=getattr(identity, "id", None) if identity is not None else None,
        user_id=str(user_id),
        via="web",
        mc_uuid=getattr(identity, "mc_uuid", "") if identity is not None else "",
        mc_name=getattr(identity, "mc_name", "") if identity is not None else "",
    )


def _resolve_identity(
    principal: FashionPrincipal,
    db: Session,
    identity_id: Optional[str] = None,
):
    """把 principal 解析成一条 fashion_identity（网页端支持 ?identityId= 指定）。"""
    if identity_id:
        identity = svc.get_identity(db, identity_id)
        if identity is None:
            raise HTTPException(
                status_code=404, detail={"code": "identity_not_found", "message": "身份不存在"}
            )
        owner = getattr(identity, "user_id", None)
        # 安全：不能写成 owner == principal.user_id —— 两者都是 None（未绑定身份的
        # mod token 访问未绑定身份）时会误判为同一个 owner，造成任意 fashion token
        # 越权读写他人 loadout。只有「网页登录态且同一非空网站账号」或「mod 自己
        # 的身份」合法；identityId 参数是网页多身份切换用的，mod token 不借此跨身份。
        same_user = (
            principal.via == "web"
            and principal.user_id is not None
            and owner == principal.user_id
        )
        own_identity = (
            principal.identity_id is not None
            and getattr(identity, "id", None) == principal.identity_id
        )
        if not (same_user or own_identity):
            raise HTTPException(
                status_code=403, detail={"code": "forbidden", "message": "无权访问该身份"}
            )
        return identity

    if principal.identity_id:
        identity = svc.get_identity(db, principal.identity_id)
        if identity is not None:
            return identity

    if principal.user_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "no_identity",
                "message": "当前账号尚未绑定 Minecraft 身份，请先在「时装绑定」页完成绑定",
            },
        )
    raise HTTPException(
        status_code=401, detail={"code": "unauthorized", "message": "无法识别调用者身份"}
    )


def _assert_identity_bindable(db: Session, identity_id: str, user_id: str):
    """设备码绑定前校验：身份存在，且未绑定到其他网站账号。"""
    identity = svc.get_identity(db, identity_id)
    if identity is None:
        raise HTTPException(
            status_code=404, detail={"code": "identity_not_found", "message": "身份不存在"}
        )
    owner = getattr(identity, "user_id", None)
    if owner and owner != str(user_id):
        raise HTTPException(
            status_code=409,
            detail={"code": "identity_bound_to_other", "message": "该 MC 账号已绑定到其他网站账号"},
        )
    return identity


# ── 认证：challenge / verify ────────────────────────────────────────────

@router.post("/auth/challenge", response_model=ChallengeResponse)
def auth_challenge(
    body: ChallengeRequest,
    request: Request,
) -> ChallengeResponse:
    """生成一次性 serverId（TTL 120s，绑定来源 IP）。"""
    source_ip = request.client.host if getattr(request, "client", None) else ""
    _check_challenge_rate(source_ip or "unknown")
    try:
        challenge = create_challenge(
            body.mc_uuid, body.mc_name, body.auth_server, source_ip=source_ip
        )
    except FashionAuthError as exc:
        raise _http_error(exc)
    return ChallengeResponse(
        server_id=challenge.server_id,
        expires_in=challenge.ttl_remaining,
        auth_type=challenge.auth_type,
    )


@router.post("/auth/verify", response_model=VerifyResponse)
async def auth_verify(
    body: VerifyRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> VerifyResponse:
    """消费 serverId，向会话服务器反查 hasJoined 并签发 token。"""
    source_ip = request.client.host if getattr(request, "client", None) else ""
    try:
        challenge = consume_challenge(
            body.server_id,
            body.mc_uuid,
            body.mc_name,
            source_ip=source_ip,
        )
        info = await verify_with_session_server(challenge)
        # 落库是同步阻塞（SQLite 写 + 可能的唯一约束重查），别在事件循环里做
        identity = await run_in_threadpool(
            svc.upsert_identity,
            db,
            mc_uuid=info["mc_uuid"],
            mc_name=info["mc_name"],
            auth_type=info["auth_type"],
            auth_server=info["auth_server"],
        )
        token, ttl = issue_token(
            getattr(identity, "id", ""),
            mc_uuid=getattr(identity, "mc_uuid", ""),
            mc_name=getattr(identity, "mc_name", ""),
        )
    except (FashionAuthError, FashionServiceError) as exc:
        logger.warning(
            "fashion verify failed uuid={} code={}", body.mc_uuid, getattr(exc, "code", "?")
        )
        raise _http_error(exc)

    logger.info(
        "fashion verify ok uuid={} name={} type={}",
        getattr(identity, "mc_uuid", ""),
        getattr(identity, "mc_name", ""),
        getattr(identity, "auth_type", ""),
    )
    return VerifyResponse(
        token=token,
        expires_in=ttl,
        auth_type=getattr(identity, "auth_type", "mojang") or "mojang",
        identity=IdentityPayload(
            mc_uuid=getattr(identity, "mc_uuid", ""),
            mc_name=getattr(identity, "mc_name", ""),
            verified=True,
        ),
    )


# ── 目录 ────────────────────────────────────────────────────────────────

@router.get("/catalog", response_model=CatalogResponse)
def get_catalog(
    category: Optional[str] = None,
    pack_id: Optional[str] = None,
    principal: FashionPrincipal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> CatalogResponse:
    """时装目录（只含 enabled，按 sortOrder 排序）。"""
    _validate_catalog_filters(category, pack_id)
    items = svc.list_catalog(db, enabled_only=True, category=category, pack_id=pack_id)
    return CatalogResponse(items=[CatalogItem(**item) for item in items])


# ── 穿戴配置 ────────────────────────────────────────────────────────────

@router.put("/loadout", response_model=LoadoutPutResponse)
def put_loadout(
    body: LoadoutPutRequest,
    identity_id: Optional[str] = Query(None, alias="identityId"),
    principal: FashionPrincipal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> LoadoutPutResponse:
    """全量覆盖穿戴配置（未出现的槽位 = 卸下）。

    未知 / 已下架的 cosmeticId 只跳过该条并放进 ``warnings``（不整条失败），
    只有请求体本身非法（slot / 条数 / scale）才 4xx。
    """
    identity = _resolve_identity(principal, db, identity_id)
    try:
        updated_at, warnings = svc.put_loadout(
            db,
            getattr(identity, "id", ""),
            [item.model_dump(by_alias=True, exclude_unset=True) for item in body.items],
            # v1.1：网页登录态 / 已绑定账号的 mod token 会带 user_id，用于
            # 「付费时装未购买不能穿」的校验；未绑定的 mod token 传 None 跳过。
            user_id=getattr(principal, "user_id", None),
        )
    except FashionServiceError as exc:
        raise _http_error(exc)
    if warnings:
        logger.info(
            "fashion loadout skipped identity={} items={}",
            getattr(identity, "id", ""), warnings,
        )
    return LoadoutPutResponse(
        ok=True,
        updated_at=updated_at.isoformat() if updated_at else "",
        warnings=[LoadoutWarning(**item) for item in warnings],
    )


@router.get("/loadout/me", response_model=LoadoutResponse)
def get_my_loadout(
    identity_id: Optional[str] = Query(None, alias="identityId"),
    principal: FashionPrincipal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> LoadoutResponse:
    """当前身份（或网页账号绑定的身份）的穿戴配置。"""
    identity = _resolve_identity(principal, db, identity_id)
    items = svc.get_loadout(db, getattr(identity, "id", ""))
    return LoadoutResponse(items=[LoadoutItemOut(**item) for item in items])


@router.post("/loadouts/resolve", response_model=ResolveResponse)
def resolve_loadouts(
    body: ResolveRequest,
    principal: FashionPrincipal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ResolveResponse:
    """按 UUID 批量解析（热路径：只返回 assetKey 级元数据，最多 200 个）。"""
    try:
        payload = svc.resolve_loadouts(db, body.mc_uuids)
    except FashionServiceError as exc:
        raise _http_error(exc)
    return ResolveResponse(**payload)


# ── 我的衣柜（收藏，user 维度） ─────────────────────────────────────────
#
# 与 /loadout 的分工：loadout 是「穿在身上什么」（MC 身份维度，mod 也读写）；
# wardrobe 是「我收藏了哪些时装」（网站账号维度）。
# 一个账号绑定的多个 MC 身份共享同一份衣柜，所以：
#   - 网页端（商店页 / 衣柜页）走 get_current_user（纯网页 JWT，只按账号）；
#   - mod 端走 GET /wardrobe/me + get_principal（fashion token 或网页 JWT 都行），
#     由 principal.user_id 反查账号；身份没绑账号时返回 linked=false 而不是空衣柜。

@router.get("/wardrobe", response_model=WardrobeListResponse)
def list_wardrobe(
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WardrobeListResponse:
    """我的衣柜（按加入时间倒序；目录已删除/已停用的条目仍返回并标 ``missing``）。"""
    items = svc.list_wardrobe(db, str(user.id))
    return WardrobeListResponse(items=[WardrobeItemOut(**item) for item in items])


@router.get("/wardrobe/me", response_model=WardrobeMeResponse)
def my_wardrobe_for_client(
    principal: FashionPrincipal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> WardrobeMeResponse:
    """mod 端拉取「我的衣柜」收藏（mod fashion token 或网页 JWT 都接受）。

    游戏内衣柜按这份清单过滤：只有收藏里的时装才显示。返回的是**精简条目**
    （cosmeticId / name / category / packId / missing），mod 拿它跟本地已装包比对，
    决定哪几件已经下载、哪几件点穿上时要先触发时装包下发。

    ``linked=false`` 的两种情况（都返回 200，**不是错误**）：
    - mod token 对应的 MC 身份还没绑定网站账号（``identity.user_id`` 为空）；
    - 网页 JWT 指向的账号没有任何绑定身份。

    客户端必须把 ``linked=false`` 理解成「没有衣柜可参照」→ 退回显示本地已装全部，
    而不是当成"衣柜是空的"，否则没绑定的玩家打开衣柜会一片空白。
    """
    user_id = getattr(principal, "user_id", None)
    if not user_id:
        return WardrobeMeResponse(linked=False, items=[])
    items = svc.list_wardrobe(db, str(user_id))
    return WardrobeMeResponse(
        linked=True,
        items=[
            WardrobeMeItemOut(
                cosmetic_id=item.get("cosmeticId", "") or "",
                name=item.get("name", "") or "",
                category=item.get("category", "other") or "other",
                pack_id=item.get("packId", "") or "",
                missing=bool(item.get("missing", False)),
            )
            for item in items
        ],
    )


@router.post("/wardrobe", response_model=WardrobeAddResponse)
def add_wardrobe_item(
    body: WardrobeAddRequest,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WardrobeAddResponse:
    """把一件时装加入衣柜。

    幂等：已经在衣柜里时返回 ``added=false`` + 该条目（仍是 200），前端不必区分
    「首次加入」和「重复点击」的失败态。目录里没有 / 已停用 → 404
    ``cosmetic_not_found``；空或超长 cosmeticId → 400 ``invalid_cosmetic_id``。
    """
    try:
        item, added = svc.add_wardrobe_item(db, str(user.id), body.cosmetic_id)
    except FashionServiceError as exc:
        raise _http_error(exc)
    return WardrobeAddResponse(ok=True, added=added, item=WardrobeItemOut(**item))


@router.post("/wardrobe/batch-delete", response_model=WardrobeBatchDeleteResponse)
def batch_delete_wardrobe_items(
    body: WardrobeBatchDeleteRequest,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WardrobeBatchDeleteResponse:
    """批量删除（衣柜页「全选删除」）。

    ``removed`` 是**真正删掉**的条数：不存在的 id 静默忽略（并发删除、目录被删的
    残留条目都走这条路径，不该让整批失败）。空数组 → 400 ``empty_selection``；
    去重后超过 500 条 → 400 ``too_many_items``。
    """
    try:
        removed = svc.remove_wardrobe_items(db, str(user.id), body.cosmetic_ids)
    except FashionServiceError as exc:
        raise _http_error(exc)
    return WardrobeBatchDeleteResponse(ok=True, removed=removed)


@router.delete("/wardrobe/{cosmetic_id}", response_model=WardrobeRemoveResponse)
def delete_wardrobe_item(
    cosmetic_id: str,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WardrobeRemoveResponse:
    """单件删除（幂等：不在衣柜里时 ``removed=false``，仍是 200）。

    cosmeticId 里可能有冒号（``pack1:cape_dragon``），作为单个路径段直接传即可，
    前端记得 ``encodeURIComponent``。
    """
    removed = svc.remove_wardrobe_item(db, str(user.id), cosmetic_id)
    return WardrobeRemoveResponse(ok=True, removed=removed)


# ── 芒果块钱包 / 代币购买（网页 JWT）─────────────────────────────────────
#
# 契约：docs/fashion-mango-wallet-api.md
# 货币叫「芒果块」，整数，余额恒非负。充值通道本期只保留接口形状：
# ``GET /wallet/recharge/options`` 返回 available=false，``POST /wallet/recharge``
# 固定 501；后续接支付时只需实现入账并把 available 打开，前端不用改契约。
# 管理端发放是本期唯一的入金途径。

_RECHARGE_NOTICE = "充值通道即将开放，目前可联系管理员发放芒果块"
# 501 的 message 与 /wallet/recharge/options 的 notice 分开：前者是「操作结果」，
# 后者是「入口说明」，不要混用（契约 §3.1 与 docs/fashion-mango-wallet-api.md）。
_RECHARGE_501_MESSAGE = "充值通道即将开放，敬请期待"


@router.get("/wallet", response_model=WalletOut)
def get_my_wallet(
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WalletOut:
    """我的芒果块余额（首次访问自动创建余额 0 的钱包）。"""
    return WalletOut(**svc.get_wallet(db, str(user.id)))


@router.get("/wallet/transactions", response_model=WalletTransactionListResponse)
def list_my_transactions(
    limit: int = Query(svc.DEFAULT_LIST_LIMIT, ge=1),
    offset: int = Query(0, ge=0),
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WalletTransactionListResponse:
    """芒果块收支明细（按时间倒序，分页，limit 最大 100）。"""
    payload = svc.list_transactions(db, str(user.id), limit=limit, offset=offset)
    return WalletTransactionListResponse(**payload)


@router.get("/wallet/recharge/options", response_model=RechargeOptionsResponse)
def recharge_options(
    _: UserModel = Depends(get_current_user),
) -> RechargeOptionsResponse:
    """充值入口信息（本期恒 ``available=false``，channels 为空）。

    前端「充值」弹窗只读这个接口决定渲染什么，不硬编码占位文案；接入支付后
    后端把 channels / available 填上，前端无需发版。
    """
    return RechargeOptionsResponse(
        available=False,
        currency=MANGO_CURRENCY_NAME,
        channels=[],
        notice=_RECHARGE_NOTICE,
    )


@router.post("/wallet/recharge", response_model=RechargeResponse)
def recharge_wallet(
    body: RechargeRequest,
    _: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RechargeResponse:
    """**预留接口**：本期不接支付，固定 501，接口形状先定死。

    后续实现点（保持请求/响应形状不变）：

    1. 校验 ``amount``（正整数、单次上限）与 ``channel`` 在 channels 白名单内；
    2. 创建订单并拉起支付，回调验签成功后用 ``clientRequestId`` 作幂等键调用
       ``svc.get_or_create_wallet`` + 条件入账 + 写 ``tx_type="recharge"`` 流水；
    3. 返回 ``RechargeResponse``，并把 ``/wallet/recharge/options`` 的 available 置 true。

    ⚠️ 现在**绝不能**真的入账：没有支付凭证的入金等于无限刷币。
    """
    raise HTTPException(
        status_code=501,
        detail={"code": "recharge_not_available", "message": _RECHARGE_501_MESSAGE},
    )


@router.get("/store/items", response_model=StoreItemsResponse)
def list_store_items(
    category: Optional[str] = None,
    pack_id: Optional[str] = None,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StoreItemsResponse:
    """商店页聚合数据：目录 + 定价 + 拥有/衣柜状态 + 钱包余额（一次拿全）。

    与 ``GET /catalog`` 的区别：catalog 是 mod 契约、带全站共享缓存、不含用户态；
    这里按当前登录用户计算 owned/inWardrobe，不能复用那份缓存。
    """
    _validate_catalog_filters(category, pack_id)
    payload = svc.store_items(
        db, str(user.id), category=category, pack_id=pack_id
    )
    return StoreItemsResponse(
        currency=payload["currency"],
        wallet=WalletOut(**payload["wallet"]),
        rechargeAvailable=bool(payload["rechargeAvailable"]),
        items=[StoreItem(**item) for item in payload["items"]],
    )


@router.post("/store/purchase", response_model=PurchaseResponse)
def purchase_cosmetic(
    body: PurchaseRequest,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PurchaseResponse:
    """用芒果块购买 / 免费领取一件时装（购买成功自动加入衣柜）。

    幂等：已拥有时返回 ``purchased=false, alreadyOwned=true``，不再扣款；
    带 ``clientRequestId`` 时重复提交同一 key 直接返回首次结果。
    错误码见 docs/fashion-mango-wallet-api.md §3.2（402 余额不足 / 404 / 409 / 429）。
    """
    _check_purchase_rate(str(user.id))
    try:
        payload = svc.purchase(
            db,
            str(user.id),
            body.cosmetic_id,
            client_request_id=body.client_request_id,
        )
    except FashionServiceError as exc:
        raise _http_error(exc)
    item = payload.get("item")
    return PurchaseResponse(
        ok=bool(payload.get("ok", True)),
        purchased=bool(payload.get("purchased", False)),
        alreadyOwned=bool(payload.get("alreadyOwned", False)),
        free=bool(payload.get("free", False)),
        priceMango=int(payload.get("priceMango", 0) or 0),
        balance=int(payload.get("balance", 0) or 0),
        cosmeticId=str(payload.get("cosmeticId", "") or ""),
        item=StoreItem(**item) if item else None,
        transactionId=str(payload.get("transactionId", "") or ""),
    )


@router.get("/store/orders", response_model=PurchaseOrderListResponse)
def list_my_orders(
    limit: int = Query(svc.DEFAULT_LIST_LIMIT, ge=1),
    offset: int = Query(0, ge=0),
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PurchaseOrderListResponse:
    """我的购买/获得记录（所有权维度，删衣柜不会消失）。"""
    payload = svc.list_orders(db, str(user.id), limit=limit, offset=offset)
    return PurchaseOrderListResponse(**payload)


# ── 管理端：芒果块（site_admin）──────────────────────────────────────────

@router.get("/admin/wallet/users", response_model=AdminWalletUserListResponse)
def admin_list_wallet_users(
    keyword: str = Query("", max_length=64),
    limit: int = Query(svc.DEFAULT_LIST_LIMIT, ge=1),
    offset: int = Query(0, ge=0),
    _: UserModel = Depends(require_site_admin),
    db: Session = Depends(get_db),
) -> AdminWalletUserListResponse:
    """用户钱包列表（按 gameId / displayName 模糊搜索）。"""
    payload = svc.admin_wallet_users(
        db, keyword=keyword, limit=limit, offset=offset
    )
    return AdminWalletUserListResponse(**payload)


@router.post("/admin/wallet/adjust", response_model=AdminWalletAdjustResponse)
def admin_adjust_wallet(
    body: AdminWalletAdjustRequest,
    admin: UserModel = Depends(require_site_admin),
    db: Session = Depends(get_db),
) -> AdminWalletAdjustResponse:
    """发放 / 扣回芒果块（充值开放前唯一入金途径，写操作者审计）。

    ``delta > 0`` 发放，``delta < 0`` 扣回且不允许把余额扣成负数。
    """
    try:
        payload = svc.admin_adjust_wallet(
            db,
            body.user_id,
            body.delta,
            operator_user_id=str(admin.id),
            note=body.note,
        )
    except FashionServiceError as exc:
        raise _http_error(exc)
    logger.info(
        "fashion mango admin adjust target={} delta={} operator={}",
        body.user_id, body.delta, getattr(admin, "id", ""),
    )
    return AdminWalletAdjustResponse(**payload)


@router.post("/admin/wallet/grant-cosmetic", response_model=AdminGrantCosmeticResponse)
def admin_grant_cosmetic(
    body: AdminGrantCosmeticRequest,
    admin: UserModel = Depends(require_site_admin),
    db: Session = Depends(get_db),
) -> AdminGrantCosmeticResponse:
    """直接给用户发放一件时装（不扣币，自动加入衣柜）：补发 / 活动赠送 / 联调。"""
    try:
        payload = svc.admin_grant_cosmetic(
            db,
            body.user_id,
            body.cosmetic_id,
            operator_user_id=str(admin.id),
            note=body.note,
        )
    except FashionServiceError as exc:
        raise _http_error(exc)
    item = payload.get("item")
    return AdminGrantCosmeticResponse(
        ok=bool(payload.get("ok", True)),
        userId=str(payload.get("userId", "") or ""),
        cosmeticId=str(payload.get("cosmeticId", "") or ""),
        granted=bool(payload.get("granted", False)),
        item=StoreItem(**item) if item else None,
    )


# ── 包级下发（公开，mod 自动下发用） ────────────────────────────────────
#
# 客户端实现：net/PackCatalog.java（PACKS_PATH="/packs"）、net/PackDownloader.java。
# 硬性要求（mod docs/fashion-api.md §1/§2）：
#   - 清单必须 **200 直出 JSON**（客户端 Redirect.NEVER；404/501/405 会关闭本次会话的自动下发）
#   - 下载必须 **200 直出字节流**、Content-Length 精确、不 302 到 CDN
#   - ``publishedAt`` 是**毫秒时间戳 number**，不是 ISO-8601 字符串

@router.get("/packs", response_model=PacksResponse, response_model_exclude_none=True)
def list_packs(response: Response, db: Session = Depends(get_db)) -> PacksResponse:
    """时装包清单（公开可匿名读，客户端 10 分钟 TTL）。"""
    packs = svc.list_packs(db)
    # 客户端有 10 分钟本地 TTL，服务端给 5 分钟 max-age 即可（doc 建议值）
    response.headers["Cache-Control"] = "public, max-age=300"
    return PacksResponse(packs=[PackItem(**item) for item in packs])


@router.get("/packs/{pack_id}/{version}/download")
@router.head("/packs/{pack_id}/{version}/download")
def download_pack(
    pack_id: str,
    version: str,
    db: Session = Depends(get_db),
) -> FileResponse:
    """整包 zip 字节流（公开）。

    直接返回文件（``FileResponse`` 用 ``os.stat`` 填 Content-Length，精确且不缓冲），
    **绝不重定向**：客户端 ``HttpClient.Redirect.NEVER``，302 会被判定为下载失败。
    同时注册 HEAD：mod 文档 §8 的联调自测用 ``curl -sI``（HEAD）核对
    ``Content-Length`` / ``Content-Type`` / 有无 ``Location``，只注册 GET 会拿到 405。

    ⚠️ 2026-11-01 事故（§六.106）：**查完 DB 必须立刻 ``db.close()``**，再返回
    ``FileResponse``。FastAPI 把 ``yield`` 依赖（``get_db``）挂在 ``request_stack`` 上，
    而该栈是在 ``await response(...)``（= 文件流发完）之后才退出的 —— 只靠依赖收尾，
    一个 zip 的整个传输期间都会**独占一个 DB 连接**（大包能占几十秒）。
    这里显式提前关会话（Session.close() 幂等，依赖收尾再关一次无害），
    既保住事故修复，也保住 ``Depends(get_db)`` 的可注入性（测试能 override）。
    """
    try:
        pack = svc.get_pack_asset(db, pack_id, version)
    finally:
        db.close()  # 必须在返回 FileResponse 之前关掉
    if not pack:
        raise HTTPException(
            status_code=404,
            detail={"code": "pack_not_found", "message": "包不存在或已下架"},
        )
    root = svc.resolve_asset_root()
    try:
        path = svc.safe_join(root, pack.get("storagePath", ""))
    except FashionServiceError:
        logger.warning("fashion: 包存储路径非法 pack={} version={}", pack_id, version)
        raise HTTPException(
            status_code=404,
            detail={"code": "pack_not_found", "message": "包不存在或已下架"},
        )
    if not path.is_file():
        # DB 有记录但磁盘丢了（容器重建没挂持久卷）—— 明确 404 而不是 500，
        # 客户端会冷却 10 分钟后重试，不会把整个自动下发关掉
        logger.error(
            "fashion: 包文件缺失 pack={} version={} path={}", pack_id, version, path
        )
        raise HTTPException(
            status_code=404,
            detail={"code": "pack_not_found", "message": "包文件缺失，请联系管理员"},
        )
    headers = {
        # 清单里 sha256 不变则内容不变，可安全长缓存
        "Cache-Control": "public, max-age=3600",
        "ETag": f'"{pack.get("sha256", "")}"',
        "X-Content-Type-Options": "nosniff",
    }
    return FileResponse(
        str(path),
        media_type="application/zip",
        headers=headers,
    )


# ── 资产（公开，长缓存） ────────────────────────────────────────────────

@router.get("/assets/{asset_key:path}")
def get_asset(asset_key: str, db: Session = Depends(get_db)):
    """下载资产文件。

    公开可读：资产按 sha256 内容寻址、本身不含隐私；且 ``<img src>`` 无法带
    Authorization 头，网页衣柜预览需要匿名可读。响应带 immutable 长缓存。

    ⚠️ 2026-11-01 事故（§六.106）：**查完 DB 必须立刻 ``db.close()``**，再返回
    ``FileResponse``。FastAPI 0.139 把 ``yield`` 依赖挂在 ``request_stack`` 上，而该栈
    是在 ``await response(scope, receive, send)``（= FileResponse **整个文件流发完**）
    之后才退出的。于是每下载一个资产文件都会**独占一个 DB 连接直到传完**；
    时装页一次并发几十个资产 → 连接池（5+10）瞬间打满 → 全站 504。
    这里显式提前关会话，文件流期间不再持有连接；同时保留 ``Depends(get_db)``，
    测试仍能 override 注入临时库（事故修复最初把依赖换成手动 ``get_session_factory()``，
    代价是绕过了 DI：测试注入的临时库失效，只能连到真实库）。
    """
    try:
        asset = svc.resolve_asset_file(db, asset_key)
    finally:
        db.close()  # 必须在返回 FileResponse 之前关掉
    if not asset:
        raise HTTPException(
            status_code=404, detail={"code": "asset_not_found", "message": "资产不存在"}
        )
    path: Path = asset["path"]
    media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    headers = {
        "Cache-Control": "public, max-age=31536000, immutable",
        "ETag": asset.get("etag", ""),
        "X-Content-Type-Options": "nosniff",
    }
    return FileResponse(str(path), media_type=media_type, headers=headers)


# ── 设备码流程（网页绑定 MC 账号） ─────────────────────────────────────

@router.post("/device/start", response_model=DeviceStartResponse)
def device_start(
    principal: FashionPrincipal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> DeviceStartResponse:
    """mod 侧发起绑定：返回 6 位用户码给玩家看。"""
    identity = _resolve_identity(principal, db)
    # 限流：否则一个 mod 能无限生成设备码，把内存表（上限 2000）刷满、
    # 挤掉其他玩家的绑定码（等于 DoS）
    _check_device_start_rate(str(getattr(identity, "id", "")))
    record = start_device_flow(getattr(identity, "id", ""))
    from vmtools_next.config import get_config

    cfg = get_config().fashion
    return DeviceStartResponse(
        user_code=record.user_code,
        device_code=record.device_code,
        expires_in=max(0, int(record.expires_at - time.time())),
        interval=int(getattr(cfg, "device_poll_interval_seconds", 3) or 3),
    )


@router.post("/device/approve", response_model=DeviceApproveResponse)
def device_approve(
    body: DeviceApproveRequest,
    request: Request = None,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DeviceApproveResponse:
    """网页登录用户输入 6 位码（或 deviceCode），把该 MC 身份绑定到当前账号。"""
    user_id = str(getattr(user, "id", ""))
    source_ip = request.client.host if getattr(request, "client", None) else ""
    _check_device_approve_rate(f"{user_id}|{source_ip}")

    code = (body.user_code or "").strip() or body.device_code
    # 先做身份归属预检：approve_device_flow 会把「已批准」写进内存记录，
    # 若身份已绑到别的账号而先写后报 409，会让合法玩家再也无法完成绑定。
    record = find_device(code)
    if record is not None and record.expires_at > time.time():
        _assert_identity_bindable(db, record.identity_id, user_id)
    try:
        record = approve_device_flow(code, user_id)
        identity = _assert_identity_bindable(db, record.identity_id, user_id)
        svc.bind_identity_to_user(db, identity, user_id)
    except (FashionAuthError, FashionServiceError) as exc:
        raise _http_error(exc)

    return DeviceApproveResponse(
        ok=True,
        identity_id=getattr(identity, "id", ""),
        mc_uuid=getattr(identity, "mc_uuid", ""),
        mc_name=getattr(identity, "mc_name", ""),
    )


@router.get("/device/status", response_model=DeviceStatusResponse)
def device_status(
    device_code: str = Query(..., alias="deviceCode"),
    principal: FashionPrincipal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> DeviceStatusResponse:
    """mod 轮询绑定结果（approved 后即可在网页衣柜里操作）。

    ``deviceCode`` 是 camelCase（与文档 / mod 一致）；Python 形参保持 snake_case
    方便测试直调路由函数。

    ⚠️ 审计 v1.2 P3-7：``deviceCode`` 只能走 query——mod 端 ``FashionApi.java:155``
    固定拼 ``GET /device/status?deviceCode=...``，改成 POST body 会破坏客户端契约。
    因此这里保持 query，仅保证**服务端日志**不打印该值；代价是它可能进入 nginx
    access log（默认 combined 会记录 ``$request``）。有日志读权限的人可在 10 分钟
    有效期内用它轮询绑定状态（不能直接完成绑定），生产建议对 access log 做脱敏/限权。
    """
    identity = _resolve_identity(principal, db)
    try:
        payload = device_flow_status(device_code, getattr(identity, "id", ""))
    except FashionAuthError as exc:
        raise _http_error(exc)
    return DeviceStatusResponse(
        status=payload["status"],
        user_code=payload.get("userCode", ""),
        bound_user_id=payload.get("boundUserId"),
    )


# ── 我的身份 ────────────────────────────────────────────────────────────

@router.get("/identities/me", response_model=IdentityListResponse)
def my_identities(
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IdentityListResponse:
    """当前网页账号已绑定的 MC 身份列表（绑定页展示用）。"""
    rows = svc.list_identities_for_user(db, getattr(user, "id", ""))
    return IdentityListResponse(items=[IdentityOut(**svc.identity_to_dict(row)) for row in rows])


@router.delete("/identities/{identity_id}")
def unbind_identity(
    identity_id: str,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """解绑：只允许解绑自己名下的身份。"""
    identity = svc.get_identity(db, identity_id)
    if identity is None:
        raise HTTPException(
            status_code=404, detail={"code": "identity_not_found", "message": "身份不存在"}
        )
    if getattr(identity, "user_id", None) != str(getattr(user, "id", "")):
        raise HTTPException(
            status_code=403, detail={"code": "forbidden", "message": "只能解绑自己的身份"}
        )
    svc.unbind_identity(db, identity)
    return {"ok": True}


# ── 管理端：目录 ────────────────────────────────────────────────────────

@router.get("/admin/catalog", response_model=CatalogAdminResponse)
def admin_list_catalog(
    category: Optional[str] = None,
    pack_id: Optional[str] = None,
    _: UserModel = Depends(require_site_admin),
    db: Session = Depends(get_db),
) -> CatalogAdminResponse:
    """全部目录条目（含 disabled）。"""
    _validate_catalog_filters(category, pack_id)
    items = svc.list_catalog(
        db, enabled_only=False, category=category, pack_id=pack_id
    )
    return CatalogAdminResponse(items=[CatalogAdminItem(**item) for item in items])


@router.post("/admin/catalog", response_model=CatalogAdminItem)
def admin_create_catalog(
    body: CatalogUpsertRequest,
    _: UserModel = Depends(require_site_admin),
    db: Session = Depends(get_db),
) -> CatalogAdminItem:
    payload = body.model_dump(by_alias=True, exclude_none=True, exclude_unset=True)
    if not payload.get("cosmeticId"):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_cosmetic_id", "message": "cosmeticId 不能为空"},
        )
    try:
        item = svc.upsert_catalog_item(db, payload)
    except FashionServiceError as exc:
        raise _http_error(exc)
    logger.info("fashion admin catalog created cosmetic={}", payload.get("cosmeticId"))
    return CatalogAdminItem(**item)


@router.put("/admin/catalog/{cosmetic_id}", response_model=CatalogAdminItem)
def admin_update_catalog(
    cosmetic_id: str,
    body: CatalogUpsertRequest,
    _: UserModel = Depends(require_site_admin),
    db: Session = Depends(get_db),
) -> CatalogAdminItem:
    if svc.get_catalog_item(db, cosmetic_id) is None:
        raise HTTPException(
            status_code=404, detail={"code": "cosmetic_not_found", "message": "时装不存在"}
        )
    payload = body.model_dump(by_alias=True, exclude_none=True, exclude_unset=True)
    payload["cosmeticId"] = cosmetic_id
    try:
        item = svc.upsert_catalog_item(db, payload)
    except FashionServiceError as exc:
        raise _http_error(exc)
    logger.info("fashion admin catalog updated cosmetic={}", cosmetic_id)
    return CatalogAdminItem(**item)


@router.delete("/admin/catalog/{cosmetic_id}")
def admin_delete_catalog(
    cosmetic_id: str,
    _: UserModel = Depends(require_site_admin),
    db: Session = Depends(get_db),
) -> dict:
    deleted = svc.delete_catalog_item(db, cosmetic_id)
    logger.info("fashion admin catalog delete cosmetic={} ok={}", cosmetic_id, deleted)
    if not deleted:
        raise HTTPException(
            status_code=404, detail={"code": "cosmetic_not_found", "message": "时装不存在"}
        )
    return {"ok": True}


# ── 管理端：资产 ────────────────────────────────────────────────────────

_MAX_ASSET_MB = MAX_ASSET_BYTES // (1024 * 1024)
# 分块读上传：即使拿不到 file.size，也绝不会把超大文件整个读进内存
_UPLOAD_CHUNK = 1024 * 1024


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=413,
        detail={
            "code": "asset_too_large",
            "message": f"单个资产不能超过 {_MAX_ASSET_MB}MB",
        },
    )


async def _read_asset_upload(file: UploadFile) -> bytes:
    """读取上传内容，**分块读取**并在超过上限时立刻 413。

    Starlette 的 ``UploadFile.size`` 由 multipart 解析器按实际字节数填充，能拿到就先
    预检（一个字节都不读）；拿不到（老版本 / 异常实现）也不能 ``read()`` 到底 ——
    那样一个超大文件就能把 worker 的内存打爆。所以统一按 1MB 分块，超限即停。
    """
    declared_size = getattr(file, "size", None)
    if isinstance(declared_size, int) and declared_size > MAX_ASSET_BYTES:
        raise _too_large()

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_ASSET_BYTES:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/admin/assets", response_model=AssetListResponse)
def admin_list_assets(
    pack_id: Optional[str] = None,
    _: UserModel = Depends(require_site_admin),
    db: Session = Depends(get_db),
) -> AssetListResponse:
    items = svc.list_assets(db, pack_id)
    return AssetListResponse(items=[AssetOut(**item) for item in items])


@router.post("/admin/assets", response_model=AssetUploadResponse)
async def admin_upload_asset(
    file: UploadFile = File(...),
    pack_id: str = Form("default"),
    version: str = Form("1"),
    cosmetic_id: str = Form(""),
    _: UserModel = Depends(require_site_admin),
    db: Session = Depends(get_db),
) -> AssetUploadResponse:
    """上传资产（≤64MB，按 sha256 去重）。带 cosmeticId 时同步回填目录的 sha256/bytes。"""
    # 先校验 cosmeticId 再落盘：否则 404 时文件已经写进资产目录，留下孤儿文件
    target_row = None
    if cosmetic_id:
        target_row = svc.get_catalog_item(db, cosmetic_id)
        if target_row is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "cosmetic_not_found", "message": f"时装不存在：{cosmetic_id}"},
            )
    data = await _read_asset_upload(file)
    try:
        # 落盘（最大 64MB 写 + fsync）是**同步阻塞**操作：单 worker 部署下直接
        # 跑在事件循环里会把 socket.io 推送与其它请求一起卡住，所以丢进线程池。
        # 会话在等待期间不被事件循环使用，SQLite 连接已设 check_same_thread=False。
        asset = await run_in_threadpool(
            svc.store_asset,
            db,
            pack_id=pack_id,
            version=version,
            filename=file.filename or "asset.bin",
            data=data,
        )
    except FashionServiceError as exc:
        raise _http_error(exc)

    linked = ""
    if target_row is not None:
        target_row.sha256 = asset["sha256"]
        target_row.bytes = asset["bytes"]
        if not getattr(target_row, "asset_key", ""):
            target_row.asset_key = asset["storagePath"]
        db.commit()
        svc.invalidate_caches()
        linked = cosmetic_id
        logger.info(
            "fashion admin asset linked cosmetic={} sha256={} bytes={}",
            cosmetic_id, asset.get("sha256", ""), asset.get("bytes", 0),
        )

    return AssetUploadResponse(
        ok=True, asset=AssetOut(**asset), cosmetic_id=linked
    )


# ── 管理端：包级下发 ────────────────────────────────────────────────────

@router.post("/admin/packs", response_model=PackUploadResponse, response_model_exclude_none=True)
async def admin_upload_pack(
    file: UploadFile = File(...),
    pack_id: str = Form(...),
    version: str = Form(...),
    _: UserModel = Depends(require_site_admin),
    db: Session = Depends(get_db),
) -> PackUploadResponse:
    """上传（或覆盖）一个时装包 zip —— 发布后立即出现在 ``GET /packs``。

    上传时按 mod 契约 §2/§6.8.1 做静态校验：zip 结构安全（路径穿越/符号链接/
    加密/ZIP64/zip bomb/可执行文件/隐藏文件…）+ 根目录 ``pack.json`` 存在且
    ``id`` 与 packId 一致。坏包在这里被挡住，玩家端不会出现"安装失败"。
    """
    data = await _read_asset_upload(file)
    try:
        # 同上：zip 校验（可到 128MB）与落盘都是同步 CPU/IO，放线程池，别卡事件循环
        await run_in_threadpool(
            svc.store_asset,
            db,
            pack_id=pack_id,
            version=version,
            filename=file.filename or f"{pack_id}.zip",
            data=data,
            pack_zip=True,
        )
        entry = await run_in_threadpool(svc.pack_entry, db, pack_id, version)
    except FashionServiceError as exc:
        raise _http_error(exc)
    if entry is None:
        # 理论上不会发生（刚存完）：给出明确错误而不是返回空对象
        raise HTTPException(
            status_code=500,
            detail={"code": "pack_not_published", "message": "包已保存但未能生成清单项"},
        )
    logger.info(
        "fashion admin pack published id={} version={} bytes={}",
        entry.get("id", ""), entry.get("version", ""), entry.get("bytes", 0),
    )
    return PackUploadResponse(ok=True, pack=PackItem(**entry))


@router.delete("/admin/packs/{pack_id}/{version}")
def admin_delete_pack(
    pack_id: str,
    version: str,
    _: UserModel = Depends(require_site_admin),
    db: Session = Depends(get_db),
) -> dict:
    """下架一个包（从 ``/packs`` 移除；玩家本地已下载的包不受影响）。"""
    try:
        deleted = svc.delete_pack(db, pack_id, version)
    except FashionServiceError as exc:
        raise _http_error(exc)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={"code": "pack_not_found", "message": "包不存在或已下架"},
        )
    logger.info("fashion admin pack unpublished id={} version={}", pack_id, version)
    return {"ok": True}
