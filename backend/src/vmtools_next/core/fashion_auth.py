"""时装系统身份认证 —— Mojang 正版 / Yggdrasil 皮肤站 serverId 校验。

为什么只有两条路径
------------------
目标 MC 服务器同时开「正版登录」与「皮肤站登录（authlib-injector / Yggdrasil）」，
**没有离线玩家**，所以这里不实现「离线 UUID 直接发 token」的兜底 —— 那等于任何
人填一个 UUID 就能冒用他人时装配置。

认证流程（与 mod 约定，详见 ``docs/fashion-api.md``）
----------------------------------------------------
1. mod 调 :func:`create_challenge` → 拿到一次性 ``serverId``（TTL 120s）
2. mod 用玩家自己的 accessToken 调会话服务器
   ``POST /session/minecraft/join``（正版）或皮肤站同名端点
3. mod 调 :func:`consume_challenge` + :func:`verify_with_session_server`
   → 后端 ``GET .../hasJoined?username=&serverId=`` 反查，**比对返回的 UUID**
   与 mod 声明的 UUID 是否一致，一致才发 token

安全约束
--------
- ``serverId``：一次性（消费即删）、带 TTL、绑定请求来源 IP（防跨源重放）
- ``hasJoined`` 结果必须比对 UUID（只比用户名可被同名账号冒用）
- 皮肤站地址只允许使用配置白名单内的 base；客户端上报的 ``authServer`` 仅当
  落在白名单内才采用，否则忽略 —— 防 SSRF / 防把 token 送给攻击者域名
- 所有对外 HTTP 调用 5s 超时；调用失败返回结构化错误，不抛 500
- 挑战与 token 都带**用途**（``audience``）：``fashion`` / ``ping`` 共用同一套挑战表
  与 hasJoined 校验；``serverId`` 不能跨用途消费，token 默认按用途隔离
  （唯一例外：``/api/ping`` 的**业务路由**接受 ``ping`` 与 ``fashion`` 两种受众 ——
  VMFashion 2.0 合订版复用时装 token，见 :func:`decode_token_any`）。

模块不 import ``jwt`` / ``httpx`` 于顶层：两者在离线测试环境没有 stub，且延迟
导入可以让 import 冒烟在无依赖机器上通过（与 ``api/deps.py`` 的写法一致）。
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

# 直接用 loguru 全局 logger（infra.logging 的 get_logger 返回 bind 后的对象，
# 离线 stub 环境下为 None；这里只需要能记日志）
from loguru import logger

# ── 常量 ────────────────────────────────────────────────────────────────

AUTH_TYPE_MOJANG = "mojang"
AUTH_TYPE_YGGDRASIL = "yggdrasil"

# token 受众（JWT ``aud``）。同一套挑战表 / hasJoined 校验被多个 mod 复用，
# 靠 aud 区分用途：ping token 不能调时装接口；时装 token 可调 /api/ping 业务路由
# （VMFashion 2.0 合订版，见 decode_token_any）。
AUDIENCE_FASHION = "fashion"
AUDIENCE_PING = "ping"

# MC 用户名：3-16 位字母/数字/下划线（正版与主流皮肤站一致）
_MC_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
_UUID_DASHED_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")

# 用户码字母表：去掉容易看错的 I / O / 0 / 1
USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
USER_CODE_LENGTH = 6

# 内存表上限（防被刷爆内存）；超出后先按 TTL 清理，仍超出则淘汰最旧的
_MAX_CHALLENGES = 5000
_MAX_DEVICE_CODES = 2000


class FashionAuthError(Exception):
    """认证失败：``code`` 供 mod 做分支处理，``status_code`` 给 HTTP 层用。"""

    def __init__(self, code: str, message: str = "", status_code: int = 400):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.status_code = status_code


# ── 规范化工具 ──────────────────────────────────────────────────────────

def normalize_uuid(value: Any) -> str:
    """把 UUID 规范化为「带连字符的小写」形式。

    接受 ``AABBCCDDEEFF00112233445566778899``（32 位无连字符）与
    ``AABBCCDD-EEFF-0011-2233-445566778899``（大小写任意）。

    Raises:
        FashionAuthError: 不是合法 UUID（``code="invalid_uuid"``）。
    """
    raw = str(value or "").strip().lower()
    if not raw:
        raise FashionAuthError("invalid_uuid", "缺少 mcUuid")
    compact = raw.replace("-", "")
    if _UUID_DASHED_RE.match(raw):
        return raw
    if _UUID_HEX_RE.match(compact):
        return (
            f"{compact[0:8]}-{compact[8:12]}-{compact[12:16]}-"
            f"{compact[16:20]}-{compact[20:32]}"
        )
    raise FashionAuthError("invalid_uuid", f"mcUuid 格式非法：{value!r}")


def is_valid_uuid(value: Any) -> bool:
    try:
        normalize_uuid(value)
        return True
    except FashionAuthError:
        return False


def normalize_mc_name(value: Any) -> str:
    name = str(value or "").strip()
    if not _MC_NAME_RE.match(name):
        raise FashionAuthError("invalid_mc_name", "mcName 需为 3-16 位字母/数字/下划线")
    return name


def normalize_auth_server(url: Any) -> str:
    """规范化皮肤站 base URL（``https://host/api/yggdrasil``）。

    只允许 https（本机回环地址允许 http 便于开发）。拒绝 userinfo / query /
    fragment，避免 ``https://good@evil.com`` 这类解析歧义带来的 SSRF。

    Raises:
        FashionAuthError: ``code="invalid_auth_server"``。
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:  # 端口非法等
        raise FashionAuthError("invalid_auth_server", f"authServer 非法：{raw}") from exc

    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        raise FashionAuthError("invalid_auth_server", f"authServer 非法：{raw}")
    if parsed.username or parsed.password:
        raise FashionAuthError("invalid_auth_server", "authServer 不允许包含用户信息")
    if parsed.query or parsed.fragment:
        raise FashionAuthError("invalid_auth_server", "authServer 不允许包含查询串/锚点")
    if scheme == "http" and host not in ("127.0.0.1", "localhost", "::1"):
        raise FashionAuthError("invalid_auth_server", "authServer 必须使用 https")

    path = (parsed.path or "").rstrip("/")
    port_part = f":{port}" if port else ""
    # urlsplit().hostname 会把 IPv6 的方括号剥掉，重建时必须补回，否则
    # http://[::1]:8080 会变成非法的 http://::1:8080
    host_part = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host_part}{port_part}{path}"


