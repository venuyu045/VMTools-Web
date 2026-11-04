"""fashion: 补 ``fashion_asset.storage_path`` 索引

背景（2026-11-01 事故 §六.106）
-----------------------------
``/api/fashion/assets/{assetKey}`` 的回退分支（管理端资产预览 / 手工静态资产）
按 ``fashion_asset.storage_path`` **精确查**一行。没有索引时这是全表扫描：
生产 5666 行资产、时装页一次并发几十个资产请求 → SQLite 每次扫全表 →
连接池（5+10）瞬间打满 → 全站 504。

事故修复在 ORM 模型上加了 ``storage_path = Column(..., index=True)``，但：

- ``init_db()`` 的 ``Base.metadata.create_all(checkfirst=True)`` **只建缺失的表**，
  对已存在的表不会补建索引（SQLAlchemy 语义：不修改已有表）；
- ``20261028_fashion`` 迁移里也只建了 ``pack_id`` / ``sha256`` 两个索引。

于是：**任何「先跑 alembic 建表」的库（含按 ``docs/fashion-api.md`` §13.4 部署的
生产库）都不会有 ``ix_fashion_asset_storage_path``**，事故的性能修复实际上没生效。
本迁移把索引补齐（幂等：先探测索引/表是否存在）。

Revision ID: 20261103_fashion_asset_idx
Revises: 20261101_fashion_wardrobe
Create Date: 2026-11-03
"""
from __future__ import annotations

from typing import Optional

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20261103_fashion_asset_idx"
down_revision = "20261101_fashion_wardrobe"
branch_labels = None
depends_on = None

_TABLE = "fashion_asset"
_INDEX = "ix_fashion_asset_storage_path"


def _table_exists() -> bool:
    """``fashion_asset`` 是否存在；探测失败时按「存在」处理（交给索引探测兜底）。"""
    try:
        bind = op.get_bind()
        return _TABLE in set(sa.inspect(bind).get_table_names())
    except Exception:  # pragma: no cover - 极端降级路径（离线 stub / 无 bind）
        return True


def _index_names() -> Optional[set[str]]:
    """``fashion_asset`` 现有索引名；探测失败返回 ``None``（按「缺失」处理）。"""
    try:
        bind = op.get_bind()
        return {str(ix.get("name")) for ix in sa.inspect(bind).get_indexes(_TABLE)}
    except Exception:  # pragma: no cover - 极端降级路径
        return None


def upgrade() -> None:
    if not _table_exists():
        # 全新库：表由后续 ``init_db()`` 的 create_all 建，建表时会带上该索引
        return
    names = _index_names()
    if names is not None and _INDEX in names:
        return
    op.create_index(_INDEX, _TABLE, ["storage_path"])


def downgrade() -> None:
    if not _table_exists():
        return
    names = _index_names()
    if names is not None and _INDEX not in names:
        return
    op.drop_index(_INDEX, table_name=_TABLE)
