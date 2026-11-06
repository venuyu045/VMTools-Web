"""fashion: 芒果块钱包 / 代币购买（v1.1）

新增三张表 + 目录两列 + 历史数据回填
------------------------------------
- ``fashion_catalog.price_mango``  INTEGER  价格（芒果块），0 = 免费
- ``fashion_catalog.purchasable``  BOOLEAN  是否可在商店售卖
- ``fashion_wallet``               每账号一行的余额
- ``fashion_ownership``            已获得时装（购买 / 领取 / 发放 / 历史回填）
- ``fashion_mango_transaction``    收支流水（含幂等键）

⚠️ ``init_db()`` 的 ``Base.metadata.create_all(checkfirst=True)`` **只建缺失的表，
不会给已存在的表加列**（2026-11-01 事故 §六.106 已踩过：索引没补上导致全表扫描）。
所以 ``price_mango`` / ``purchasable`` 必须由本迁移显式 ``add_column``；
``db.py::_run_lightweight_migrations`` 里也加了同款幂等 ALTER 作为「不跑 alembic
的开发机」兜底，两处都写、互不冲突。

**历史回填**：v1.1 上线前玩家免费加入衣柜的时装一律视为「已拥有」（价格 0，
source=legacy_wardrobe），不追溯收费、不因管理端后续设价而突然不能穿/不能加。
同时给这些账号建余额 0 的钱包。服务层 ``ensure_legacy_ownership`` 还有一份懒回填
兜底（覆盖没跑迁移的库）。

幂等：所有建表/加列/回填先探测，重复执行 ``alembic upgrade head`` 不会炸，
也不会产生重复所有权（唯一约束 + 预查）。

Revision ID: 20261105_fashion_mango
Revises: 20261103_fashion_asset_idx
Create Date: 2026-11-05
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20261105_fashion_mango"
down_revision = "20261103_fashion_asset_idx"
branch_labels = None
depends_on = None

_CATALOG = "fashion_catalog"
_WARDROBE = "fashion_wardrobe"
_WALLET = "fashion_wallet"
_OWNERSHIP = "fashion_ownership"
_TRANSACTION = "fashion_mango_transaction"


def _existing_tables() -> set[str]:
    try:
        bind = op.get_bind()
        return set(sa.inspect(bind).get_table_names())
    except Exception:  # pragma: no cover - 离线 stub / 无 bind
        return set()


def _existing_columns(table: str) -> set[str]:
    try:
        bind = op.get_bind()
        return {str(c.get("name")) for c in sa.inspect(bind).get_columns(table)}
    except Exception:  # pragma: no cover
        return set()


def _add_catalog_columns() -> None:
    """给已有目录表补两列（全新库由 create_all 建表时自带，探测到就跳过）。"""
    if _CATALOG not in _existing_tables():
        return
    columns = _existing_columns(_CATALOG)
    if "price_mango" not in columns:
        op.add_column(
            _CATALOG,
            sa.Column(
                "price_mango",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
    if "purchasable" not in columns:
        op.add_column(
            _CATALOG,
            sa.Column(
                "purchasable",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )


def _create_tables(existing: set[str]) -> None:
    if _WALLET not in existing:
        op.create_table(
            _WALLET,
            sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), primary_key=True),
            sa.Column("balance", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
            sa.Column("total_earned", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
            sa.Column("total_spent", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )

    if _OWNERSHIP not in existing:
        op.create_table(
            _OWNERSHIP,
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("cosmetic_id", sa.String(), nullable=False),
            sa.Column("price_paid", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
            sa.Column("source", sa.String(), nullable=False, server_default=sa.text("'purchase'")),
            sa.Column("order_no", sa.String(), nullable=False, server_default=sa.text("''")),
            sa.Column("note", sa.String(), nullable=False, server_default=sa.text("''")),
            sa.Column("granted_by", sa.String(), nullable=True),
            sa.Column("acquired_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "user_id", "cosmetic_id", name="uq_fashion_ownership_user_cosmetic"
            ),
        )
        op.create_index(f"ix_{_OWNERSHIP}_user_id", _OWNERSHIP, ["user_id"])
        op.create_index(f"ix_{_OWNERSHIP}_cosmetic_id", _OWNERSHIP, ["cosmetic_id"])
        op.create_index(
            "ix_fashion_ownership_user_acquired", _OWNERSHIP, ["user_id", "acquired_at"]
        )
        op.create_index(f"ix_{_OWNERSHIP}_order_no", _OWNERSHIP, ["order_no"])

    if _TRANSACTION not in existing:
        op.create_table(
            _TRANSACTION,
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("delta", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
            sa.Column("balance_after", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
            sa.Column("tx_type", sa.String(), nullable=False, server_default=sa.text("'purchase'")),
            sa.Column("cosmetic_id", sa.String(), nullable=False, server_default=sa.text("''")),
            sa.Column("cosmetic_name", sa.String(), nullable=False, server_default=sa.text("''")),
            sa.Column("note", sa.String(), nullable=False, server_default=sa.text("''")),
            sa.Column("idempotency_key", sa.String(), nullable=True),
            sa.Column("operator_user_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(f"ix_{_TRANSACTION}_user_id", _TRANSACTION, ["user_id"])
        op.create_index(f"ix_{_TRANSACTION}_tx_type", _TRANSACTION, ["tx_type"])
        op.create_index(f"ix_{_TRANSACTION}_created_at", _TRANSACTION, ["created_at"])
        op.create_index(f"ix_{_TRANSACTION}_idempotency_key", _TRANSACTION, ["idempotency_key"], unique=True)
        op.create_index(
            "ix_fashion_mango_tx_user_created", _TRANSACTION, ["user_id", "created_at"]
        )


def _backfill(existing: set[str]) -> None:
    """历史衣柜 → 所有权 + 钱包（只在三张新表都已存在时跑）。"""
    needed = {_WARDROBE, _WALLET, _OWNERSHIP}
    if not needed.issubset(existing):
        return
    bind = op.get_bind()

    wardrobe_rows = bind.execute(
        sa.text(f"SELECT user_id, cosmetic_id, added_at FROM {_WARDROBE}")
    ).fetchall()
    if not wardrobe_rows:
        return

    owned = {
        (str(r[0]), str(r[1]))
        for r in bind.execute(
            sa.text(f"SELECT user_id, cosmetic_id FROM {_OWNERSHIP}")
        ).fetchall()
    }
    wallet_users = {
        str(r[0])
        for r in bind.execute(sa.text(f"SELECT user_id FROM {_WALLET}")).fetchall()
    }

    # 只为「确实有历史收藏」的账号建钱包；余额一律 0，不凭空发币
    for user_id, cosmetic_id, added_at in wardrobe_rows:
        uid, cid = str(user_id), str(cosmetic_id)
        if uid and uid not in wallet_users:
            bind.execute(
                sa.text(
                    f"INSERT INTO {_WALLET} "
                    "(user_id, balance, total_earned, total_spent, updated_at) "
                    "VALUES (:u, 0, 0, 0, :t)"
                ),
                {"u": uid, "t": added_at},
            )
            wallet_users.add(uid)
        if (uid, cid) in owned:
            continue
        bind.execute(
            sa.text(
                f"INSERT INTO {_OWNERSHIP} "
                "(id, user_id, cosmetic_id, price_paid, source, order_no, note, granted_by, acquired_at) "
                "VALUES (:i, :u, :c, 0, 'legacy_wardrobe', '', '上线前已收藏，自动继承所有权', NULL, :t)"
            ),
            {"i": str(uuid.uuid4()), "u": uid, "c": cid, "t": added_at},
        )
        owned.add((uid, cid))


def upgrade() -> None:
    existing = _existing_tables()
    _add_catalog_columns()
    _create_tables(existing)
    # 重新探测：_create_tables 可能刚建了表
    _backfill(_existing_tables())


def downgrade() -> None:
    existing = _existing_tables()
    # 逆序删表（先删不被外键引用的）
    for table in (_TRANSACTION, _OWNERSHIP, _WALLET):
        if table in existing:
            op.drop_table(table)

    if _CATALOG in existing:
        columns = _existing_columns(_CATALOG)
        # batch_alter_table：SQLite 不支持原生 DROP COLUMN 时自动重建表
        with op.batch_alter_table(_CATALOG) as batch_op:
            if "purchasable" in columns:
                batch_op.drop_column("purchasable")
            if "price_mango" in columns:
                batch_op.drop_column("price_mango")