def normalize_user_code(code: Any) -> str:
    """用户码规范化：去空格/连字符并转大写（``ab-12cd`` → ``AB12CD``）。"""
    raw = str(code or "").strip().upper()
    return re.sub(r"[\s\-_]", "", raw)


def mask_user_code(code: Any) -> str:
    """日志脱敏：6 位用户码本身是「绑定凭据」，日志里只留前 2 位。"""
    raw = normalize_user_code(code)
    return f"{raw[:2]}***" if raw else ""


def _safe_auth_server_for_log(value: Any) -> str:
    """日志脱敏：绝不把带 userinfo/query 的原始 authServer 写进日志。"""
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        if parsed.hostname:
            return f"{parsed.scheme or '?'}://{parsed.hostname}"
    except Exception:
        pass
    return raw[:32]


# ── 配置访问 ────────────────────────────────────────────────────────────

def _settings(settings: Any = None) -> Any:
    """取 FashionSettings：显式传入优先（测试用），否则读全局配置。

    读不到配置时退化为默认值（宁可认证接口返回业务错误，也不要 500）。
    """
    if settings is not None:
        return settings
    try:
        from vmtools_next.config import get_config

        return get_config().fashion
    except Exception as exc:  # pragma: no cover - 仅异常路径
        logger.warning("fashion: 读取配置失败，使用默认值：{}", exc)
        from vmtools_next.config import FashionSettings

        return FashionSettings()


