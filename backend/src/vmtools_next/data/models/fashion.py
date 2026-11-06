"""时装系统（VMFashion mod）ORM 模型。

八张表：

- ``fashion_identity``        已验证的 MC 身份（正版 / 皮肤站），可关联 vmtools 账号
- ``fashion_catalog``         时装目录（cosmetic 元数据，资产以 sha256 引用）+ 芒果块定价
- ``fashion_loadout``         玩家穿戴配置，唯一约束 (identity_id, slot)
- ``fashion_asset``           资产文件（按 sha256 去重，落盘路径见 storage_path）
- ``fashion_wardrobe``        用户「我的衣柜」收藏，唯一约束 (user_id, cosmetic_id)
- ``fashion_wallet``          用户的芒果块钱包（一个账号一条，余额非负整数）
- ``fashion_ownership``       用户「已获得」的时装（购买/领取/发放），唯一约束 (user_id, cosmetic_id)
- ``fashion_mango_transaction`` 芒果块收支流水（含幂等键，用于对账与防重复扣款）

约定：``mc_uuid`` 统一存**带连字符的小写** UUID，读写入口都用
``core.fashion_auth.normalize_uuid`` 规范化，避免 ``AABB…`` / ``aabb…`` 两份数据。

「获得时装」的完整链路（v1.1 起）
---------------------------------
``fashion_catalog.price_mango > 0`` 的时装需要先用芒果块购买：购买事务写
``fashion_ownership`` + ``fashion_mango_transaction``，并**自动写入**
``fashion_wardrobe``（mod 端 ``GET /wardrobe/me`` 只读衣柜，所以买完必须进衣柜
才穿得上）。``fashion_ownership`` 与 ``fashion_wardrobe`` 是两个维度：前者是
「买没买」（永不因删衣柜而消失），后者是「收藏夹里放不放」（玩家可自行删）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from vmtools_next.data.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FashionIdentityModel(Base):
    """一条已验证的 Minecraft 身份（一个 MC UUID 一条）。"""

    __tablename__ = "fashion_identity"

    id = Column(String, primary_key=True)
    # 带连字符的小写 UUID（32 位无连字符入参会在入口规范化）
    mc_uuid = Column(String, nullable=False, unique=True, index=True)
    mc_name = Column(String, nullable=False, index=True)
    # mojang | yggdrasil
    auth_type = Column(String, nullable=False, default="mojang")
    # 皮肤站 base URL（正版为空串）
    auth_server = Column(String, nullable=False, default="")
    verified_at = Column(DateTime, nullable=True)
    # 关联的 vmtools 账号（设备码绑定后写入；可为 NULL）
    user_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    last_seen_at = Column(DateTime, nullable=True)

    loadouts = relationship(
        "FashionLoadoutModel",
        back_populates="identity",
        cascade="all, delete-orphan",
    )


class FashionCatalogModel(Base):
    """时装目录条目（cosmetic）。

    资产不在库里内联，只用 ``sha256`` + ``bytes`` 指向 ``fashion_asset``
    或 ``asset_dir`` 下的静态文件；``asset_key`` 是 mod 侧请求资产时用的键。
    """

    __tablename__ = "fashion_catalog"

    cosmetic_id = Column(String, primary_key=True)
    pack_id = Column(String, nullable=False, default="", index=True)
    name = Column(String, nullable=False, default="")
    # cape | wings | hat | back | pet | aura | other
    category = Column(String, nullable=False, default="other", index=True)
    asset_key = Column(String, nullable=False, default="", index=True)
    sha256 = Column(String, nullable=False, default="")
    bytes = Column(Integer, nullable=False, default=0)
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=_utcnow, nullable=False)

    # ── 芒果块定价（v1.1）─────────────────────────────────────────────────
    # 单位为「芒果块」，整数，0 = 免费（与上线前行为完全一致）。
    # 不加 CHECK 约束：SQLite 的 ALTER TABLE ADD COLUMN 带 CHECK 在旧库上兼容差，
    # 非负与非超上限由服务层 validate_price_mango() 统一保证。
    price_mango = Column(BigInteger, nullable=False, default=0)
    # False = 商店不售卖（仅管理端发放 / 活动赠送），仍可穿戴、仍出现在目录里。
    purchasable = Column(Boolean, nullable=False, default=True)


class FashionLoadoutModel(Base):
    """玩家单个槽位的穿戴配置（(identity_id, slot) 唯一）。"""

    __tablename__ = "fashion_loadout"

    id = Column(String, primary_key=True)
    identity_id = Column(
        String, ForeignKey("fashion_identity.id"), nullable=False, index=True
    )
    # 槽位：cape | wings | hat | back | pet | aura | other
    slot = Column(String, nullable=False)
    cosmetic_id = Column(String, nullable=False)
    scale = Column(Float, nullable=False, default=1.0)
    updated_at = Column(DateTime, default=_utcnow, nullable=False)

    identity = relationship("FashionIdentityModel", back_populates="loadouts")

    __table_args__ = (
        UniqueConstraint("identity_id", "slot", name="uq_fashion_loadout_identity_slot"),
        Index("ix_fashion_loadout_identity_slot", "identity_id", "slot"),
    )


class FashionAssetModel(Base):
    """已上传的资产文件（同一 sha256 只落盘一份）。"""

    __tablename__ = "fashion_asset"

    id = Column(String, primary_key=True)
    pack_id = Column(String, nullable=False, default="", index=True)
    version = Column(String, nullable=False, default="1")
    filename = Column(String, nullable=False, default="")
    sha256 = Column(String, nullable=False, index=True)
    bytes = Column(Integer, nullable=False, default=0)
    # 相对 asset_dir 的落盘路径（禁止绝对路径 / ..）
    # index=True：`/assets/{key}` 的回退分支按 storage_path 精确查（见
    # core.fashion_service.asset_storage_path_exists）；无索引时会退化成全表扫描，
    # 4396 行资产下每个资产请求都全表扫一遍 —— 时装页一次拉几十个资产，
    # 直接把连接池打满（2026-11-01 事故 §六.106）。
    storage_path = Column(String, nullable=False, default="", index=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)


class FashionWardrobeModel(Base):
    """用户「我的衣柜」收藏（一件时装一条）。

    为什么按 ``user_id`` 而不是 ``identity_id``
    -------------------------------------------
    衣柜是**网站账号**的收藏夹（玩家在商店里「加入衣柜」），一个账号可能绑定多个
    MC 身份（正版 + 皮肤站），换身份登录时看到的是同一份衣柜才符合直觉；穿戴
    配置（``fashion_loadout``）才是身份维度的。所以这里外键指 ``users.id``。

    为什么保留 ``cosmetic_id`` 而不做外键
    ------------------------------------
    目录条目（``fashion_catalog``）随时可能被管理端删除/下架，但玩家已经收藏的
    记录不能跟着消失 —— 否则「删目录」会静默清空所有人的衣柜。目录查不到时由
    服务层把条目标成 ``missing`` 返回，玩家自己决定删不删。

    唯一约束 ``(user_id, cosmetic_id)``：重复「加入衣柜」是幂等的（不重复插入），
    同时兜住并发双击。
    """

    __tablename__ = "fashion_wardrobe"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    cosmetic_id = Column(String, nullable=False, index=True)
    added_at = Column(DateTime, default=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "cosmetic_id", name="uq_fashion_wardrobe_user_cosmetic"
        ),
    )


# ── 芒果块（v1.1：代币购买时装）─────────────────────────────────────────


class FashionWalletModel(Base):
    """用户芒果块钱包（一个网站账号一条，余额恒为非负整数）。

    为什么把钱包单独建表而不是给 ``users`` 加列
    ---------------------------------------------
    ``users`` 是鉴权模块的核心表，被登录/成员管理/机器人等十几个模块共用；把
    钱包挤进去会让每次时装购买都去写鉴权热表，也让「时装系统整体下线」时留下
    一列孤儿数据。独立表 + ``user_id`` 主键，读改写都只碰一行。

    ``total_earned`` / ``total_spent`` 是累计量（不是当前余额），给「收支明细」
    页做汇总用；每次入账/扣款同时累加，流水表则保留逐笔明细，两者可对账。
    """

    __tablename__ = "fashion_wallet"

    user_id = Column(String, ForeignKey("users.id"), primary_key=True)
    balance = Column(BigInteger, nullable=False, default=0)
    total_earned = Column(BigInteger, nullable=False, default=0)
    total_spent = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(DateTime, default=_utcnow, nullable=False)


class FashionOwnershipModel(Base):
    """用户「已获得」的时装（买断制，一件一条）。

    与 :class:`FashionWardrobeModel` 的区别
    --------------------------------------
    - ownership 是**权利**：买过就永远在，玩家从衣柜里删掉收藏不会失去权利，
      回商店再点一次「加入衣柜」免费补回（``POST /wardrobe`` 见服务层校验）。
    - wardrobe 是**收藏夹**：玩家可自由增删，mod 端 ``GET /wardrobe/me`` 只读它。

    唯一约束 ``(user_id, cosmetic_id)`` 是购买幂等的最后一道防线：即使两个并发
    请求都通过了「未拥有」检查，也只有一个能插入成功，另一个回滚且不扣款。

    ``cosmetic_id`` 存**规范 ID**（``packId:localId``），与 wardrobe 保持一致，
    不加外键：目录条目被删/下架后已购记录要保留（玩家已付过钱）。
    """

    __tablename__ = "fashion_ownership"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    cosmetic_id = Column(String, nullable=False, index=True)
    # 实付芒果块（免费领取 / 历史回填 / 管理端赠送为 0）
    price_paid = Column(BigInteger, nullable=False, default=0)
    # purchase | free_claim | legacy_wardrobe | admin_grant | migration
    source = Column(String, nullable=False, default="purchase")
    # 订单号：给「购买记录」页与后续对账用（同一笔购买共享）
    order_no = Column(String, nullable=False, default="", index=True)
    note = Column(String, nullable=False, default="")
    # 管理端发放时记录操作者，便于审计
    granted_by = Column(String, nullable=True)
    acquired_at = Column(DateTime, default=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "cosmetic_id", name="uq_fashion_ownership_user_cosmetic"
        ),
        Index("ix_fashion_ownership_user_acquired", "user_id", "acquired_at"),
    )


class FashionMangoTransactionModel(Base):
    """芒果块收支流水（只追加，不改不删）。

    ``delta`` 正数为入账、负数为扣款，``balance_after`` 记该笔之后的余额 ——
    这样任意时点的余额都能用最后一条流水复核，出现「余额对不上」时能定位到
    具体哪一笔。``delta=0`` 的流水用于免费领取 / 管理端赠送（记录「获得」这件事，
    但不影响余额）。

    ``idempotency_key`` 唯一（可空）：``POST /store/purchase`` 的 ``clientRequestId``
    落在这里，重复提交同一 key 直接返回首次结果，不重复扣款。SQLite / PostgreSQL
    的唯一索引都允许多个 NULL，所以不带 key 的请求互不影响。
    """

    __tablename__ = "fashion_mango_transaction"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    delta = Column(BigInteger, nullable=False, default=0)
    balance_after = Column(BigInteger, nullable=False, default=0)
    # 列名避开 Python 内置 ``type``：对外 JSON 仍是 ``type``（见 schemas）
    # purchase | refund | admin_adjust | recharge | grant
    tx_type = Column(String, nullable=False, default="purchase", index=True)
    cosmetic_id = Column(String, nullable=False, default="")
    cosmetic_name = Column(String, nullable=False, default="")
    note = Column(String, nullable=False, default="")
    idempotency_key = Column(String, nullable=True, unique=True, index=True)
    # 管理端调整时记录操作者（自助充值为空）
    operator_user_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_fashion_mango_tx_user_created", "user_id", "created_at"),
    )
