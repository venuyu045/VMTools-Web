"""标点中继（VMPing）核心 —— 纯内存房间 / 长轮询 / 限流。

接口分组
--------
本模块**不含 HTTP**，只被 ``api/routers/ping.py`` 调用：

``publish(...)``
    发送一条标点：限流 → 房间容量 → 入环形缓冲 → 唤醒同房间等待者
``wait_for_pings(room, since, timeout, dimension)``
    长轮询（async，事件驱动）：有 ``seq > since`` 的存活标点立刻返回；否则把等待者
    挂到房间上，发送方唤醒或超时返回空数组
``snapshot(room, since, dimension)``
    立即取一次（不等待），客户端重连补齐用
``prune()`` / ``stats()`` / ``reset()``
    运维与单测探针

鉴权
----
本模块**不鉴权**：身份由 ``api/routers/ping.py`` 从 ``aud="ping"`` 的 Bearer token
里解出（``mcUuid`` / ``mcName``）后传进来。``roomId`` 只是客户端算出的不透明字符串
（``[a-f0-9]{16}``），服务端不反推服务器地址、也不落库。

容量（默认值见 :class:`PingConfig`，与 ``docs/协议-v1.md`` §6 一致）
------------------------------------------------------------------
=========================  =========  ==============================================
项                         默认       行为
=========================  =========  ==============================================
每房间存活标点             200        满了 ``room_full``（429），过期条目先惰性清掉
标点 TTL                   20s        惰性清理（房间被访问时才扫一遍）
房间空闲回收               60s        无活动且无等待者即回收（``seq`` 随之归零）
全局房间数                 1000       超限先回收空闲房间，仍超限 ``room_full``
每房间等待者               64         超出 ``room_full``（429）
全局长轮询并发             5000       超出 ``server_busy``（503 + Retry-After）
每玩家发送                 5 条 / 3s  滑动窗口，超出 ``rate_limited``（429 + Retry-After）
=========================  =========  ==============================================

并发模型（单进程）
------------------
状态用 ``threading.RLock`` 保护：临界区只有纯内存的 O(1) / O(n≤200) 操作，没有任何
I/O 与 ``await``，所以**不会阻塞事件循环**；同时天然覆盖「FastAPI 把同步路由丢进
线程池」的路径（``asyncio.Lock`` 跨事件循环不安全：``asyncio.run()`` 每次都新建
loop，模块级锁一旦绑定到已关闭的 loop 就抛 ``bound to a different event loop``）。

长轮询用 ``asyncio.Event`` **事件驱动**唤醒（不是短轮询）：发送方与等待者在同一事件
循环时直接 ``set()``，跨线程时走 ``loop.call_soon_threadsafe``。

⚠️ 多 worker 部署（``uvicorn --workers N`` / gunicorn）时每个进程一份独立内存，
同一房间的玩家会落到不同进程 → 互相收不到标点。**必须单 worker**，
详见 ``docs/后端实现.md`` 的部署注意。
"""
from __future__ import annotations

import asyncio
import math
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ── 协议常量 ────────────────────────────────────────────────────────────

#: 标点类型白名单（协议 §4）
KINDS: tuple[str, ...] = ("default", "danger", "item", "go", "help")

#: 房间键：hex(sha256(归一化服务器地址))[0:16]，小写十六进制
ROOM_ID_RE = re.compile(r"^[a-f0-9]{16}$")

_UUID_DASHED_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")

#: dimension 字段最大长度（协议 §4）
MAX_DIMENSION_LENGTH = 128
#: targetName 最大长度（协议 §4）
MAX_TARGET_NAME_LENGTH = 64
#: 坐标上限（协议 §4）：|x|,|z| ≤ 3e7，|y| ≤ 2048
MAX_COORD_XZ = 3e7
MAX_COORD_Y = 2048.0

# ── 默认容量（config.PingSettings 的兜底值，改默认值请两处一起改） ─────