def allowed_auth_servers(settings: Any = None) -> list[str]:
    """配置里的皮肤站白名单（已规范化；非法项跳过并告警）。"""
    cfg = _settings(settings)
    out: list[str] = []
    for base in getattr(cfg, "yggdrasil_bases", []) or []:
        try:
            norm = normalize_auth_server(base)
        except FashionAuthError:
            logger.warning("fashion: 忽略非法 yggdrasil base 配置 {}", base)
            continue
        if norm and norm not in out:
            out.append(norm)
    return out


def pick_auth_server(auth_server: Any, settings: Any = None) -> tuple[str, str]:
    """决定 ``auth_type`` 与要使用的皮肤站 base。

    - 客户端上报了 ``authServer`` 且**落在白名单内** → ``("yggdrasil", base)``
    - 上报了但不在白名单 / 格式非法 → 仍按 yggdrasil 处理，但 base 置空
      （后续只会依次尝试配置的 bases；绝不请求客户端给的地址）
    - 未上报 → ``("mojang", "")``
    """
    raw = str(auth_server or "").strip()
    if not raw:
        return AUTH_TYPE_MOJANG, ""
    try:
        norm = normalize_auth_server(raw)
    except FashionAuthError as exc:
        # 格式非法属于客户端错误（docs §8 invalid_auth_server），不能静默降级；
        # 只有"格式合法但不在白名单"才按设计忽略。
        logger.warning("fashion: 客户端上报的 authServer 非法：{}", _safe_auth_server_for_log(raw))
        raise FashionAuthError(
            "invalid_auth_server", "authServer 必须是 https（本机回环除外）且不含用户信息"
        ) from exc
    if norm in allowed_auth_servers(settings):
        return AUTH_TYPE_YGGDRASIL, norm
    logger.warning("fashion: authServer 不在白名单内，已忽略：{}", norm)
    return AUTH_TYPE_YGGDRASIL, ""


def has_joined_url(base: str, auth_type: str) -> str:
    """会话服务器的 hasJoined 端点。

    - Mojang：``{base}/session/minecraft/hasJoined``
    - Yggdrasil（皮肤站）：``{base}/sessionserver/session/minecraft/hasJoined``
    """
    base = (base or "").rstrip("/")
    if auth_type == AUTH_TYPE_YGGDRASIL:
        return f"{base}/sessionserver/session/minecraft/hasJoined"
    return f"{base}/session/minecraft/hasJoined"


def has_joined_candidates(challenge: "Challenge", settings: Any = None) -> list[tuple[str, str, str]]:
    """返回要依次尝试的 ``(url, auth_type, base)``。

    主认证族优先，另一族作为兜底（跨族探测只发 serverId + username，不含任何
    凭据，失败也只是 204，成本可控）。去重后顺序稳定。
    """
    cfg = _settings(settings)
    mojang_base = normalize_auth_server(getattr(cfg, "mojang_session_server", "")) or (
        "https://sessionserver.mojang.com"
    )
    ygg_bases = allowed_auth_servers(cfg)

    primary: list[tuple[str, str]] = []
    secondary: list[tuple[str, str]] = []
    if challenge.auth_type == AUTH_TYPE_YGGDRASIL:
        if challenge.auth_server:
            primary.append((challenge.auth_server, AUTH_TYPE_YGGDRASIL))
        primary.extend((b, AUTH_TYPE_YGGDRASIL) for b in ygg_bases)
        secondary.append((mojang_base, AUTH_TYPE_MOJANG))
    else:
        primary.append((mojang_base, AUTH_TYPE_MOJANG))
        secondary.extend((b, AUTH_TYPE_YGGDRASIL) for b in ygg_bases)

    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for base, auth_type in primary + secondary:
        url = has_joined_url(base, auth_type)
        if url in seen:
            continue
        seen.add(url)
        out.append((url, auth_type, base))
    return out


# ── challenge（一次性 serverId） ────────────────────────────────────────

