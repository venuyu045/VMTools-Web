"""标点中继 API（VMPing Fabric 客户端 mod）。

接口分组
--------
认证（匿名）::

    POST /api/ping/auth/challenge          申请一次性 serverId（TTL 120s，绑定来源 IP）
    POST /api/ping/auth/verify             反查 hasJoined 后签发 token（有效期 24h）

mod（``Authorization: Bearer <token>``，token ``aud="ping"``）::

    POST /api/ping/rooms/{roomId}/pings    发送标点（201）
    GET  /api/ping/rooms/{roomId}/pings    长轮询拉取（200，最多等 25s）

鉴权
----
复用 ``core/fashion_auth.py`` 的 serverId 挑战表与 hasJoined 校验（**不复制**那套
逻辑），只有 token 受众不同（``aud="ping"``）。ping token 的 ``sub`` 就是 ``mcUuid``：
中继是纯内存的，不需要身份表，因此本路由**不依赖 SQLAlchemy Session / 数据库**。

两套 token 靠 ``aud`` 隔离：fashion token 调 ping 接口 → 401 ``bad_token``，
ping token 调时装接口同理；``serverId`` 也记录用途，不能跨用途消费。

容量（默认值，``VMT_PING__*`` 可覆盖，详见 ``docs/后端实现.md``）
--------------------------------------------------------------
每房间存活标点 200 ｜ 标点 TTL 20s ｜ 房间空闲回收 60s ｜ 全局房间 1000 ｜
每房间长轮询等待者 64 ｜ 全局长轮询并发 5000 ｜ 每玩家 5 条 / 3s ｜
认证 challenge 每 IP 20 次 / 60s。

房间被回收后**重建时 ``seq`` 从 0/1 重新开始**（不是全局单调）：客户端用
``seq < 本地 lastSeq`` 判定「房间被回收」，重置游标并清空他人标点。

错误体
------
统一 ``{"detail": {"code": "...", "message": "..."}}``；429 / 503 额外带 ``Retry-After``（秒）。
业务路由严格用协议 §1 的 code（``unauthorized`` / ``bad_token`` / ``rate_limited`` /
``room_full`` / ``bad_request`` / ``server_error``），外加 v1 实现补充的
``server_busy``（503，全局长轮询并发打满）；认证接口沿用 ``fashion_auth`` 的细分 code
（``challenge_expired`` / ``not_joined`` / ``uuid_mismatch`` …），客户端按 HTTP 状态码
降级即可。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from loguru import logger

from vmtools_next.api.schemas.ping import (
    ChallengeRequest,
    ChallengeResponse,
    PingBatchResponse,
    PingOut,
    PingSendRequest,
    PingSendResponse,
    PlayerRef,
    VerifyRequest,
    VerifyResponse,
)
from vmtools_next.core import ping_relay as relay_core
from vmtools_next.core.fashion_auth import (
    AUDIENCE_PING,
    FashionAuthError,
    consume_challenge,
    create_challenge,
    decode_token,
    issue_token,
    verify_with_session_server,
)

router = APIRouter(prefix="/api/ping", tags=["ping"])
_bearer = HTTPBearer(auto_error=False)

#: 本组接口只接受 ``aud="ping"`` 的 token
TOKEN_AUDIENCE = AUDIENCE_PING


# ── 统一错误体 ──────────────────────────────────────────────────────────

def _error(
    code: str,
    message: str,
    status_code: int = 400,
    retry_after: Optional[int] = None,
) -> HTTPException:
    """构造 ``{"detail": {"code", "message"}}`` 形式的 HTTPException。

    ``headers`` 用属性赋值而不是构造参数：离线 stub 环境（``tests/_stub_env.py``）
    的 ``HTTPException`` 不接受 ``headers``，而真 FastAPI 的 ``HTTPException.headers``
    本来就是个普通属性（Starlette 渲染响应时读它），两种环境都能工作。
    """
    exc = HTTPException(status_code=status_code, detail={"code": code, "message": message})
    if retry_after is not None:
        try:
            exc.headers = {"Retry-After": str(max(1, int(retry_after)))}
        except Exception:  # pragma: no cover - 只可能出现在极端替身
            pass
    return exc


def _relay_error(exc: relay_core.PingRelayError) -> HTTPException:
    """中继业务错误 → HTTP（429 带 Retry-After）。"""
    return _error(exc.code, exc.message, exc.status_code, retry_after=exc.retry_after)


def _fashion_error(exc: FashionAuthError) -> HTTPException:
    """认证错误 → HTTP（沿用 fashion_auth 的细分 code，便于客户端/运维定位）。"""
    return _error(exc.code, exc.message, exc.status_code)


# ── 配置 / 中继实例 ────────────────────────────────────────────────────

def _ping_config() -> relay_core.PingConfig:
    """读 ``config.PingSettings``；读不到（离线测试 / 配置损坏）退化为内置默认值。"""
    settings = None
    try:
        from vmtools_next.config import get_config

        settings = getattr(get_config(), "ping", None)
    except Exception as exc:  # pragma: no cover - 仅异常路径
        logger.warning("ping: 读取配置失败，使用默认值：{}", exc)
    return relay_core.PingConfig.from_settings(settings)


_relay: Optional[relay_core.PingRelay] = None
_challenge_limiter: Optional[relay_core.SlidingWindowLimiter] = None
_state_lock = threading.Lock()


def get_relay() -> relay_core.PingRelay:
    """进程内单例中继（首次调用时按配置构造）。

    ⚠️ 纯内存、单进程：多 worker 部署时同一房间的玩家会落到不同进程，互相收不到
    标点（见 ``docs/后端实现.md``）。改配置需要重启进程。
    """
    global _relay
    if _relay is None:
        with _state_lock:
            if _relay is None:
                _relay = relay_core.PingRelay(_ping_config())
    return _relay


def set_relay(relay: Optional[relay_core.PingRelay]) -> None:
    """替换中继实例（单测注入假时钟 / 小容量；传 None = 下次按配置重建）。"""
    global _relay
    with _state_lock:
        _relay = relay


def get_challenge_limiter() -> relay_core.SlidingWindowLimiter:
    """challenge 限流器（防刷爆共用的挑战表；每 IP 20 次 / 60s）。"""
    global _challenge_limiter
    if _challenge_limiter is None:
        with _state_lock:
            if _challenge_limiter is None:
                cfg = _ping_config()
                _challenge_limiter = relay_core.SlidingWindowLimiter(
                    cfg.challenge_rate_limit, cfg.challenge_rate_window_seconds
                )
    return _challenge_limiter


def reset_state() -> None:
    """清空进程内状态（单测用；生产不要调用）。"""
    global _relay, _challenge_limiter
    with _state_lock:
        _relay = None
        _challenge_limiter = None


# ── 鉴权依赖 ────────────────────────────────────────────────────────────

@dataclass
class PingPrincipal:
    """调用者身份：直接来自 ping token 的 claims（中继不需要查库）。"""

    mc_uuid: str
    mc_name: str = ""


def get_ping_principal(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> PingPrincipal:
    """校验 ``Authorization: Bearer <token>``（``aud="ping"``）。"""
    token = getattr(credentials, "credentials", "") if credentials is not None else ""
    if not token:
        raise _error("unauthorized", "缺少 Authorization: Bearer token", 401)
    try:
        claims = decode_token(token, audience=TOKEN_AUDIENCE)
    except FashionAuthError as exc:
        if exc.code == "server_misconfigured":
            raise _error("server_error", "服务端未配置 JWT 签名密钥", 500)
        # 签名错 / 过期 / 受众不是 ping 一律 bad_token：客户端重新认证一次
        raise _error("bad_token", "token 无效或已过期，请重新认证", 401)
    try:
        mc_uuid = relay_core.normalize_uuid(claims.get("mcUuid"))
    except relay_core.PingRelayError:
        raise _error("bad_token", "token 缺少有效的 mcUuid", 401)
    return PingPrincipal(
        mc_uuid=mc_uuid, mc_name=str(claims.get("mcName") or "").strip()[:32]
    )


# ── 认证：challenge / verify ────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "") or "")


def _check_challenge_rate(source_ip: str) -> None:
    try:
        get_challenge_limiter().check(
            source_ip or "unknown", message="认证请求过于频繁，请稍后再试"
        )
    except relay_core.PingRelayError as exc:
        raise _relay_error(exc)


@router.post("/auth/challenge", response_model=ChallengeResponse)
def auth_challenge(body: ChallengeRequest, request: Request) -> ChallengeResponse:
    """生成一次性 serverId（TTL 120s，绑定来源 IP）。

    挑战表与 VMFashion **共用**（同一个 ``core.fashion_auth``），只是用途标记为
    ``ping`` —— 这个 serverId 只能在本组 ``/verify`` 消费。
    """
    source_ip = _client_ip(request)
    _check_challenge_rate(source_ip)
    try:
        challenge = create_challenge(
            body.mc_uuid,
            body.mc_name,
            body.auth_server,
            source_ip=source_ip,
            audience=TOKEN_AUDIENCE,
        )
    except FashionAuthError as exc:
        raise _fashion_error(exc)
    return ChallengeResponse(
        server_id=challenge.server_id,
        expires_in=challenge.ttl_remaining,
        auth_type=challenge.auth_type,
    )


@router.post("/auth/verify", response_model=VerifyResponse)
async def auth_verify(body: VerifyRequest, request: Request) -> VerifyResponse:
    """消费 serverId → 反查会话服务器 hasJoined → 签发 ``aud="ping"`` 的 token。"""
    source_ip = _client_ip(request)
    try:
        challenge = consume_challenge(
            body.server_id,
            body.mc_uuid,
            body.mc_name,
            source_ip=source_ip,
            audience=TOKEN_AUDIENCE,
        )
        info = await verify_with_session_server(challenge)
        token, ttl = issue_token(
            info["mc_uuid"],
            mc_uuid=info["mc_uuid"],
            mc_name=info["mc_name"],
            audience=TOKEN_AUDIENCE,
            ttl_seconds=_ping_config().token_ttl_seconds,
        )
    except FashionAuthError as exc:
        logger.warning(
            "ping verify failed uuid={} code={}",
            getattr(body, "mc_uuid", "?"),
            getattr(exc, "code", "?"),
        )
        raise _fashion_error(exc)

    logger.info(
        "ping verify ok uuid={} name={} type={}",
        info["mc_uuid"], info["mc_name"], info["auth_type"],
    )
    return VerifyResponse(
        token=token,
        expires_at=int(time.time() * 1000) + ttl * 1000,
        mc_uuid=info["mc_uuid"],
        mc_name=info["mc_name"],
        auth_type=info["auth_type"],
    )


# ── 标点：发送 / 长轮询 ────────────────────────────────────────────────

def _to_ping_out(ping: relay_core.Ping) -> PingOut:
    """中继实体 → 协议 §5 的响应模型（字段名与协议一一对应）。"""
    return PingOut(
        seq=ping.seq,
        id=ping.id,
        player=PlayerRef(uuid=ping.player_uuid, name=ping.player_name),
        dimension=ping.dimension,
        x=ping.x,
        y=ping.y,
        z=ping.z,
        kind=ping.kind,
        target_uuid=ping.target_uuid,
        target_name=ping.target_name,
        server_time=ping.server_time,
        expires_at=ping.expires_at,
    )


@router.post("/rooms/{room_id}/pings", response_model=PingSendResponse, status_code=201)
async def send_ping(
    room_id: str,
    body: PingSendRequest,
    principal: PingPrincipal = Depends(get_ping_principal),
    version: Optional[str] = Header(default=None, alias="X-VMPing-Version"),
) -> PingSendResponse:
    """发送一条标点（201）。

    校验：坐标有限且在 ``|x|,|z| ≤ 3e7``、``|y| ≤ 2048`` 内；``kind`` 白名单；
    ``dimension`` ≤128 字符；``targetName`` ≤64 字符；``targetUuid`` 合法 UUID。
    限流（每玩家 5 条 / 3s）与房间容量（200）由中继核心判定。
    """
    try:
        ping = get_relay().publish(
            room_id=room_id,
            player_uuid=principal.mc_uuid,
            player_name=principal.mc_name,
            dimension=body.dimension,
            x=body.x,
            y=body.y,
            z=body.z,
            kind=body.kind,
            target_uuid=body.target_uuid,
            target_name=body.target_name,
            client_time=body.client_time,
        )
    except relay_core.PingRelayError as exc:
        raise _relay_error(exc)
    if version or body.client_time is not None:
        # 协议 §1：版本头只记日志，不做强校验；clientTime 同样只做诊断
        logger.debug(
            "ping publish version={} clientTime={} room={} seq={} player={}",
            version or "-", body.client_time, ping.seq, ping.player_name,
        )
    return PingSendResponse(
        id=ping.id,
        seq=ping.seq,
        server_time=ping.server_time,
        expires_at=ping.expires_at,
    )


@router.get("/rooms/{room_id}/pings", response_model=PingBatchResponse)
async def fetch_pings(
    room_id: str,
    since: Optional[int] = Query(default=None, alias="since"),
    timeout: Optional[float] = Query(default=None, alias="timeout"),
    dimension: Optional[str] = Query(default=None, alias="dimension"),
    principal: PingPrincipal = Depends(get_ping_principal),
) -> PingBatchResponse:
    """长轮询拉取标点（200）。

    - ``since``：只返回 ``seq > since`` 的存活标点；缺省 0（重连补齐拿全部）
    - ``timeout``：最多等多少秒；缺省 20，上限 25，0 = 立即返回
    - ``dimension``：可选，按维度过滤（不同维度共用房间）
    - 返回的 ``seq`` 是房间当前最大序号，客户端下次用它做 ``since``
    """
    try:
        batch = await get_relay().wait_for_pings(
            room_id,
            since if since is not None else 0,
            timeout,
            dimension,
        )
    except relay_core.PingRelayError as exc:
        raise _relay_error(exc)
    return PingBatchResponse(
        seq=batch.seq,
        server_time=batch.server_time,
        pings=[_to_ping_out(ping) for ping in batch.pings],
    )