DEFAULT_MAX_PINGS_PER_ROOM = 200
DEFAULT_PING_TTL_SECONDS = 20.0
# 房间空闲回收：单进程部署（config.prod.yaml workers=1）下收短到 60s，
# 让「服务器换图/没人玩」的房间尽快让位，房间数上限才压得住内存
DEFAULT_ROOM_IDLE_SECONDS = 60.0
DEFAULT_MAX_ROOMS = 1000
DEFAULT_MAX_WAITERS_PER_ROOM = 64
# 全局长轮询并发上限（跨房间）：超出返回 503 server_busy，避免长连接把单进程
# 的事件循环/文件描述符吃满（一个等待者 = 一个挂起中的 HTTP 连接）
DEFAULT_MAX_WAITERS_TOTAL = 5000
DEFAULT_RATE_LIMIT = 5
DEFAULT_RATE_WINDOW_SECONDS = 3.0
DEFAULT_WAIT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_WAIT_TIMEOUT_SECONDS = 25.0
DEFAULT_TOKEN_TTL_SECONDS = 86400
DEFAULT_CHALLENGE_RATE_LIMIT = 20
DEFAULT_CHALLENGE_RATE_WINDOW_SECONDS = 60.0


# ── 错误 ────────────────────────────────────────────────────────────────

class PingRelayError(Exception):
    """中继业务错误：``code`` 直接进 ``{"detail": {"code": ..., "message": ...}}``。

    ``retry_after`` 非空时，路由层会把它写进 ``Retry-After`` 响应头（429 用）。
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        status_code: int = 400,
        retry_after: Optional[int] = None,
    ):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.status_code = status_code
        self.retry_after = retry_after


# ── 参数校验（协议 §4；路由层直接复用，保证错误体一致） ────────────────

def normalize_room_id(value: Any) -> str:
    """校验并规范化房间键（小写 ``[a-f0-9]{16}``）。

    客户端算的是小写 hex；这里容忍大小写混写（统一转小写），其余一律 400。
    """
    raw = str(value or "").strip().lower()
    if not ROOM_ID_RE.match(raw):
        raise PingRelayError("bad_request", "roomId 必须是 16 位十六进制字符串")
    return raw


def normalize_uuid(value: Any) -> str:
    """把 UUID 规范化为「带连字符的小写」形式；非法 → 400 ``bad_request``。"""
    raw = str(value or "").strip().lower()
    if _UUID_DASHED_RE.match(raw):
        return raw
    compact = raw.replace("-", "")
    if _UUID_HEX_RE.match(compact):
        return (
            f"{compact[0:8]}-{compact[8:12]}-{compact[12:16]}-"
            f"{compact[16:20]}-{compact[20:32]}"
        )
    raise PingRelayError("bad_request", f"UUID 格式非法：{value!r}")


def normalize_target_uuid(value: Any) -> Optional[str]:
    """可空的实体 UUID（点到实体时带上）；空值 → ``None``。"""
    raw = str(value or "").strip()
    if not raw:
        return None
    return normalize_uuid(raw)


def validate_coordinate(name: str, value: Any, limit: float) -> float:
    """坐标必须是有限浮点且在 ``|value| ≤ limit`` 内（拒绝 NaN/Inf）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PingRelayError("bad_request", f"{name} 必须是数字")
    if not math.isfinite(number):
        raise PingRelayError("bad_request", f"{name} 不能是 NaN/Inf")
    if abs(number) > limit:
        raise PingRelayError(
            "bad_request", f"{name} 超出范围（|{name}| ≤ {limit:g}）"
        )
    return number


def validate_dimension(value: Any, *, optional: bool = False) -> Optional[str]:
    """维度是**消息字段**（不是房间键）：非空、≤128 字符。

    ``optional=True`` 时用于 GET 的过滤参数：空串 / None → ``None``（不过滤）。
    """
    raw = str(value or "").strip()
    if not raw:
        if optional:
            return None
        raise PingRelayError("bad_request", "dimension 不能为空")
    if len(raw) > MAX_DIMENSION_LENGTH:
        raise PingRelayError(
            "bad_request", f"dimension 长度不能超过 {MAX_DIMENSION_LENGTH}"
        )
    return raw