@dataclass
class Challenge:
    """一次认证握手。"""

    server_id: str
    mc_uuid: str
    mc_name: str
    auth_type: str
    auth_server: str
    source_ip: str
    created_at: float
    expires_at: float
    #: 该挑战签发时声明的用途（token 受众）。挑战表是 fashion / ping 共用的，
    #: 记下用途后「ping 的 serverId」就不能拿去换 fashion token（反之亦然）。
    audience: str = AUDIENCE_FASHION

    @property
    def ttl_remaining(self) -> int:
        return max(0, int(self.expires_at - time.time()))


_CHALLENGES: dict[str, Challenge] = {}
_CHALLENGE_LOCK = threading.Lock()


def _prune_challenges(now: float) -> None:
    expired = [k for k, v in _CHALLENGES.items() if v.expires_at <= now]
    for key in expired:
        _CHALLENGES.pop(key, None)


def create_challenge(
    mc_uuid: Any,
    mc_name: Any,
    auth_server: Any = None,
    *,
    source_ip: str = "",
    settings: Any = None,
    audience: str = AUDIENCE_FASHION,
) -> Challenge:
    """生成一次性 serverId 并缓存（TTL 见配置，默认 120s）。

    ``audience`` 只记录用途（默认 ``fashion``）；VMPing 传 ``ping``，
    消费时必须带同一个值，防止跨用途重放。
    """
    cfg = _settings(settings)
    uuid_norm = normalize_uuid(mc_uuid)
    name_norm = normalize_mc_name(mc_name)
    auth_type, base = pick_auth_server(auth_server, cfg)
    ttl = int(getattr(cfg, "challenge_ttl_seconds", 120) or 120)
    now = time.time()

    challenge = Challenge(
        server_id=secrets.token_hex(16),
        mc_uuid=uuid_norm,
        mc_name=name_norm,
        auth_type=auth_type,
        auth_server=base,
        source_ip=(source_ip or "").strip(),
        created_at=now,
        expires_at=now + ttl,
        audience=str(audience or AUDIENCE_FASHION),
    )
    with _CHALLENGE_LOCK:
        _prune_challenges(now)
        if len(_CHALLENGES) >= _MAX_CHALLENGES:
            # 仍超限：淘汰最早过期的若干条（fail-safe，不影响正常握手）
            for key in sorted(_CHALLENGES, key=lambda k: _CHALLENGES[k].expires_at)[: _MAX_CHALLENGES // 10 or 1]:
                _CHALLENGES.pop(key, None)
        _CHALLENGES[challenge.server_id] = challenge
    logger.info(
        "{} challenge issued uuid={} name={} type={} ttl={}s",
        challenge.audience, uuid_norm, name_norm, auth_type, ttl,
    )
    return challenge


def consume_challenge(
    server_id: Any,
    mc_uuid: Any,
    mc_name: Any,
    *,
    source_ip: str = "",
    settings: Any = None,
    audience: str = AUDIENCE_FASHION,
) -> Challenge:
    """校验并**消费** challenge（一次性）。

    依次校验：存在 → 未过期 → 未被使用 → 用途一致 → UUID/用户名一致 → 来源 IP 一致。
    任一失败都会抛 :class:`FashionAuthError`（消费失败时保留记录，除过期/已用）。
    """
    sid = str(server_id or "").strip()
    if not sid:
        raise FashionAuthError("challenge_not_found", "缺少 serverId")
    # 先校验入参格式：非法 UUID/用户名不应消耗掉合法 serverId
    uuid_norm = normalize_uuid(mc_uuid)
    name_norm = normalize_mc_name(mc_name)
    now = time.time()
    with _CHALLENGE_LOCK:
        challenge = _CHALLENGES.get(sid)
        if challenge is None:
            raise FashionAuthError("challenge_not_found", "serverId 不存在或已被使用")
        if challenge.expires_at <= now:
            _CHALLENGES.pop(sid, None)
            raise FashionAuthError("challenge_expired", "serverId 已过期，请重新发起认证")
        if uuid_norm != challenge.mc_uuid or name_norm.lower() != challenge.mc_name.lower():
            raise FashionAuthError(
                "challenge_mismatch", "serverId 与本次认证的账号不匹配", status_code=403
            )
        # 用途隔离：ping 的 serverId 不能拿去换 fashion token（反之亦然）
        if challenge.audience != str(audience or AUDIENCE_FASHION):
            raise FashionAuthError(
                "challenge_mismatch", "serverId 与本次认证的用途不匹配", status_code=403
            )
        if challenge.source_ip and source_ip and challenge.source_ip != source_ip:
            raise FashionAuthError(
                "source_mismatch", "serverId 与请求来源不匹配", status_code=403
            )
        _CHALLENGES.pop(sid, None)  # 一次性：全部校验通过后原子消费
    return challenge


# ── hasJoined 校验 ──────────────────────────────────────────────────────

async def _fetch_json(url: str, timeout: float) -> tuple[int, Optional[dict]]:
    """GET 一个 JSON 端点，返回 ``(status, payload)``。

    204/非 200 一律返回 ``(status, None)``；网络异常向上抛由调用方降级。

    ``follow_redirects=False``：hasJoined 只发 serverId + username，但仍不该被
    302 带到任意主机；``trust_env=False``：忽略环境里的 HTTP(S)_PROXY —— 否则
    「向会话服务器反查」会被一个环境变量劫持到代理上（可用性 + 隐私风险）。
    """
    import httpx

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, trust_env=False
    ) as client:
        resp = await client.get(url)
        status = int(getattr(resp, "status_code", 0) or 0)
        if status != 200:
            return status, None
        try:
            data = resp.json()
        except Exception:
            return status, None
        return status, data if isinstance(data, dict) else None


