"""Pydantic schemas for the ping (VMPing 标点中继) API.

对外 JSON 一律 **camelCase**（mod 是 Java 写的，字段名按 mod 侧约定），Python 侧保持
snake_case：用 ``alias`` + ``populate_by_name`` 两全（与 ``api/schemas/fashion.py``
同一套写法）。

错误响应统一为::

    {"detail": {"code": "bad_request", "message": "..."}}

参数**取值范围**不在这里用 pydantic 约束（``ge`` / ``max_length`` 会变成 FastAPI 的
422，而协议 §1 要求 400 ``bad_request``）：类型只做粗校验，范围/白名单统一在
``core/ping_relay.py`` 的校验函数里做，错误体才可控。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class PingSchema(BaseModel):
    """所有 ping schema 的基类：允许用字段名或别名（camelCase）赋值。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


# ── 认证（与 VMFashion 的 serverId 挑战同构，token aud="ping"） ─────────

class ChallengeRequest(PingSchema):
    mc_uuid: str = Field(..., alias="mcUuid", description="玩家 UUID（带/不带连字符均可）")
    mc_name: str = Field(..., alias="mcName", description="玩家名（3-16 位）")
    auth_server: str = Field(
        default="",
        alias="authServer",
        description="皮肤站 base URL；正版玩家留空。白名单外的地址会被忽略",
    )


class ChallengeResponse(PingSchema):
    server_id: str = Field(..., alias="serverId")
    expires_in: int = Field(..., alias="expiresIn", description="serverId 剩余有效秒数")
    auth_type: str = Field(default="mojang", alias="authType", description="mojang | yggdrasil")


class VerifyRequest(PingSchema):
    mc_uuid: str = Field(..., alias="mcUuid")
    mc_name: str = Field(..., alias="mcName")
    auth_server: str = Field(default="", alias="authServer")
    server_id: str = Field(..., alias="serverId")


class VerifyResponse(PingSchema):
    """协议 §2.3：``expiresAt`` 是**毫秒时间戳**（不是剩余秒数）。"""

    token: str
    expires_at: int = Field(..., alias="expiresAt", description="token 过期时刻（毫秒）")
    mc_uuid: str = Field(..., alias="mcUuid")
    mc_name: str = Field(..., alias="mcName")
    auth_type: str = Field(default="mojang", alias="authType")


# ── 标点 ────────────────────────────────────────────────────────────────

class PlayerRef(PingSchema):
    """标点作者（协议 §5 的 ``player`` 对象）。"""

    uuid: str
    name: str = ""


class PingOut(PingSchema):
    """一条标点（协议 §5）。"""

    seq: int
    id: str
    player: PlayerRef
    dimension: str
    x: float
    y: float
    z: float
    kind: str = "default"
    target_uuid: Optional[str] = Field(default=None, alias="targetUuid")
    target_name: Optional[str] = Field(default=None, alias="targetName")
    server_time: int = Field(..., alias="serverTime")
    expires_at: int = Field(..., alias="expiresAt")


class PingSendRequest(PingSchema):
    """发送标点（协议 §4）。服务端**不信** ``clientTime``，``serverTime`` 为准。"""

    dimension: str = Field(..., description="维度 ID，如 minecraft:overworld（≤128 字符）")
    x: float
    y: float
    z: float
    kind: str = Field(default="default", description="default | danger | item | go | help")
    target_uuid: Optional[str] = Field(default=None, alias="targetUuid")
    target_name: Optional[str] = Field(default=None, alias="targetName")
    client_time: Optional[int] = Field(
        default=None, alias="clientTime", description="客户端时间戳（毫秒，仅诊断用）"
    )


class PingSendResponse(PingSchema):
    id: str
    seq: int
    server_time: int = Field(..., alias="serverTime")
    expires_at: int = Field(..., alias="expiresAt")


class PingBatchResponse(PingSchema):
    """长轮询 / 快照结果（协议 §5）。``seq`` 是房间当前最大序号，下次当 ``since`` 用。"""

    seq: int
    server_time: int = Field(..., alias="serverTime")
    pings: list[PingOut] = Field(default_factory=list)