def validate_kind(value: Any) -> str:
    """``kind`` 白名单：default | danger | item | go | help。"""
    kind = str(value or "").strip().lower()
    if kind not in KINDS:
        raise PingRelayError("bad_request", f"kind 必须是 {', '.join(KINDS)} 之一")
    return kind


def validate_target_name(value: Any) -> Optional[str]:
    """``targetName`` 可空，最长 64 字符。"""
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) > MAX_TARGET_NAME_LENGTH:
        raise PingRelayError(
            "bad_request", f"targetName 长度不能超过 {MAX_TARGET_NAME_LENGTH}"
        )
    return raw


def validate_player_name(value: Any) -> str:
    """玩家名来自 token claims；空值给个占位，超长截断（不因上游异常拒服务）。"""
    raw = str(value or "").strip()
    return raw[:32] if raw else "unknown"


# ── 配置 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PingConfig:
    """中继运行参数（纯 dataclass，不依赖 pydantic，便于离线单测）。

    生产值来自 ``config.PingSettings``（``VMT_PING__*`` 环境变量），
    :meth:`from_settings` 在配置读不到时退化为这里的默认值。
    """

    enabled: bool = True
    max_pings_per_room: int = DEFAULT_MAX_PINGS_PER_ROOM
    ping_ttl_seconds: float = DEFAULT_PING_TTL_SECONDS
    room_idle_seconds: float = DEFAULT_ROOM_IDLE_SECONDS
    max_rooms: int = DEFAULT_MAX_ROOMS
    max_waiters_per_room: int = DEFAULT_MAX_WAITERS_PER_ROOM
    max_waiters_total: int = DEFAULT_MAX_WAITERS_TOTAL
    rate_limit: int = DEFAULT_RATE_LIMIT
    rate_window_seconds: float = DEFAULT_RATE_WINDOW_SECONDS
    wait_timeout_seconds: float = DEFAULT_WAIT_TIMEOUT_SECONDS
    max_wait_timeout_seconds: float = DEFAULT_MAX_WAIT_TIMEOUT_SECONDS
    token_ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS
    challenge_rate_limit: int = DEFAULT_CHALLENGE_RATE_LIMIT
    challenge_rate_window_seconds: float = DEFAULT_CHALLENGE_RATE_WINDOW_SECONDS

    @classmethod
    def from_settings(cls, settings: Any = None) -> "PingConfig":
        """从 ``PingSettings``（或任意同名属性对象）构造；缺项用默认值。"""

        def _get(name: str, default: Any) -> Any:
            value = getattr(settings, name, None) if settings is not None else None
            return default if value is None else value

        return cls(
            enabled=bool(_get("enabled", True)),
            max_pings_per_room=int(_get("max_pings_per_room", DEFAULT_MAX_PINGS_PER_ROOM)),
            ping_ttl_seconds=float(_get("ping_ttl_seconds", DEFAULT_PING_TTL_SECONDS)),
            room_idle_seconds=float(_get("room_idle_seconds", DEFAULT_ROOM_IDLE_SECONDS)),
            max_rooms=int(_get("max_rooms", DEFAULT_MAX_ROOMS)),
            max_waiters_per_room=int(
                _get("max_waiters_per_room", DEFAULT_MAX_WAITERS_PER_ROOM)
            ),
            max_waiters_total=int(
                _get("max_waiters_total", DEFAULT_MAX_WAITERS_TOTAL)
            ),
            rate_limit=int(_get("rate_limit", DEFAULT_RATE_LIMIT)),
            rate_window_seconds=float(
                _get("rate_window_seconds", DEFAULT_RATE_WINDOW_SECONDS)
            ),
            wait_timeout_seconds=float(
                _get("wait_timeout_seconds", DEFAULT_WAIT_TIMEOUT_SECONDS)
            ),
            max_wait_timeout_seconds=float(
                _get("max_wait_timeout_seconds", DEFAULT_MAX_WAIT_TIMEOUT_SECONDS)
            ),
            token_ttl_seconds=int(_get("token_ttl_seconds", DEFAULT_TOKEN_TTL_SECONDS)),
            challenge_rate_limit=int(
                _get("challenge_rate_limit", DEFAULT_CHALLENGE_RATE_LIMIT)
            ),
            challenge_rate_window_seconds=float(
                _get("challenge_rate_window_seconds", DEFAULT_CHALLENGE_RATE_WINDOW_SECONDS)
            ),
        )