async def verify_with_session_server(
    challenge: Challenge,
    *,
    settings: Any = None,
    fetch: Any = None,
) -> dict:
    """向会话服务器反查 hasJoined，返回确认后的身份信息。

    Returns:
        ``{"mc_uuid", "mc_name", "auth_type", "auth_server"}``

    Raises:
        FashionAuthError: ``not_joined`` / ``uuid_mismatch`` / ``upstream_unavailable``
    """
    cfg = _settings(settings)
    timeout = float(getattr(cfg, "http_timeout_seconds", 5.0) or 5.0)
    fetcher = fetch or _fetch_json

    candidates = has_joined_candidates(challenge, cfg)
    responded = False
    last_error: Optional[str] = None

    for url, auth_type, base in candidates:
        query = f"username={challenge.mc_name}&serverId={challenge.server_id}"
        full_url = f"{url}?{query}"
        try:
            status, payload = await fetcher(full_url, timeout)
        except Exception as exc:  # 超时 / 连接失败 → 降级到下一个候选
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("fashion hasJoined 调用失败 {}: {}", base or url, last_error)
            continue

        if status >= 500:
            # 上游故障不能当"未加入"：否则客户端会误导玩家反复重试 join
            last_error = f"upstream_status_{status}"
            logger.warning("fashion hasJoined 上游返回 {}: {}", status, base or url)
            continue
        if status != 200 or not payload:
            responded = True
            continue

        responded = True
        returned_uuid = payload.get("id")
        try:
            returned_norm = normalize_uuid(returned_uuid)
        except FashionAuthError:
            logger.warning("fashion hasJoined 返回了非法 UUID：{!r}", returned_uuid)
            continue

        if returned_norm != challenge.mc_uuid:
            # 关键安全校验：只比用户名可被同名账号冒用，必须比 UUID
            logger.warning(
                "fashion hasJoined UUID 不匹配：声明 {} 实际 {}", challenge.mc_uuid, returned_norm
            )
            raise FashionAuthError(
                "uuid_mismatch", "会话服务器返回的 UUID 与声明的 mcUuid 不一致", status_code=403
            )

        return {
            "mc_uuid": returned_norm,
            "mc_name": str(payload.get("name") or challenge.mc_name),
            "auth_type": auth_type,
            # 模型约定：正版身份的 auth_server 为空串，只有皮肤站才记录 base
            "auth_server": base if auth_type == AUTH_TYPE_YGGDRASIL else "",
        }

    if not responded:
        raise FashionAuthError(
            "upstream_unavailable",
            "认证服务暂时不可用，请稍后重试",
            status_code=503,
        )
    raise FashionAuthError(
        "not_joined",
        "会话服务器未确认该玩家已加入（请先在客户端完成 join 再调用 verify）",
        status_code=403,
    )


# ── Token ───────────────────────────────────────────────────────────────

def _jwt_params(secret: Optional[str] = None) -> tuple[str, str]:
    if secret:
        return secret, "HS256"
    try:
        from vmtools_next.config import get_config

        cfg = get_config()
        return cfg.server.secret_key, cfg.server.jwt_algorithm
    except Exception as exc:  # 未配置密钥 → 不静默用弱密钥
        raise FashionAuthError(
            "server_misconfigured", "服务端未配置 JWT 签名密钥", status_code=500
        ) from exc


def issue_token(
    identity_id: str,
    *,
    mc_uuid: str = "",
    mc_name: str = "",
    settings: Any = None,
    secret: Optional[str] = None,
    audience: str = AUDIENCE_FASHION,
    ttl_seconds: Optional[int] = None,
) -> tuple[str, int]:
    """签发 mod 专用 JWT，返回 ``(token, ttl_seconds)``。

    ``audience`` 默认 ``fashion``（VMFashion 的 token）；VMPing 传 ``ping``，
    两套 token 靠 ``aud`` 隔离，签名密钥相同也不会被另一套接口接受。
    ``ttl_seconds`` 缺省取配置 ``token_ttl_seconds``（默认 24h）。
    """
    import jwt

    cfg = _settings(settings)
    if ttl_seconds is None:
        ttl = int(getattr(cfg, "token_ttl_seconds", 86400) or 86400)
    else:
        ttl = int(ttl_seconds)
    aud = str(audience or AUDIENCE_FASHION)
    key, alg = _jwt_params(secret)
    now = int(time.time())
    payload = {
        "sub": identity_id,
        "aud": aud,
        "typ": aud,
        "iat": now,
        "exp": now + ttl,
        "mcUuid": mc_uuid,
        "mcName": mc_name,
    }
    return jwt.encode(payload, key, algorithm=alg), ttl


def decode_token(
    token: str,
    *,
    settings: Any = None,
    secret: Optional[str] = None,
    audience: str = AUDIENCE_FASHION,
) -> dict:
    """校验 mod token 并返回 claims（默认 ``aud="fashion"``）。

    Raises:
        FashionAuthError: ``invalid_token``（签名错 / 过期 / 受众不符）
    """
    import jwt

    aud = str(audience or AUDIENCE_FASHION)
    key, alg = _jwt_params(secret)
    try:
        claims = jwt.decode(token, key, algorithms=[alg], audience=aud)
    except Exception as exc:
        raise FashionAuthError("invalid_token", "token 无效或已过期", status_code=401) from exc
    if not isinstance(claims, dict) or not claims.get("sub"):
        raise FashionAuthError("invalid_token", "token 缺少 sub", status_code=401)
    return claims