# ── 滑动窗口限流 ────────────────────────────────────────────────────────

class SlidingWindowLimiter:
    """进程内滑动窗口限流（单机；多 worker 各进程独立，见模块顶部说明）。

    只用于「防刷」这类**软**限制：超限抛 :class:`PingRelayError`（429 +
    ``retry_after``），调用方转成 ``Retry-After`` 头。
    """

    def __init__(self, limit: int, window_seconds: float, *, max_keys: int = 5000):
        self.limit = max(1, int(limit))
        self.window = max(0.0, float(window_seconds))
        self.max_keys = max(1, int(max_keys))
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(
        self,
        key: str,
        *,
        now: Optional[float] = None,
        message: str = "操作过于频繁，请稍后再试",
    ) -> None:
        """记一次命中；超限抛 ``rate_limited``（429，带 retry_after 秒）。"""
        current = time.monotonic() if now is None else now
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if current - t < self.window]
            if len(hits) >= self.limit:
                # 最早那次命中滑出窗口后即可重试
                retry_after = max(1, int(math.ceil(self.window - (current - hits[0]))))
                self._hits[key] = hits
                raise PingRelayError(
                    "rate_limited", message, 429, retry_after=retry_after
                )
            hits.append(current)
            self._hits[key] = hits
            if len(self._hits) > self.max_keys:  # 防内存膨胀：清掉整窗过期的 key
                stale = [
                    k for k, v in self._hits.items() if not v or current - v[-1] >= self.window
                ]
                for key_name in stale:
                    self._hits.pop(key_name, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._hits)