def decode_token_any(
    token: str,
    *,
    audiences: Iterable[str],
    settings: Any = None,
    secret: Optional[str] = None,
) -> dict:
    """按顺序尝试多个受众解码，返回**第一个**通过的 claims。

    用于「同一组业务路由接受多种用途 token」：``/api/ping`` 的业务路由同时接受
    ``aud="ping"``（独立 VMPing 0.1.0）与 ``aud="fashion"``（VMFashion 2.0 合订版
    复用时装 token）。签名密钥相同，靠 ``aud`` 区分用途，因此逐个尝试即可。

    全部受众都失败时统一抛 ``invalid_token``（401），不向调用方暴露「是哪个受众
    不对」；但 ``server_misconfigured`` 这类**与 token 无关**的错误原样上抛 ——
    多受众尝试不能把「服务端没配密钥」吞成「客户端 token 过期」。

    Args:
        token: ``Authorization: Bearer`` 里的 JWT 原文。
        audiences: 允许的受众，按尝试顺序（空项与重复项自动忽略）。
        settings / secret: 语义与 :func:`decode_token` 一致（``secret`` 优先）。

    Raises:
        FashionAuthError: ``invalid_token``（签名错 / 过期 / 受众都不匹配）。
    """
    candidates: list[str] = []
    for audience in audiences or ():
        aud = str(audience or "").strip()
        if aud and aud not in candidates:
            candidates.append(aud)

    last_error: Optional[FashionAuthError] = None
    for aud in candidates:
        try:
            return decode_token(token, settings=settings, secret=secret, audience=aud)
        except FashionAuthError as exc:
            if exc.code != "invalid_token":
                raise
            last_error = exc
    raise FashionAuthError(
        "invalid_token", "token 无效或已过期", status_code=401
    ) from last_error


def decode_web_token(token: str) -> Optional[str]:
    """校验网页 JWT（``sub`` = vmtools user id），返回 user_id 或 None。

    只用于「网页端也能调 loadout 接口」这一条路径；失败返回 None（不抛），
    由路由层统一按 401 处理。

    安全：显式拒绝带 ``aud`` 的 token。网页主 token 不带 aud；带 ``aud`` 的是
    viewer（``aud=viewer``）/ fashion（``aud=fashion``）等其他用途的 token，
    绝不能因为签名相同就被当成网页登录态复用。这里用 ``verify_aud=False``
    取出 claims 后自己判断，避免依赖 PyJWT 版本对「无 audience 参数」的隐式行为。
    """
    try:
        import jwt

        from vmtools_next.config import get_config

        cfg = get_config()
        claims = jwt.decode(
            token,
            cfg.server.secret_key,
            algorithms=[cfg.server.jwt_algorithm],
            options={"verify_aud": False},
        )
    except Exception:
        return None
    if not isinstance(claims, dict):
        return None
    aud = claims.get("aud")
    if aud is not None and aud != "web":
        return None
    return claims.get("sub")


# ── 设备码（网页绑定 MC 账号） ──────────────────────────────────────────

@dataclass
class DeviceCode:
    """设备码：mod 展示 ``user_code``，网页登录用户输入后完成绑定。"""

    device_code: str
    user_code: str
    identity_id: str
    created_at: float
    expires_at: float
    approved_user_id: Optional[str] = None
    approved_at: Optional[float] = None
    extra: dict = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.approved_user_id:
            return "approved"
        if time.time() >= self.expires_at:
            return "expired"
        return "pending"


_DEVICE_CODES: dict[str, DeviceCode] = {}
_DEVICE_LOCK = threading.Lock()
# 已批准的设备码在过期后再留一段时间，让 mod 的轮询能拿到 approved
# （玩家在最后一秒批准、mod 下一轮才轮询的情况）；之后清掉防内存缓慢增长
_DEVICE_APPROVED_GRACE_SECONDS = 600


def _prune_device_codes(now: float) -> None:
    for key in [
        k
        for k, v in _DEVICE_CODES.items()
        if v.expires_at <= now
        and (not v.approved_user_id or now >= v.expires_at + _DEVICE_APPROVED_GRACE_SECONDS)
    ]:
        _DEVICE_CODES.pop(key, None)


def _generate_user_code() -> str:
    return "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))


def start_device_flow(identity_id: str, *, settings: Any = None) -> DeviceCode:
    """为某个已认证身份生成设备码（mod 调用）。"""
    cfg = _settings(settings)
    ttl = int(getattr(cfg, "device_code_ttl_seconds", 600) or 600)
    now = time.time()
    with _DEVICE_LOCK:
        _prune_device_codes(now)
        if len(_DEVICE_CODES) >= _MAX_DEVICE_CODES:
            oldest = sorted(_DEVICE_CODES, key=lambda k: _DEVICE_CODES[k].created_at)
            for key in oldest[: _MAX_DEVICE_CODES // 10 or 1]:
                _DEVICE_CODES.pop(key, None)
        existing_codes = {v.user_code for v in _DEVICE_CODES.values()}
        user_code = _generate_user_code()
        while user_code in existing_codes:  # 6 位码空间 887M，正常不会撞
            user_code = _generate_user_code()
        record = DeviceCode(
            device_code=secrets.token_urlsafe(32),
            user_code=user_code,
            identity_id=str(identity_id),
            created_at=now,
            expires_at=now + ttl,
        )
        _DEVICE_CODES[record.device_code] = record
    logger.info(
        "fashion device flow started identity={} user_code={}",
        identity_id,
        mask_user_code(user_code),
    )
    return record


def find_device_by_user_code(user_code: Any) -> Optional[DeviceCode]:
    code = normalize_user_code(user_code)
    if not code:
        return None
    with _DEVICE_LOCK:
        for record in _DEVICE_CODES.values():
            if record.user_code == code:
                return record
    return None


def get_device(device_code: Any) -> Optional[DeviceCode]:
    code = str(device_code or "").strip()
    if not code:
        return None
    with _DEVICE_LOCK:
        return _DEVICE_CODES.get(code)


def find_device(code: Any) -> Optional[DeviceCode]:
    """按 ``userCode`` 或 ``deviceCode`` 查设备码记录（approve 两种入参都支持）。

    先按原文精确匹配 ``deviceCode``（token_urlsafe 可能含 ``-``/``_``，不能被
    用户码规范化逻辑破坏），再按规范化后的 6 位 ``userCode`` 匹配。
    """
    raw = str(code or "").strip()
    if not raw:
        return None
    with _DEVICE_LOCK:
        record = _DEVICE_CODES.get(raw)
        if record is not None:
            return record
        norm = normalize_user_code(raw)
        if not norm:
            return None
        return next((r for r in _DEVICE_CODES.values() if r.user_code == norm), None)


def approve_device_flow(user_code: Any, user_id: str, *, settings: Any = None) -> DeviceCode:
    """网页登录用户输入 6 位码完成绑定。

    Raises:
        FashionAuthError: ``device_not_found`` / ``device_expired`` /
            ``device_bound_to_other``
    """
    code = str(user_code or "").strip()
    if not code:
        raise FashionAuthError("device_not_found", "请输入 6 位绑定码")
    now = time.time()
    with _DEVICE_LOCK:
        record = _DEVICE_CODES.get(code)
        if record is None:
            norm = normalize_user_code(code)
            record = next(
                (r for r in _DEVICE_CODES.values() if r.user_code == norm), None
            ) if norm else None
        if record is None:
            raise FashionAuthError("device_not_found", "绑定码不存在", status_code=404)
        if record.expires_at <= now:
            raise FashionAuthError("device_expired", "绑定码已过期，请在客户端重新生成")
        if record.approved_user_id and record.approved_user_id != str(user_id):
            raise FashionAuthError(
                "device_bound_to_other", "该绑定码已被其他账号使用", status_code=409
            )
        record.approved_user_id = str(user_id)
        record.approved_at = now
    logger.info(
        "fashion device approved identity={} user={}", record.identity_id, user_id
    )
    return record


def device_flow_status(device_code: Any, identity_id: str) -> dict:
    """mod 轮询绑定结果。"""
    record = get_device(device_code)
    if record is None or record.identity_id != str(identity_id):
        raise FashionAuthError("device_not_found", "deviceCode 不存在", status_code=404)
    return {
        "status": record.status,
        "userCode": record.user_code,
        "boundUserId": record.approved_user_id,
    }


# ── 测试辅助 ────────────────────────────────────────────────────────────

def reset_state() -> None:
    """清空内存态（单测用；生产不要调用）。"""
    with _CHALLENGE_LOCK:
        _CHALLENGES.clear()
    with _DEVICE_LOCK:
        _DEVICE_CODES.clear()