# ── 数据模型 ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Ping:
    """一条标点（不可变；对外字段名见 :meth:`to_dict`，与协议 §5 一致）。"""

    id: str
    seq: int
    player_uuid: str
    player_name: str
    dimension: str
    x: float
    y: float
    z: float
    kind: str
    target_uuid: Optional[str]
    target_name: Optional[str]
    server_time: int
    expires_at: int
    #: 进程内单调时钟的过期时刻（TTL 惰性清理用；不对外）
    expires_mono: float = field(default=0.0, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """协议 §5 的 JSON 形状（camelCase）。"""
        return {
            "seq": self.seq,
            "id": self.id,
            "player": {"uuid": self.player_uuid, "name": self.player_name},
            "dimension": self.dimension,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "kind": self.kind,
            "targetUuid": self.target_uuid,
            "targetName": self.target_name,
            "serverTime": self.server_time,
            "expiresAt": self.expires_at,
        }


@dataclass
class PingBatch:
    """一次拉取的结果：``seq`` 是房间当前最大序号，客户端下次用它做 ``since``。"""

    seq: int
    server_time: int
    pings: list[Ping]


class _Waiter:
    """长轮询等待者：一个 ``asyncio.Event`` + 它所属的事件循环（跨线程唤醒用）。"""

    __slots__ = ("loop", "event")

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.event = asyncio.Event()

    def wake(self) -> None:
        """唤醒等待者；同 loop 直接 set，跨线程走 call_soon_threadsafe。"""
        try:
            loop = self.loop
            if loop.is_closed():
                return
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:  # 当前线程没有运行中的 loop（线程池调用方）
                running = None
            if running is loop:
                self.event.set()
            else:
                loop.call_soon_threadsafe(self.event.set)
        except Exception:  # pragma: no cover - 唤醒失败最坏只是等到超时
            pass


class _Room:
    """一个房间：单调 ``seq`` + 标点环形缓冲 + 等待者集合 + 最后活跃时刻。"""

    __slots__ = ("room_id", "seq", "pings", "waiters", "last_active")

    def __init__(self, room_id: str, now: float, max_pings: int):
        self.room_id = room_id
        self.seq = 0
        # 环形缓冲：maxlen 是硬上限；正常路径在 append 前就会因「满」返回 room_full，
        # maxlen 只是兜底，防止逻辑写错时无限增长。
        self.pings: deque[Ping] = deque(maxlen=max_pings)
        self.waiters: set[_Waiter] = set()
        self.last_active = now


# ── 中继 ────────────────────────────────────────────────────────────────

class PingRelay:
    """纯内存标点中继（单进程）。

    典型用法（路由层）::

        relay = PingRelay.from_settings(get_config().ping)
        ping = relay.publish(room_id=..., player_uuid=..., ...)
        batch = await relay.wait_for_pings(room_id, since=0, timeout=20)
    """

    def __init__(
        self,
        config: Optional[PingConfig] = None,
        *,
        clock: Optional[Callable[[], float]] = None,
        wall_clock: Optional[Callable[[], float]] = None,
        id_factory: Optional[Callable[[], str]] = None,
    ):
        self.config = config or PingConfig()
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        self._id_factory = id_factory or (lambda: secrets.token_hex(16))
        self._lock = threading.RLock()
        self._rooms: dict[str, _Room] = {}
        # 全局长轮询并发（跨房间）：房间级上限之外再兜一层，防长连接打满单进程
        self._waiters: set[_Waiter] = set()
        self._limiter = SlidingWindowLimiter(
            self.config.rate_limit, self.config.rate_window_seconds
        )

    # ── 构造 ────────────────────────────────────────────────────────────
    @classmethod
    def from_settings(cls, settings: Any = None) -> "PingRelay":
        """按 ``PingSettings``（或 ``PingConfig``）构造。"""
        if isinstance(settings, PingConfig):
            return cls(settings)
        return cls(PingConfig.from_settings(settings))

    # ── 时钟 ────────────────────────────────────────────────────────────
    def _now(self) -> float:
        return float(self._clock())

    def _now_ms(self) -> int:
        return int(self._wall_clock() * 1000)

    # ── 发送 ────────────────────────────────────────────────────────────
    def publish(
        self,
        *,
        room_id: str,
        player_uuid: str,
        player_name: str = "",
        dimension: str,
        x: float,
        y: float,
        z: float,
        kind: str = "default",
        target_uuid: Optional[str] = None,
        target_name: Optional[str] = None,
        client_time: Optional[int] = None,
    ) -> Ping:
        """发送一条标点，返回落库（内存）后的实体。

        Args:
            client_time: 客户端时间戳（毫秒）。**只用于日志/诊断**，服务端时间才是准。

        Raises:
            PingRelayError: ``bad_request``（参数越界）/ ``rate_limited``（429）/
                ``room_full``（429，房间满或全局房间数满）。
        """
        room_key = normalize_room_id(room_id)
        dim = validate_dimension(dimension)
        kind_value = validate_kind(kind)
        x_value = validate_coordinate("x", x, MAX_COORD_XZ)
        y_value = validate_coordinate("y", y, MAX_COORD_Y)
        z_value = validate_coordinate("z", z, MAX_COORD_XZ)
        target_uuid_value = normalize_target_uuid(target_uuid)
        target_name_value = validate_target_name(target_name)
        uuid_value = normalize_uuid(player_uuid)
        name_value = validate_player_name(player_name)

        # 先限流再动房间：被限流的请求不该创建房间 / 占容量
        self._limiter.check(uuid_value, now=self._now(), message="标点发送过于频繁，请稍后再试")

        now = self._now()
        now_ms = self._now_ms()
        ttl_ms = int(self.config.ping_ttl_seconds * 1000)
        waiters: list[_Waiter] = []
        with self._lock:
            room = self._rooms.get(room_key)
            if room is None:
                room = self._create_room_locked(room_key, now)
            self._prune_pings_locked(room, now)
            if len(room.pings) >= self.config.max_pings_per_room:
                raise PingRelayError(
                    "room_full", "房间标点已满，请稍后再试", 429, retry_after=1
                )
            room.seq += 1
            ping = Ping(
                id=self._id_factory(),
                seq=room.seq,
                player_uuid=uuid_value,
                player_name=name_value,
                dimension=dim,
                x=x_value,
                y=y_value,
                z=z_value,
                kind=kind_value,
                target_uuid=target_uuid_value,
                target_name=target_name_value,
                server_time=now_ms,
                expires_at=now_ms + ttl_ms,
                expires_mono=now + self.config.ping_ttl_seconds,
            )
            room.pings.append(ping)
            room.last_active = now
            waiters = list(room.waiters)
        # 锁外唤醒：唤醒回调可能触发等待者所在线程的调度，别在临界区里做
        for waiter in waiters:
            waiter.wake()
        return ping

    # ── 拉取（立即） ────────────────────────────────────────────────────
    def snapshot(
        self,
        room_id: str,
        since: int = 0,
        dimension: Optional[str] = None,
        *,
        create: bool = False,
    ) -> PingBatch:
        """立即取一次 ``seq > since`` 的存活标点（不等）。"""
        room_key = normalize_room_id(room_id)
        since_value = max(0, int(since or 0))
        dim = validate_dimension(dimension, optional=True)
        now = self._now()
        with self._lock:
            room = self._rooms.get(room_key)
            if room is None:
                if not create:
                    return PingBatch(seq=0, server_time=self._now_ms(), pings=[])
                room = self._create_room_locked(room_key, now)
            self._prune_pings_locked(room, now)
            room.last_active = now
            pings = [
                ping
                for ping in room.pings
                if ping.seq > since_value and (dim is None or ping.dimension == dim)
            ]
            return PingBatch(seq=room.seq, server_time=self._now_ms(), pings=pings)

    # ── 拉取（长轮询） ──────────────────────────────────────────────────
    async def wait_for_pings(
        self,
        room_id: str,
        since: int = 0,
        timeout: Optional[float] = None,
        dimension: Optional[str] = None,
    ) -> PingBatch:
        """长轮询：有 ``seq > since`` 的存活标点立刻返回，否则最多等 ``timeout`` 秒。

        - ``timeout`` 缺省取配置（20s），上限 25s，负数按 0 处理（= 立即返回）
        - 期间被发送方唤醒但只有**别的维度**的标点 → 继续等剩余时间
        - 房间等待者上限 64 → ``room_full``（429）；全局长轮询上限 5000 →
          ``server_busy``（503，带 ``Retry-After``）

        Raises:
            PingRelayError: ``bad_request`` / ``room_full`` / ``server_busy``
        """
        room_key = normalize_room_id(room_id)
        since_value = max(0, int(since or 0))
        dim = validate_dimension(dimension, optional=True)
        wait_seconds = self.clamp_timeout(timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds
        while True:
            batch = self.snapshot(room_key, since_value, dim, create=False)
            if batch.pings:
                return batch
            remaining = deadline - loop.time()
            if remaining <= 0:
                return batch
            waiter = self._add_waiter(room_key, loop)
            try:
                # 注册后再查一次：覆盖「上面 snapshot 之后、注册之前」有人 publish 的
                # 竞态（同步路由在线程池里发送时真会发生）。注册之后到达的 publish
                # 一定会 set event，所以不会丢唤醒。
                batch = self.snapshot(room_key, since_value, dim, create=False)
                if batch.pings:
                    return batch
                await asyncio.wait_for(waiter.event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return self.snapshot(room_key, since_value, dim, create=False)
            finally:
                self._remove_waiter(room_key, waiter)

    def clamp_timeout(self, timeout: Optional[float]) -> float:
        """把请求里的 ``timeout`` 夹到 ``[0, max_wait_timeout_seconds]``。"""
        default = self.config.wait_timeout_seconds
        try:
            value = default if timeout is None else float(timeout)
        except (TypeError, ValueError):
            value = default
        if not math.isfinite(value):
            value = default
        return max(0.0, min(value, self.config.max_wait_timeout_seconds))

    # ── 清理 / 观测 ─────────────────────────────────────────────────────
    def prune(self) -> dict[str, int]:
        """手动清理过期标点与空闲房间，返回清理结果（运维/单测探针）。"""
        now = self._now()
        removed_rooms = 0
        removed_pings = 0
        with self._lock:
            for room_key in list(self._rooms):
                room = self._rooms[room_key]
                before = len(room.pings)
                self._prune_pings_locked(room, now)
                removed_pings += before - len(room.pings)
            for room_key in list(self._rooms):
                room = self._rooms[room_key]
                if room.waiters:
                    continue
                if now - room.last_active >= self.config.room_idle_seconds:
                    self._rooms.pop(room_key, None)
                    removed_rooms += 1
        return {"rooms": removed_rooms, "pings": removed_pings}

    def stats(self) -> dict[str, int]:
        """当前内存占用快照（房间数 / 标点数 / 等待者数）。"""
        with self._lock:
            return {
                "rooms": len(self._rooms),
                "pings": sum(len(room.pings) for room in self._rooms.values()),
                "waiters": len(self._waiters),
            }

    def reset(self) -> None:
        """清空全部内存态（单测用；生产不要调用）。"""
        with self._lock:
            self._rooms.clear()
            self._waiters.clear()
        self._limiter.reset()

    # ── 内部：房间 ──────────────────────────────────────────────────────
    def _create_room_locked(self, room_key: str, now: float) -> _Room:
        """建房间；全局房间数超限时先回收空闲房间，仍超限 → ``room_full``。"""
        if len(self._rooms) >= self.config.max_rooms:
            self._prune_idle_rooms_locked(now)
            if len(self._rooms) >= self.config.max_rooms:
                raise PingRelayError(
                    "room_full", "服务端房间数已达上限，请稍后再试", 429, retry_after=5
                )
        room = _Room(room_key, now, self.config.max_pings_per_room)
        self._rooms[room_key] = room
        return room

    def _prune_idle_rooms_locked(self, now: float) -> None:
        for room_key in list(self._rooms):
            room = self._rooms[room_key]
            if room.waiters:  # 有人正在等：不能回收，否则唤醒丢失
                continue
            if now - room.last_active >= self.config.room_idle_seconds:
                self._rooms.pop(room_key, None)

    def _prune_pings_locked(self, room: _Room, now: float) -> None:
        """TTL 惰性清理：deque 按 seq 有序，过期的一定在最左边。"""
        pings = room.pings
        while pings and pings[0].expires_mono <= now:
            pings.popleft()

    # ── 内部：等待者 ────────────────────────────────────────────────────
    def _add_waiter(self, room_key: str, loop: asyncio.AbstractEventLoop) -> _Waiter:
        now = self._now()
        with self._lock:
            room = self._rooms.get(room_key)
            if room is None:
                room = self._create_room_locked(room_key, now)
            if len(room.waiters) >= self.config.max_waiters_per_room:
                raise PingRelayError(
                    "room_full", "该房间订阅者过多，请稍后再试", 429, retry_after=1
                )
            if len(self._waiters) >= self.config.max_waiters_total:
                # 全局长轮询打满：503（客户端按 server_error 退避重试，别当成房间问题）
                raise PingRelayError(
                    "server_busy", "服务端长轮询连接数已达上限，请稍后再试", 503, retry_after=1
                )
            waiter = _Waiter(loop)
            room.waiters.add(waiter)
            self._waiters.add(waiter)
            room.last_active = now
            return waiter

    def _remove_waiter(self, room_key: str, waiter: _Waiter) -> None:
        with self._lock:
            self._waiters.discard(waiter)
            room = self._rooms.get(room_key)
            if room is None:
                return
            room.waiters.discard(waiter)
            room.last_active = self._now()
