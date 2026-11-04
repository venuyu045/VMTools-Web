"""时装系统业务逻辑：目录 / 穿戴配置 / 批量解析 / 资产落盘。

分层约定：本模块只做「数据 + 文件」的事，不做 HTTP；异常统一抛
:class:`FashionServiceError`（带 ``code`` / ``status_code``），由路由层翻译。

热路径（``resolve_loadouts``）设计
---------------------------------
mod 在玩家进服时按 UUID 批量拉取穿戴配置，所以：

- 只返回 ``assetKey`` 级别的元数据（``assetKey`` / ``sha256`` / ``bytes``），
  **绝不内联资产字节**；资产走 ``GET /api/fashion/assets/{assetKey}`` 独立下载
- 单次最多 ``resolve_max_uuids``（默认 200）个 UUID，超出直接 400
- 进程内 TTL 缓存（默认 5s）挡住「同一批玩家反复进服」的重复查询
- 一次 SQL 批量取 identity、一次批量取 loadout（不是 N+1）

缓存是**进程内**的：多 worker 部署时各进程独立，靠短 TTL 自愈；管理端写入会
主动清掉本进程缓存（见 :func:`invalidate_caches`）。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import stat
import threading
import time
import uuid as uuid_lib
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from vmtools_next.core.fashion_auth import (
    AUTH_TYPE_MOJANG,
    FashionAuthError,
    normalize_uuid,
)
from vmtools_next.data.models.fashion import (
    FashionAssetModel,
    FashionCatalogModel,
    FashionIdentityModel,
    FashionLoadoutModel,
    FashionWardrobeModel,
)
from loguru import logger
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

# 槽位 = 时装类别（mod 侧渲染器按类别找模型）
SLOTS: tuple[str, ...] = ("cape", "wings", "hat", "back", "pet", "aura", "other")
CATEGORIES: tuple[str, ...] = SLOTS
SLOT_ORDER = {slot: idx for idx, slot in enumerate(SLOTS)}

MIN_SCALE = 0.25
MAX_SCALE = 4.0

# ── cosmeticId 契约（mod docs/fashion-api.md §3） ────────────────────────
#
# 客户端**只认** ``<packId>:<localId>`` 的冒号前缀来决定「要不要整包下发」
# （``sync/AssetSyncService.requestMissing``：没有冒号直接 return，既不下载也解析不到
# packId）。所以全局 ID 是唯一能让**其他玩家**看见这件时装的形态，后端必须：
#   1. 管理端写入时校验「冒号前缀 == packId 字段」，别让脏数据进目录；
#   2. 写 loadout 时**绝不把玩家上报的全局 ID 降级成简单 ID**。
# 简单 ID（无冒号）仍被接受：历史目录数据与旧客户端都还在用它，但它是 legacy 形态。
COSMETIC_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
# 全局 ID 里 localId 的字符集（比 packId 宽一点点：不参与路径拼接）
LOCAL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,96}$")

# 资产文件名允许的扩展名（避免上传 .php/.sh 之类被当成脚本）
ALLOWED_ASSET_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".glb", ".gltf", ".bin", ".ogg", ".wav", ".json", ".zip", ".bbmodel",
}
MAX_ASSET_BYTES = 64 * 1024 * 1024  # 64MB / 个

# ── 包级下发（mod 契约 docs/fashion-api.md §1/§2） ──────────────────────
#
# packId 会被客户端直接当目录名（packs/server/<packId>/），所以规则与
# net/PackCatalog.isSafePackId 完全一致：只允许 [A-Za-z0-9._-]{1,64}，且不能是
# "." / ".." / 以 "." 开头（客户端 .tmp-* 清理会误删）/ "server"（客户端命名空间）。
PACK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# version 会进 URL 路径段：只用 URL 里无需转义的字符，避免客户端 encodePathSegment
# 与后端路由解码不一致（"+" 在 query 里是空格，是经典坑）。
PACK_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PACK_ID_RESERVED = {"server"}

# 单个 zip 的硬上限：客户端 128MB（mod 契约 §2），后端资产上限更严（64MB）取小值
MAX_PACK_BYTES = 128 * 1024 * 1024

# zip 结构限制：与客户端 net/SafeZip 的 Limits 对齐（服务端先挡，玩家端才不会安装失败）
ZIP_MAX_ENTRIES = 4096
ZIP_MAX_ENTRY_BYTES = 64 * 1024 * 1024
ZIP_MAX_TOTAL_BYTES = 512 * 1024 * 1024
ZIP_MAX_NAME_LENGTH = 255
ZIP_RATIO_LIMIT = 100.0
ZIP_RATIO_MIN_UNCOMPRESSED = 10 * 1024 * 1024
ZIP_ALLOWED_METHODS = (0, 8)  # stored / deflate
ZIP_MAX_MANIFEST_BYTES = 1024 * 1024  # pack.json 上限 1MB（防止解压大文件进内存）

# 时装包里绝不该出现的东西（mod 契约 §6.8.1：拒绝可执行文件与隐藏文件）
FORBIDDEN_PACK_EXTS = {
    ".exe", ".dll", ".so", ".dylib", ".bat", ".cmd", ".com", ".msi", ".scr",
    ".ps1", ".vbs", ".sh", ".bash", ".zsh", ".py", ".pyc", ".jar", ".class",
    ".php", ".asp", ".aspx", ".jsp", ".cgi", ".lnk", ".url", ".reg",
}
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", "CLOCK$"} | {
    f"COM{i}" for i in range(1, 10)
} | {f"LPT{i}" for i in range(1, 10)}

# 仓库布局：backend/src/vmtools_next/core/fashion_service.py → parents[4] = 仓库根；
# Docker 布局：/app/src/vmtools_next/core/fashion_service.py → parents[4] = /（错误）。
# 优先按「同时存在 data/ 与 backend/（或 src/）」的目录定位项目根，兼容两种布局。
def _guess_project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "data").is_dir() and (
            (parent / "backend").is_dir() or (parent / "src").is_dir()
        ):
            return parent
    return here.parents[4] if len(here.parents) > 4 else here.parent


_REPO_ROOT = _guess_project_root()

# 文件名清洗：保留字母数字与 . _ -，其余替换成 -
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


class FashionServiceError(Exception):
    """业务错误：``code`` 给 mod 分支用，``status_code`` 给 HTTP 层用。"""

    def __init__(self, code: str, message: str = "", status_code: int = 400):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.status_code = status_code


def _commit_or_conflict(db: Any, *, what: str) -> None:
    """提交；并发触发唯一约束时转成 409 业务错误（审计 v1.2 P2-2）。

    典型场景：mod 与网页衣柜同时保存同一身份、同一 MC UUID 并发 verify、
    管理端并发创建同一条目录。路由只捕获 :class:`FashionServiceError`，
    这里统一翻译成 ``conflict``，避免把 ``IntegrityError`` 漏成 500。

    不做重试循环：客户端拿到 409 后整体重试即可，重试留在请求线程里只会
    放大写冲突。
    """
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        logger.warning("fashion: {} 并发写入冲突（已转 409）：{}", what, exc)
        raise FashionServiceError(
            "conflict", "数据已被其他请求修改，请重试", status_code=409
        ) from exc


# ── 配置 / 路径 ─────────────────────────────────────────────────────────

def _settings(settings: Any = None) -> Any:
    """取 FashionSettings：显式传入优先（测试用），否则读全局配置。

    全局配置读不到（未初始化 / 无密钥 / 离线工具）时退化为默认值，避免一次
    配置加载失败把目录读取和资产下载也带崩。
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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    """ISO-8601 UTC 字符串。SQLite 读回的 datetime 是 naive，按 UTC 补时区。"""
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def resolve_asset_root(settings: Any = None) -> Path:
    """资产根目录（相对路径按仓库根解析）。"""
    cfg = _settings(settings)
    raw = str(getattr(cfg, "asset_dir", "data/fashion-assets") or "data/fashion-assets")
    path = Path(raw)
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return path.resolve()


def ensure_asset_root(settings: Any = None) -> Path:
    root = resolve_asset_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_join(root: Path, relative: str) -> Path:
    """把相对路径安全地拼到 ``root`` 下（目录穿越防护）。

    Raises:
        FashionServiceError: ``invalid_path`` / ``path_traversal``
    """
    raw = str(relative or "").replace("\\", "/").strip()
    if not raw or "\x00" in raw:
        raise FashionServiceError("invalid_path", "资产路径为空或包含非法字符")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise FashionServiceError("invalid_path", "资产路径必须是相对路径")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts:
        raise FashionServiceError("invalid_path", "资产路径为空")
    if any(part == ".." for part in parts):
        raise FashionServiceError("path_traversal", "资产路径不允许包含 ..")

    root_resolved = root.resolve()
    candidate = (root_resolved / Path(*parts)).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise FashionServiceError("path_traversal", "资产路径越界")
    return candidate


# ── 缓存 ────────────────────────────────────────────────────────────────

_CACHE_LOCK = threading.Lock()
_CATALOG_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_RESOLVE_CACHE: dict[str, tuple[float, dict]] = {}
# 包清单快照（/packs + resolve 里的 packVersion 都读它）
_PACK_CACHE: dict[str, tuple[float, dict]] = {}
# 单个 storage_path 是否已登记（精确查缓存，替代全表扫描；§六.106）
_STORAGE_PATH_EXISTS_CACHE: dict[str, tuple[float, bool]] = {}
# resolve 缓存上限（防被大量不同 UUID 刷爆内存）
_RESOLVE_CACHE_MAX = 5000


def invalidate_caches() -> None:
    """清空本进程的目录/解析/包清单/资产路径缓存（管理端写入后调用）。"""
    with _CACHE_LOCK:
        _CATALOG_CACHE.clear()
        _RESOLVE_CACHE.clear()
        _PACK_CACHE.clear()
        _STORAGE_PATH_EXISTS_CACHE.clear()


def _catalog_cache_ttl(settings: Any = None) -> float:
    cfg = _settings(settings)
    try:
        return float(getattr(cfg, "catalog_cache_ttl_seconds", 30) or 0)
    except (TypeError, ValueError):
        return 0.0


def _resolve_cache_ttl(settings: Any = None) -> float:
    cfg = _settings(settings)
    try:
        return float(getattr(cfg, "resolve_cache_ttl_seconds", 5) or 0)
    except (TypeError, ValueError):
        return 0.0


def asset_storage_path_exists(db: Any, storage_path: str) -> bool:
    """``storage_path`` 是否确实登记在 ``fashion_asset`` 表里（精确查，走索引）。

    ⚠️ 2026-11-01 事故（§六.106）：替代原来的「拉全表建 set 再 in 判断」。
    带进程内 TTL 缓存（同一 key 30s 内只查一次），命中时零 DB 开销。
    """
    key = str(storage_path or "")
    if not key:
        return False
    now = time.time()
    with _CACHE_LOCK:
        hit = _STORAGE_PATH_EXISTS_CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    row = (
        db.query(FashionAssetModel.id)
        .filter(FashionAssetModel.storage_path == key)
        .first()
    )
    found = row is not None
    ttl = _catalog_cache_ttl()
    if ttl > 0:
        with _CACHE_LOCK:
            # 防御性上限：key 来自 URL，不能无限增长
            if len(_STORAGE_PATH_EXISTS_CACHE) >= 20000:
                for old in [k for k, v in _STORAGE_PATH_EXISTS_CACHE.items() if v[0] <= now]:
                    _STORAGE_PATH_EXISTS_CACHE.pop(old, None)
                if len(_STORAGE_PATH_EXISTS_CACHE) >= 20000:
                    _STORAGE_PATH_EXISTS_CACHE.clear()
            _STORAGE_PATH_EXISTS_CACHE[key] = (now + ttl, found)
    return found


# ── 目录（catalog） ─────────────────────────────────────────────────────

def _catalog_row_to_dict(row: Any) -> dict:
    return {
        "cosmeticId": getattr(row, "cosmetic_id", ""),
        "packId": getattr(row, "pack_id", "") or "",
        "name": getattr(row, "name", "") or "",
        "category": getattr(row, "category", "other") or "other",
        "assetKey": getattr(row, "asset_key", "") or "",
        "sha256": getattr(row, "sha256", "") or "",
        "bytes": int(getattr(row, "bytes", 0) or 0),
        "enabled": bool(getattr(row, "enabled", True)),
        "sortOrder": int(getattr(row, "sort_order", 0) or 0),
    }


def list_catalog(
    db: Any,
    *,
    enabled_only: bool = True,
    category: Optional[str] = None,
    pack_id: Optional[str] = None,
    settings: Any = None,
) -> list[dict]:
    """目录列表（按 sort_order, cosmeticId 升序），带进程内 TTL 缓存。"""
    key = (bool(enabled_only), category or "", pack_id or "")
    ttl = _catalog_cache_ttl(settings)
    now = time.time()
    if ttl > 0:
        with _CACHE_LOCK:
            hit = _CATALOG_CACHE.get(key)
            if hit and hit[0] > now:
                return [dict(item) for item in hit[1]]

    query = db.query(FashionCatalogModel)
    if enabled_only:
        query = query.filter_by(enabled=True)
    if category:
        query = query.filter_by(category=category)
    if pack_id:
        query = query.filter_by(pack_id=pack_id)
    rows = query.all() or []
    items = [_catalog_row_to_dict(row) for row in rows]
    items.sort(key=lambda it: (it["sortOrder"], it["cosmeticId"]))

    if ttl > 0:
        with _CACHE_LOCK:
            # 防御性上限：/catalog 的 category/pack_id 来自用户输入，
            # 不限制的话每个新参数组合都会留下一个 TTL 缓存条目
            if len(_CATALOG_CACHE) >= 256:
                for old_key in [k for k, v in _CATALOG_CACHE.items() if v[0] <= now]:
                    _CATALOG_CACHE.pop(old_key, None)
                if len(_CATALOG_CACHE) >= 256:
                    _CATALOG_CACHE.clear()
            _CATALOG_CACHE[key] = (now + ttl, [dict(item) for item in items])
    return items


def get_catalog_item(db: Any, cosmetic_id: str) -> Any:
    """按 cosmeticId 取目录行（不过滤 enabled）。"""
    return (
        db.query(FashionCatalogModel)
        .filter_by(cosmetic_id=str(cosmetic_id))
        .first()
    )


def get_catalog_by_asset_key(db: Any, asset_key: str) -> Any:
    return (
        db.query(FashionCatalogModel)
        .filter_by(asset_key=str(asset_key))
        .first()
    )


def split_global_cosmetic_id(cosmetic_id: Any) -> tuple[str, str]:
    """拆 ``<packId>:<localId>``，返回 ``(packId, localId)``；非全局 ID 返回 ``("", "")``。"""
    value = str(cosmetic_id or "").strip()
    if ":" not in value:
        return "", ""
    pack_id, _, local_id = value.partition(":")
    return pack_id.strip(), local_id.strip()


def _catalog_map(
    db: Any, *, enabled_only: bool = True, settings: Any = None
) -> dict[str, dict]:
    """``cosmeticId → 目录项``，并登记全局 ID ↔ 简单 ID 的**别名**。

    历史原因，目录里可能同时存在两种形态的 ID：

    - 新数据：全局 ID ``pack1:cape_dragon``（客户端可直接整包下发）
    - 旧数据：简单 ID ``cape_dragon`` + ``packId=pack1`` 字段

    而 loadout 里存的也可能是任一种形态（旧客户端上报简单 ID、mod 上报全局 ID）。
    为了两个方向都能查到同一件时装，这里除精确 ID 外再补一类别名：

    - 目录是简单 ID 且有 packId → 别名 ``<packId>:<cosmeticId>``
    - 目录是全局 ID → 别名 ``<localId>``

    精确 ID **永远优先**（先登记全部真实条目，别名只在缺失时补），所以别名不会
    覆盖真实条目；同一 localId 出现在多个包时按 ``sort_order, cosmeticId`` 取第一条
    （与 :func:`list_catalog` 的排序一致，行为可预期）。
    """
    items = list_catalog(db, enabled_only=enabled_only, settings=settings)
    mapping: dict[str, dict] = {item["cosmeticId"]: item for item in items}
    for item in items:
        cosmetic_id = item["cosmeticId"]
        _, local_id = split_global_cosmetic_id(cosmetic_id)
        if local_id:
            mapping.setdefault(local_id, item)
        else:
            pack_id = item.get("packId", "") or ""
            if pack_id:
                mapping.setdefault(f"{pack_id}:{cosmetic_id}", item)
    return mapping


def validate_cosmetic_id(cosmetic_id: Any, pack_id: Any = None) -> tuple[str, str]:
    """校验 ``cosmeticId`` 契约，返回 ``(cosmeticId, packId)``。

    规则（mod 契约 §3 / 站点文档 §4/§10）：

    - **全局形式** ``<packId>:<localId>``：只能有一个冒号，两段都不能为空，
      前缀必须能当客户端目录名（:func:`is_safe_pack_id`），localId 字符集受限；
      并且**冒号前缀必须等于 ``packId`` 字段**。只给了 ``cosmeticId`` 时按前缀推导
      ``packId``（等价于「前缀即 packId」）；只给了 ``packId`` 时由调用方先查旧行。
    - **简单形式**（无冒号）：历史目录数据 / 旧客户端仍在用，接受（legacy），
      ``packId`` 字段独立生效。

    Raises:
        FashionServiceError: ``invalid_cosmetic_id`` / ``invalid_pack_id``
    """
    cid = str(cosmetic_id or "").strip()
    pid = str(pack_id or "").strip()
    if not cid:
        raise FashionServiceError("invalid_cosmetic_id", "cosmeticId 不能为空")
    if not COSMETIC_ID_RE.match(cid):
        raise FashionServiceError(
            "invalid_cosmetic_id", "cosmeticId 只允许字母/数字/._:-，长度 ≤128"
        )
    if pid and not is_safe_pack_id(pid):
        raise FashionServiceError(
            "invalid_pack_id",
            "packId 只允许字母/数字/._-，长度 1-64，且不能是 . / .. / server / 点开头",
        )
    if ":" not in cid:
        return cid, pid

    parts = cid.split(":")
    if len(parts) != 2:
        raise FashionServiceError(
            "invalid_cosmetic_id",
            f"cosmeticId 必须形如 <packId>:<localId>（只能有一个冒号）：{cid}",
        )
    prefix, local_id = parts[0].strip(), parts[1].strip()
    if not prefix or not local_id:
        raise FashionServiceError(
            "invalid_cosmetic_id",
            f"cosmeticId 必须形如 <packId>:<localId>（两段都不能为空）：{cid}",
        )
    if not is_safe_pack_id(prefix):
        raise FashionServiceError(
            "invalid_cosmetic_id",
            f"cosmeticId 的 packId 前缀（{prefix}）不符合 packId 规则"
            "（字母/数字/._-，长度 1-64，不能是 . / .. / server / 点开头）",
        )
    if not LOCAL_ID_RE.match(local_id):
        raise FashionServiceError(
            "invalid_cosmetic_id",
            f"cosmeticId 的 localId 只允许字母/数字/._-，长度 1-96：{local_id}",
        )
    if pid and pid != prefix:
        raise FashionServiceError(
            "invalid_cosmetic_id",
            f"cosmeticId 的 packId 前缀（{prefix}）必须与 packId 字段（{pid}）一致",
        )
    # 只给了 cosmeticId：前缀即 packId（客户端就是按冒号前缀找包的）
    return cid, pid or prefix


def canonical_loadout_cosmetic_id(catalog_item: dict, reported_id: Any) -> str:
    """写 loadout 用的规范 ID：**全局 ID 优先，但前缀必须与目录 packId 一致**。

    1. 目录条目本身就是全局 ID → 用它（最权威，能保证与其他玩家一致）；
    2. 目录是 legacy 简单 ID，且玩家上报的全局 ID **前缀 == 目录 packId** →
       保留上报的全局 ID（客户端靠冒号前缀整包下发）；
    3. 两边都是简单 ID → 保持简单 ID（旧数据/旧客户端不动，向后兼容）；
    4. 目录是 legacy 简单 ID 但 `packId` 为空 / 上报前缀对不上 → **回目录 ID**。

    第 2 条是 v1.1 修复的核心（旧实现把 ``example:backboard`` 改写成目录里的
    ``backboard`` 落库，客户端因为「没有冒号前缀」永远不会整包下发这件时装）。

    第 4 条是 v1.2 补的边界：旧实现无条件保留上报的全局 ID，于是
    ``pack2:backboard``（目录 packId=pack1）或目录 packId 为空时会落库一个
    ``_catalog_map`` 里不存在的 ID → ``loadout/me`` 立刻 missing、``resolve`` 丢弃，
    玩家**静默丢件**。回目录 ID 至少保证自己/其他玩家能按 legacy 简单 ID 解析到。
    """
    catalog_id = str((catalog_item or {}).get("cosmeticId") or "").strip()
    if ":" in catalog_id:
        return catalog_id
    reported = str(reported_id or "").strip()
    if ":" in reported:
        pack_id = str((catalog_item or {}).get("packId") or "").strip()
        prefix, _, _ = reported.partition(":")
        if pack_id and prefix.strip() == pack_id:
            return reported
        logger.warning(
            "fashion: 上报的 cosmeticId 前缀与目录 packId 不一致，已按目录 ID 保存 "
            "catalog={} packId={} reported={}",
            catalog_id,
            pack_id,
            reported,
        )
    return catalog_id or reported


def upsert_catalog_item(db: Any, payload: dict) -> dict:
    """管理端新增/更新目录条目。``payload`` 用 camelCase 键（与 API 一致）。

    部分更新语义：payload 里没出现的字段保持原值（创建时用默认值），
    避免「只改 enabled」把 name / packId / assetKey 等顺手清空。

    ``cosmeticId`` 按 :func:`validate_cosmetic_id` 校验：全局形式
    ``<packId>:<localId>`` 的冒号前缀必须等于 ``packId`` 字段（只给其一时按规则推导），
    否则 ``invalid_cosmetic_id`` —— 前缀对不上会让客户端去下载一个不存在的包。
    """
    raw_cosmetic_id = payload.get("cosmeticId")
    # 先按原样查旧行：部分更新时 packId 可能不在 payload 里，校验要用旧值
    row = get_catalog_item(db, str(raw_cosmetic_id or "").strip())
    existing_pack_id = str(getattr(row, "pack_id", "") or "") if row is not None else ""
    if "packId" in payload:
        pack_id = str(payload.get("packId") or "").strip()
    else:
        pack_id = existing_pack_id
    cosmetic_id, pack_id = validate_cosmetic_id(raw_cosmetic_id, pack_id)
    if "category" in payload:
        category = str(payload.get("category") or "other").strip().lower()
        if category not in CATEGORIES:
            raise FashionServiceError(
                "invalid_category", f"category 必须是 {', '.join(CATEGORIES)} 之一"
            )
    if "assetKey" in payload:
        asset_key = str(payload.get("assetKey") or "").strip()
        if asset_key:
            # assetKey 会成为下载 URL 的 path，先做一次穿越校验
            try:
                safe_join(Path("/tmp"), asset_key)
            except FashionServiceError as exc:
                raise FashionServiceError("invalid_asset_key", exc.message) from exc

    created = row is None
    if created:
        row = FashionCatalogModel(cosmetic_id=cosmetic_id, created_at=utcnow())
        db.add(row)
        # 显式落默认值：SQLAlchemy 的 Column default 在 flush 时才生效，
        # 而这里要在 commit 前就返回完整对象（也让离线假 Session 行为一致）
        row.pack_id = ""
        row.name = cosmetic_id
        row.category = "other"
        row.asset_key = ""
        row.enabled = True
        row.sort_order = 0
        row.sha256 = ""
        row.bytes = 0
    # 全局 ID 的前缀就是权威 packId（validate 已保证两者一致 / 已推导）
    row.pack_id = pack_id[:64]
    if "name" in payload:
        row.name = str(payload.get("name") or "")[:128]
    if "category" in payload:
        row.category = category
    if "assetKey" in payload:
        row.asset_key = asset_key[:256]
    if "sha256" in payload and payload.get("sha256") is not None:
        row.sha256 = str(payload.get("sha256") or "")[:64]
    if "bytes" in payload and payload.get("bytes") is not None:
        row.bytes = max(0, int(payload.get("bytes") or 0))
    if payload.get("enabled") is not None:
        row.enabled = bool(payload.get("enabled"))
    if payload.get("sortOrder") is not None:
        row.sort_order = int(payload.get("sortOrder") or 0)
    _commit_or_conflict(db, what="catalog")
    invalidate_caches()
    logger.info("fashion catalog {} cosmetic={}", "created" if created else "updated", cosmetic_id)
    return _catalog_row_to_dict(row)


def delete_catalog_item(db: Any, cosmetic_id: str) -> bool:
    row = get_catalog_item(db, cosmetic_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    invalidate_caches()
    logger.info("fashion catalog deleted cosmetic={}", cosmetic_id)
    return True


# ── 身份（identity） ────────────────────────────────────────────────────

def identity_to_dict(row: Any) -> dict:
    return {
        "id": getattr(row, "id", ""),
        "mcUuid": getattr(row, "mc_uuid", ""),
        "mcName": getattr(row, "mc_name", ""),
        "authType": getattr(row, "auth_type", AUTH_TYPE_MOJANG) or AUTH_TYPE_MOJANG,
        "authServer": getattr(row, "auth_server", "") or "",
        "verified": bool(getattr(row, "verified_at", None)),
        "verifiedAt": iso(getattr(row, "verified_at", None)),
        "userId": getattr(row, "user_id", None),
        "createdAt": iso(getattr(row, "created_at", None)),
        "lastSeenAt": iso(getattr(row, "last_seen_at", None)),
    }


def get_identity(db: Any, identity_id: str) -> Any:
    return (
        db.query(FashionIdentityModel)
        .filter_by(id=str(identity_id))
        .first()
    )


def get_identity_by_uuid(db: Any, mc_uuid: str) -> Any:
    return (
        db.query(FashionIdentityModel)
        .filter_by(mc_uuid=normalize_uuid(mc_uuid))
        .first()
    )


def _dt_sort_key(value: Any) -> float:
    """把可能为 None / naive / aware 的 datetime 转成可比较的浮点时间戳。

    SQLite 读回来的是 naive datetime，写入时是 aware，直接比较会 TypeError。
    """
    if value is None:
        return 0.0
    try:
        return float(value.timestamp())
    except Exception:
        return 0.0


def list_identities_for_user(db: Any, user_id: str) -> list[Any]:
    rows = (
        db.query(FashionIdentityModel)
        .filter_by(user_id=str(user_id))
        .all()
        or []
    )
    return sorted(
        rows,
        key=lambda r: _dt_sort_key(
            getattr(r, "last_seen_at", None) or getattr(r, "created_at", None)
        ),
        reverse=True,
    )


def upsert_identity(
    db: Any,
    *,
    mc_uuid: str,
    mc_name: str,
    auth_type: str,
    auth_server: str = "",
    user_id: Optional[str] = None,
) -> Any:
    """验证成功后落库（一个 mc_uuid 一行，重复验证只刷新）。"""
    uuid_norm = normalize_uuid(mc_uuid)
    now = utcnow()
    row = get_identity_by_uuid(db, uuid_norm)
    if row is None:
        row = FashionIdentityModel(
            id=str(uuid_lib.uuid4()),
            mc_uuid=uuid_norm,
            mc_name=str(mc_name),
            auth_type=auth_type or AUTH_TYPE_MOJANG,
            auth_server=auth_server or "",
            created_at=now,
            verified_at=now,
            last_seen_at=now,
            user_id=user_id,
        )
        db.add(row)
    else:
        row.mc_name = str(mc_name)
        row.auth_type = auth_type or AUTH_TYPE_MOJANG
        row.auth_server = auth_server or ""
        row.verified_at = now
        row.last_seen_at = now
        if user_id:
            row.user_id = str(user_id)
    _commit_or_conflict(db, what="identity")
    return row


def bind_identity_to_user(db: Any, identity: Any, user_id: str) -> Any:
    if identity is None:
        raise FashionServiceError("identity_not_found", "身份不存在", status_code=404)
    identity.user_id = str(user_id)
    db.commit()
    logger.info("fashion identity bound identity={} user={}", getattr(identity, "id", ""), user_id)
    return identity


def unbind_identity(db: Any, identity: Any) -> Any:
    if identity is None:
        raise FashionServiceError("identity_not_found", "身份不存在", status_code=404)
    identity.user_id = None
    db.commit()
    return identity


# ── 穿戴配置（loadout） ─────────────────────────────────────────────────

def _loadout_row_to_dict(row: Any) -> dict:
    return {
        "slot": getattr(row, "slot", ""),
        "cosmeticId": getattr(row, "cosmetic_id", ""),
        "scale": float(getattr(row, "scale", 1.0) or 1.0),
    }


def _clamp_scale(value: Any) -> float:
    try:
        scale = float(value)
    except (TypeError, ValueError):
        raise FashionServiceError("invalid_scale", "scale 必须是数字")
    if scale != scale or scale in (float("inf"), float("-inf")):
        raise FashionServiceError("invalid_scale", "scale 必须是有限数字")
    return round(min(MAX_SCALE, max(MIN_SCALE, scale)), 4)


def get_loadout_rows(db: Any, identity_id: str) -> list[Any]:
    return (
        db.query(FashionLoadoutModel)
        .filter_by(identity_id=str(identity_id))
        .all()
        or []
    )


def _lookup_catalog_item(
    db: Any, cosmetic_id: str, catalog: Optional[dict[str, dict]] = None
) -> Optional[dict]:
    """把玩家上报的 cosmeticId 解析成目录条目（返回 dict，查不到返回 None）。

    三级兜底，顺序即优先级：

    1. 精确 DB 查询（最新数据，不吃缓存）；
    2. 旧逻辑：全局 ID 精确查不到时用冒号后的 ``localId`` 再查一次
       （兼容「目录里是简单 ID」的历史数据）；
    3. 目录别名表（:func:`_catalog_map` 已登记全局 ID ↔ 简单 ID 双向别名，
       覆盖「目录是全局 ID、客户端上报简单 ID」的反方向）。
    """
    row = get_catalog_item(db, cosmetic_id)
    if row is not None:
        return _catalog_row_to_dict(row)
    _, local_id = split_global_cosmetic_id(cosmetic_id)
    if local_id:
        row = get_catalog_item(db, local_id)
        if row is not None:
            return _catalog_row_to_dict(row)
    if catalog:
        return catalog.get(cosmetic_id)
    return None


def get_loadout(db: Any, identity_id: str, *, settings: Any = None) -> list[dict]:
    """玩家穿戴配置（含目录元数据，方便网页衣柜直接渲染）。"""
    rows = get_loadout_rows(db, identity_id)
    catalog = _catalog_map(db, enabled_only=False, settings=settings)
    # 包级下发（mod 契约 §3）：每件时装补 packId / packVersion
    versions = pack_versions(db, settings=settings)
    items: list[dict] = []
    for row in rows:
        item = _loadout_row_to_dict(row)
        meta = catalog.get(item["cosmeticId"])
        if meta is None:
            # 目录条目被删了：仍然返回，assetKey 置空，mod/网页按「无资产」处理
            item.update({
                "assetKey": "", "sha256": "", "bytes": 0, "name": "",
                "category": "other", "missing": True,
                "packId": "", "packVersion": "",
            })
        else:
            pack_id = meta.get("packId", "") or ""
            item.update({
                "assetKey": meta["assetKey"],
                "sha256": meta["sha256"],
                "bytes": meta["bytes"],
                "name": meta["name"],
                "category": meta["category"],
                "missing": not meta["enabled"],
                "packId": pack_id,
                "packVersion": versions.get(pack_id.lower(), ""),
            })
        items.append(item)
    items.sort(key=lambda it: SLOT_ORDER.get(it["slot"], 99))
    return items


def put_loadout(
    db: Any,
    identity_id: str,
    items: Iterable[dict],
    *,
    settings: Any = None,
) -> tuple[datetime, list[dict]]:
    """全量覆盖写入穿戴配置（未出现的槽位 = 卸下）。

    ``items`` 每项：``{"slot": ..., "cosmeticId": ..., "scale": 1.0}``。
    ``scale`` 可省略：省略时沿用该槽位已保存的缩放值（mod 不保存 scale，
    不能让它每次换装都把网页衣柜的缩放重置成 1.0）。

    ``cosmeticId`` 落库规则（mod 契约 §3）：**写规范全局 ID**。目录条目本身就是
    ``<packId>:<localId>`` 时原样落库；目录仍是历史简单 ID 而玩家上报了全局 ID 时，
    保留玩家上报的全局 ID（**绝不降级成简单 ID** —— 客户端只认冒号前缀，降级会让
    这件时装永远不被整包下发）。两边都是简单 ID 时保持简单 ID（向后兼容）。

    **部分成功语义**（mod 契约补充）：目录里查不到 / 已下架的 ``cosmeticId`` 只跳过
    该条并返回 warning，其余照常写入 —— 否则 mod 首次启动自带的本地示例包
    （``example:crown`` 等）会让整条 PUT 404，玩家连本地其它时装都同步不了。
    只有**请求体本身非法**（slot 非法 / 重复 / 超条数 / scale 非数字 / cosmeticId 空）
    才整条 4xx 拒绝。

    Returns:
        ``(updated_at, warnings)``；``warnings`` 每项
        ``{"slot", "cosmeticId", "code"}``（code: ``unknown_cosmetic`` / ``cosmetic_disabled``）。

    Raises:
        FashionServiceError: 请求体非法（槽位 / 条数 / scale）
    """
    cfg = _settings(settings)
    max_items = int(getattr(cfg, "max_loadout_items", 8) or 8)
    payload = list(items or [])
    if len(payload) > max_items:
        raise FashionServiceError(
            "too_many_items", f"最多 {max_items} 个穿戴槽位"
        )

    existing = {getattr(row, "slot", ""): row for row in get_loadout_rows(db, identity_id)}
    # 目录快照（含 disabled）：用来做「全局 ID ↔ 简单 ID」双向兜底匹配
    catalog = _catalog_map(db, enabled_only=False, settings=cfg)
    seen: set[str] = set()
    normalized: list[dict] = []
    warnings: list[dict] = []
    for raw in payload:
        slot = str((raw or {}).get("slot") or "").strip().lower()
        if slot not in SLOTS:
            raise FashionServiceError(
                "invalid_slot", f"slot 必须是 {', '.join(SLOTS)} 之一"
            )
        if slot in seen:
            raise FashionServiceError("duplicate_slot", f"slot {slot} 重复")
        seen.add(slot)
        cosmetic_id = str((raw or {}).get("cosmeticId") or "").strip()
        if not cosmetic_id:
            raise FashionServiceError("invalid_cosmetic_id", "cosmeticId 不能为空")
        cosmetic = _lookup_catalog_item(db, cosmetic_id, catalog)
        if cosmetic is None:
            # 未知时装（典型：mod 本地包的 example:crown）只跳过该条，不整条失败
            warnings.append(
                {"slot": slot, "cosmeticId": cosmetic_id, "code": "unknown_cosmetic"}
            )
            continue
        # 落库用规范全局 ID：绝不把玩家上报的 packId:localId 降级成简单 ID
        # （客户端只认冒号前缀决定整包下发，降级 = 这件时装永远不自动下发）
        canonical_id = canonical_loadout_cosmetic_id(cosmetic, cosmetic_id)
        # 防御（v1.2 P2-1）：规范 ID 必须能在目录别名表里解析到，否则落库后
        # `loadout/me` 会 missing、`resolve` 会丢弃 —— 宁可跳过并给 warning，
        # 也不要把一个「永远解析不到」的 ID 静默写进库。
        if canonical_id not in catalog:
            warnings.append(
                {"slot": slot, "cosmeticId": cosmetic_id, "code": "unknown_cosmetic"}
            )
            continue
        if not bool(cosmetic.get("enabled", True)):
            warnings.append(
                {"slot": slot, "cosmeticId": cosmetic_id, "code": "cosmetic_disabled"}
            )
            continue
        raw_scale = (raw or {}).get("scale") if isinstance(raw, dict) else None
        if raw_scale is None:
            previous = existing.get(slot)
            try:
                scale = float(getattr(previous, "scale", 1.0) or 1.0)
            except (TypeError, ValueError):
                scale = 1.0
            scale = round(min(MAX_SCALE, max(MIN_SCALE, scale)), 4)
        else:
            scale = _clamp_scale(raw_scale)
        normalized.append(
            {
                "slot": slot,
                "cosmeticId": canonical_id,
                "scale": scale,
            }
        )

    now = utcnow()
    keep: set[str] = set()
    for item in normalized:
        keep.add(item["slot"])
        row = existing.get(item["slot"])
        if row is None:
            row = FashionLoadoutModel(
                id=str(uuid_lib.uuid4()),
                identity_id=str(identity_id),
                slot=item["slot"],
                cosmetic_id=item["cosmeticId"],
                scale=item["scale"],
                updated_at=now,
            )
            db.add(row)
        else:
            row.cosmetic_id = item["cosmeticId"]
            row.scale = item["scale"]
            row.updated_at = now
    for slot, row in existing.items():
        if slot not in keep:
            db.delete(row)

    _commit_or_conflict(db, what="loadout")
    # 本进程缓存立即失效（其他 worker 靠 5s TTL 自愈）
    with _CACHE_LOCK:
        _RESOLVE_CACHE.clear()
    logger.info(
        "fashion loadout updated identity={} slots={} skipped={}",
        identity_id, sorted(keep), [item["cosmeticId"] for item in warnings],
    )
    return now, warnings


# ── 我的衣柜（wardrobe 收藏） ────────────────────────────────────────────
#
# 与穿戴配置的区别：**衣柜是网站账号的收藏夹（user 维度）**，一个账号绑定的多个
# MC 身份共享同一份；穿戴配置（fashion_loadout）才是身份维度。商店页「加入衣柜」
# 走 add_*，衣柜页列表走 list_*，单件/批量删除走 remove_*。

# 一次批量删除的上限：前端是分页勾选，500 足够覆盖「整页全选」；再放宽只会让
# 一次请求携带超长 id 列表，收益为零。
MAX_WARDROBE_BATCH_DELETE = 500


def _wardrobe_rows(db: Any, user_id: str) -> list[Any]:
    """某用户衣柜的全部行（不过滤目录状态 —— 目录没了也要能删掉）。"""
    return (
        db.query(FashionWardrobeModel)
        .filter_by(user_id=str(user_id))
        .all()
        or []
    )


def _wardrobe_item_dict(
    cosmetic_id: Any, added_at: Any, meta: Optional[dict]
) -> dict:
    """衣柜条目对外形状（camelCase，字段与 ``WardrobeItemOut`` 一一对应）。

    ``meta`` 是目录条目（:func:`_catalog_row_to_dict` 的形状）。为 None 或
    ``enabled=False`` 时标 ``missing=True``：**条目照常返回**，玩家才删得掉它；
    目录被删除时没有任何残留元数据可用，name 等字段给空值。
    """
    item = {
        "cosmeticId": str(cosmetic_id or ""),
        "name": "",
        "category": "other",
        "packId": "",
        "assetKey": "",
        "sha256": "",
        "bytes": 0,
        "sortOrder": 0,
        "addedAt": iso(added_at),
        "missing": True,
    }
    if meta is not None:
        item.update({
            "name": meta.get("name", "") or "",
            "category": meta.get("category", "other") or "other",
            "packId": meta.get("packId", "") or "",
            "assetKey": meta.get("assetKey", "") or "",
            "sha256": meta.get("sha256", "") or "",
            "bytes": int(meta.get("bytes", 0) or 0),
            "sortOrder": int(meta.get("sortOrder", 0) or 0),
            # 目录还在但被下架（enabled=False）同样算 missing：mod 不会再下发它
            "missing": not bool(meta.get("enabled", True)),
        })
    return item


def list_wardrobe(db: Any, user_id: str, *, settings: Any = None) -> list[dict]:
    """我的衣柜（按加入时间**倒序**，最近加入的排最前）。"""
    rows = _wardrobe_rows(db, user_id)
    # 目录快照含 disabled：missing 要能区分「目录里被停用」和「目录里没有」
    catalog = _catalog_map(db, enabled_only=False, settings=settings)
    # 用 _dt_sort_key 排序：SQLite 读回的是 naive datetime，直接比较会 TypeError。
    # cosmetic_id 作次键，保证同一时刻加入的条目顺序稳定（分页/前端 diff 不抖动）。
    rows.sort(
        key=lambda row: (
            _dt_sort_key(getattr(row, "added_at", None)),
            str(getattr(row, "cosmetic_id", "")),
        ),
        reverse=True,
    )
    return [
        _wardrobe_item_dict(
            getattr(row, "cosmetic_id", ""),
            getattr(row, "added_at", None),
            catalog.get(str(getattr(row, "cosmetic_id", ""))),
        )
        for row in rows
    ]


def add_wardrobe_item(db: Any, user_id: str, cosmetic_id: Any) -> tuple[dict, bool]:
    """把一件时装加入衣柜；已收藏时幂等返回 ``(item, False)``。

    **落库用规范 ID**（与 ``put_loadout`` 同一套 ``canonical_loadout_cosmetic_id``
    规则）：商店页传的是目录原值，历史目录里可能是简单 ID，而 mod 端
    ``WardrobeCollection`` 拿 ``cosmeticId`` 跟本地已装包的**全局 ID** 比对 ——
    存简单 ID 会在「同一 localId 分布在多个包」时匹配不上（短 ID 别名有歧义），
    也会让 ``/wardrobe/me`` 返回一个客户端认不出的形态。

    幂等判定同时看「上报值」和「规范值」：旧版本按简单 ID 存过的行，再次加入
    时不该因为 ID 形态不同而插出第二条。

    Raises:
        FashionServiceError: ``invalid_cosmetic_id``（空 / 超长 / 字符非法，400）、
            ``cosmetic_not_found``（目录里没有或已停用，404）
    """
    # validate_cosmetic_id 同时挡掉空串与 >128 字符（COSMETIC_ID_RE 是 {1,128}）
    cid, _pack_id = validate_cosmetic_id(cosmetic_id)
    # _lookup_catalog_item 在 get_catalog_item 之上补了全局 ID ↔ 简单 ID 的别名
    # 兜底（与 loadout 同一套规则）：商店页传目录里的原值，这里也能吃到历史数据。
    # 必须把 catalog 快照传进去 —— 别名兜底（第三级）只在有快照时生效，
    # 否则「目录是全局 ID、调用方传简单 ID」的反方向永远解析不到。
    catalog = _catalog_map(db, enabled_only=False)
    meta = _lookup_catalog_item(db, cid, catalog)
    if meta is None or not bool(meta.get("enabled", True)):
        raise FashionServiceError(
            "cosmetic_not_found", "时装不存在或已下架", status_code=404
        )
    stored_id = canonical_loadout_cosmetic_id(meta, cid) or cid
    # 幂等判定要认「同一件时装的任意历史形态」：上报值 / 规范值 / 目录原值
    # 都算同一件（旧版本按简单 ID 存过、新版按全局 ID 存的都会命中）。
    candidates = {cid, stored_id, str(meta.get("cosmeticId") or "").strip()}
    candidates.discard("")
    row = next(
        (
            existing
            for existing in _wardrobe_rows(db, user_id)
            if str(getattr(existing, "cosmetic_id", "")) in candidates
        ),
        None,
    )
    added = False
    if row is None:
        row = FashionWardrobeModel(
            id=str(uuid_lib.uuid4()),
            user_id=str(user_id),
            cosmetic_id=stored_id,
            added_at=utcnow(),
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # 并发双击「加入衣柜」：另一请求先插入了同一行，唯一约束兜底。
            # 这是幂等接口，不该把 409 甩给前端 —— 回滚后按「已存在」处理。
            db.rollback()
            row = next(
                (
                    existing
                    for existing in _wardrobe_rows(db, user_id)
                    if str(getattr(existing, "cosmetic_id", "")) in candidates
                ),
                None,
            )
            if row is None:  # pragma: no cover - 约束冲突却查不到行，说明库异常
                raise FashionServiceError(
                    "conflict", "数据已被其他请求修改，请重试", status_code=409
                )
        else:
            added = True
    elif str(getattr(row, "cosmetic_id", "")) != stored_id:
        # 历史行是简单 ID / 另一种形态：就地升级成目录的规范 ID。
        # 不新增行（幂等），但让 /wardrobe/me 逐步收敛到客户端能匹配的全局 ID。
        row.cosmetic_id = stored_id
        try:
            db.commit()
        except IntegrityError:
            # 极端情况：库里同时存在「简单 ID」与「规范 ID」两条同一件（历史并发写入，
            # 或管理端把目录 ID 从简单形态迁移成全局 ID 后玩家又加了一次）。这时升级
            # 会撞 (user_id, cosmetic_id) 唯一约束 —— 加入衣柜是幂等接口，不能因此 500：
            # 回滚保持原样，玩家在列表里删掉多余那条即可。
            db.rollback()
            row = next(
                (
                    existing
                    for existing in _wardrobe_rows(db, user_id)
                    if str(getattr(existing, "cosmetic_id", "")) == stored_id
                ),
                row,
            )
    return (
        _wardrobe_item_dict(
            getattr(row, "cosmetic_id", stored_id),
            getattr(row, "added_at", None),
            meta,
        ),
        added,
    )


def remove_wardrobe_item(db: Any, user_id: str, cosmetic_id: Any) -> bool:
    """从衣柜移除单件，返回是否真的删掉了（不存在返回 False，不算错误）。"""
    cid = str(cosmetic_id or "").strip()
    if not cid:
        return False
    row = (
        db.query(FashionWardrobeModel)
        .filter_by(user_id=str(user_id), cosmetic_id=cid)
        .first()
    )
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def normalize_wardrobe_ids(cosmetic_ids: Iterable[Any]) -> list[str]:
    """批量入参规范化：去空白、丢空串与超长（>128 不可能存在于库里）、保序去重。"""
    result: list[str] = []
    seen: set[str] = set()
    for raw in cosmetic_ids or []:
        cid = str(raw or "").strip()
        if not cid or len(cid) > 128 or cid in seen:
            continue
        seen.add(cid)
        result.append(cid)
    return result


def remove_wardrobe_items(db: Any, user_id: str, cosmetic_ids: Iterable[Any]) -> int:
    """批量移除衣柜条目，返回**真正删掉**的条数；不存在的 id 静默忽略。

    Raises:
        FashionServiceError: ``empty_selection``（去重后为空）、``too_many_items``
            （去重后超过 :data:`MAX_WARDROBE_BATCH_DELETE`）
    """
    ids = normalize_wardrobe_ids(cosmetic_ids)
    if not ids:
        raise FashionServiceError("empty_selection", "请先选择要删除的时装")
    if len(ids) > MAX_WARDROBE_BATCH_DELETE:
        raise FashionServiceError(
            "too_many_items", f"一次最多删除 {MAX_WARDROBE_BATCH_DELETE} 件时装"
        )
    wanted = set(ids)
    # 单用户衣柜规模受目录规模约束（有界），先按 user 取回再按集合过滤：
    # 比拼 500 个 IN 占位符的语句更简单，也不会踩 SQLite 的变量数上限。
    rows = [
        row
        for row in _wardrobe_rows(db, user_id)
        if str(getattr(row, "cosmetic_id", "")) in wanted
    ]
    for row in rows:
        db.delete(row)
    if rows:
        db.commit()
    logger.info(
        "fashion wardrobe batch-remove user={} requested={} removed={}",
        user_id, len(ids), len(rows),
    )
    return len(rows)


# ── 批量解析（热路径） ──────────────────────────────────────────────────

def _resolve_item(
    row: Any, catalog: dict[str, dict], versions: Optional[dict[str, str]] = None
) -> dict:
    """resolve 只给 assetKey 级别元数据，不含任何资产内容。"""
    cosmetic_id = getattr(row, "cosmetic_id", "")
    meta = catalog.get(cosmetic_id)
    if meta is None or not meta["enabled"]:
        return {}
    pack_id = meta.get("packId", "") or ""
    return {
        "slot": getattr(row, "slot", ""),
        "cosmeticId": cosmetic_id,
        "assetKey": meta["assetKey"],
        "sha256": meta["sha256"],
        "bytes": meta["bytes"],
        "scale": float(getattr(row, "scale", 1.0) or 1.0),
        # 包级下发（mod 契约 §3）：客户端据此知道要不要整包下载
        "packId": pack_id,
        "packVersion": (versions or {}).get(pack_id.lower(), ""),
    }


def resolve_loadouts(
    db: Any,
    mc_uuids: Iterable[str],
    *,
    settings: Any = None,
) -> dict:
    """按 UUID 批量解析穿戴配置。

    Returns:
        ``{"loadouts": {uuid: {"items": [...]}}, "missing": [uuid, ...]}``

    Raises:
        FashionServiceError: ``too_many_uuids`` / ``invalid_request``
    """
    cfg = _settings(settings)
    limit = int(getattr(cfg, "resolve_max_uuids", 200) or 200)
    if mc_uuids is None or isinstance(mc_uuids, (str, bytes)):
        raise FashionServiceError("invalid_request", "mcUuids 必须是数组")
    raw_list = list(mc_uuids)
    if len(raw_list) > limit:
        raise FashionServiceError(
            "too_many_uuids", f"单次最多 {limit} 个 mcUuid", status_code=400
        )

    ordered: list[str] = []
    invalid: list[str] = []
    for raw in raw_list:
        try:
            norm = normalize_uuid(raw)
        except FashionAuthError:
            invalid.append(str(raw))
            continue
        if norm not in ordered:
            ordered.append(norm)

    now = time.time()
    ttl = _resolve_cache_ttl(cfg)
    result: dict[str, dict] = {}
    pending: list[str] = []

    if ttl > 0:
        with _CACHE_LOCK:
            for uuid_str in ordered:
                hit = _RESOLVE_CACHE.get(uuid_str)
                if hit and hit[0] > now:
                    result[uuid_str] = dict(hit[1])
                else:
                    pending.append(uuid_str)
    else:
        pending = list(ordered)

    if pending:
        catalog = _catalog_map(db, enabled_only=True, settings=cfg)
        # 包级下发（mod 契约 §3）：resolve 的每件时装也要带 packId / packVersion
        versions = pack_versions(db, settings=cfg)
        found_identities: list[Any] = []
        # 分块查询，避免 SQLite 变量上限（默认 999）
        for start in range(0, len(pending), 100):
            chunk = pending[start:start + 100]
            rows = (
                db.query(FashionIdentityModel)
                .filter(FashionIdentityModel.mc_uuid.in_(chunk))
                .all()
                or []
            )
            found_identities.extend(rows)

        identity_by_uuid = {
            getattr(row, "mc_uuid", ""): row for row in found_identities
        }
        identity_ids = [getattr(row, "id", "") for row in found_identities]
        loadout_by_identity: dict[str, list[Any]] = {}
        if identity_ids:
            for start in range(0, len(identity_ids), 100):
                chunk = identity_ids[start:start + 100]
                rows = (
                    db.query(FashionLoadoutModel)
                    .filter(FashionLoadoutModel.identity_id.in_(chunk))
                    .all()
                    or []
                )
                for row in rows:
                    loadout_by_identity.setdefault(getattr(row, "identity_id", ""), []).append(row)

        for uuid_str in pending:
            identity = identity_by_uuid.get(uuid_str)
            if identity is None:
                continue
            rows = loadout_by_identity.get(getattr(identity, "id", ""), [])
            items = [
                item
                for item in (_resolve_item(row, catalog, versions) for row in rows)
                if item
            ]
            items.sort(key=lambda it: SLOT_ORDER.get(it["slot"], 99))
            payload = {"items": items}
            result[uuid_str] = payload
            if ttl > 0:
                with _CACHE_LOCK:
                    if len(_RESOLVE_CACHE) > _RESOLVE_CACHE_MAX:
                        # 防内存膨胀：先清过期项，仍超限则整体清空（宁可多查库）
                        for key in [k for k, v in _RESOLVE_CACHE.items() if v[0] <= now]:
                            _RESOLVE_CACHE.pop(key, None)
                        if len(_RESOLVE_CACHE) > _RESOLVE_CACHE_MAX:
                            _RESOLVE_CACHE.clear()
                    _RESOLVE_CACHE[uuid_str] = (now + ttl, dict(payload))

    missing = [uuid_str for uuid_str in ordered if uuid_str not in result]
    missing.extend(invalid)
    return {"loadouts": result, "missing": missing}


# ── 包级下发（packs） ───────────────────────────────────────────────────
#
# mod 的运行时单位是**目录**（model.geo.json + texture.png + emissive/anim），
# 不是单个文件；所以除了「单件资产」还要一层包级契约（mod docs/fashion-api.md）：
#
#   GET /packs                         清单（id/version/sha256/bytes/url/publishedAt）
#   GET /packs/{id}/{version}/download 整包 zip 字节流（200 直出，不重定向）
#
# 包的"发布"就是管理端上传一个 <packId>.zip：fashion_asset 行已经存了
# pack_id / version / sha256 / bytes / storage_path，足够派生清单，无需新表。

def is_safe_pack_id(pack_id: Any) -> bool:
    """packId 能否安全用作目录名 / URL 路径段（与客户端 PackCatalog 同规则）。"""
    value = str(pack_id or "").strip()
    if not PACK_ID_RE.match(value):
        return False
    if value in (".", ".."):
        return False
    if value.startswith("."):
        # 客户端用 ".tmp-*" 前缀做临时目录，点开头的包会被它当残留删掉
        return False
    if value.lower() in PACK_ID_RESERVED:
        return False
    return True


def normalize_pack_version(version: Any) -> str:
    """校验包版本号（≤64，仅 URL 安全字符）。"""
    value = str(version or "").strip()
    if not PACK_VERSION_RE.match(value):
        raise FashionServiceError(
            "invalid_pack_version", "packVersion 只允许字母/数字/._-，长度 1-64"
        )
    return value


def pack_download_url(pack_id: str, version: str) -> str:
    """清单里的相对下载地址（相对 mod 的 apiBaseUrl = ``.../api/fashion``）。

    客户端 ``PackDownloader.resolveUri`` 会把以 ``/`` 开头的相对地址拼到
    ``apiBaseUrl`` 后面，所以这里**不能**写成 ``/api/fashion/packs/...``。
    """
    return f"/packs/{pack_id}/{version}/download"


def _dt_millis(value: Any) -> int:
    """datetime → 毫秒时间戳（mod 的 publishedAt 是 number，不是 ISO 字符串）。"""
    if value is None:
        return 0
    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)
    except Exception:  # pragma: no cover - 脏数据兜底
        return 0


def pack_canonical_string(pack_id: str, version: str, sha256: str) -> bytes:
    """Ed25519 签名覆盖的规范化字节串（mod 契约 §6.1，逐字节不得改动）。

    ``UTF-8(trim(packId) + "\\n" + trim(version) + "\\n" + lowercase(sha256hex))``
    —— 三段之间单个 ``\\n``，结尾没有换行。
    """
    return (
        f"{str(pack_id or '').strip()}\n"
        f"{str(version or '').strip()}\n"
        f"{str(sha256 or '').strip().lower()}"
    ).encode("utf-8")


def _ed25519_signer(key_b64: str):
    """从 base64 私钥构造 Ed25519 签名函数（裸 32 字节种子或 PKCS#8 DER）。

    私钥只应来自后端配置（环境变量 / KMS），**绝不进客户端仓库**。
    ``cryptography`` 是可选依赖：没装时抛 ImportError，由调用方降级为「不签名」。
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = base64.b64decode(str(key_b64 or "").strip(), validate=True)
    if len(raw) == 32:
        key = Ed25519PrivateKey.from_private_bytes(raw)
    else:
        key = serialization.load_der_private_key(raw, password=None)
    return key.sign


def pack_signature(
    pack_id: str,
    version: str,
    sha256: str,
    *,
    settings: Any = None,
    signer: Any = None,
) -> tuple[str, str]:
    """给一条清单签名，返回 ``(signature_b64, signingKeyId)``。

    - 没配置私钥 → ``("", "")``：清单里不输出签名字段，客户端按宽松模式放行
      （默认）或严格模式拒绝（运维开了 requireSignature 时）；
    - 配置了私钥但 ``cryptography`` 没装 / 私钥坏 → 记 warn 后同样返回空，
      **绝不让签名失败把 /packs 变成 500**（客户端会因此关掉整个自动下发）。
    """
    cfg = _settings(settings)
    key_b64 = str(getattr(cfg, "pack_signing_key_b64", "") or "").strip()
    if not key_b64:
        return "", ""
    key_id = str(getattr(cfg, "pack_signing_key_id", "") or "").strip()
    try:
        sign = signer or _ed25519_signer(key_b64)
        signature = sign(pack_canonical_string(pack_id, version, sha256))
    except Exception as exc:
        logger.warning("fashion: 包签名失败（已降级为不签名）pack={} err={}", pack_id, exc)
        return "", ""
    return base64.b64encode(bytes(signature)).decode("ascii"), key_id


def _zip64_or_broken(data: bytes) -> bool:
    """EOCD 里出现 0xFFFF/0xFFFFFFFF 说明用了 ZIP64（客户端直接拒绝）。"""
    idx = data.rfind(b"PK\x05\x06")
    if idx < 0 or idx + 22 > len(data):
        return True
    eocd = data[idx : idx + 22]
    if eocd[8:10] == b"\xff\xff" or eocd[10:12] == b"\xff\xff":
        return True  # 条目数溢出（磁盘/总数）
    if eocd[12:16] == b"\xff\xff\xff\xff" or eocd[16:20] == b"\xff\xff\xff\xff":
        return True  # 中央目录大小/偏移溢出
    return False


def _zip_entry_problem(info: zipfile.ZipInfo) -> Optional[tuple[str, str]]:
    """单条目名安全校验，返回 ``(code, message)`` 或 None。

    规则逐条对齐客户端 ``SafeZip.normalizeEntryName``：反斜杠归一 → 空名/NUL/超长
    → 绝对路径/UNC → 盘符 → 逐段（``..``、``:``、Windows 保留名、点/空格结尾）。
    """
    raw = info.filename or ""
    if not raw:
        return "invalid_pack_zip", "zip 条目名为空"
    if len(raw) > ZIP_MAX_NAME_LENGTH:
        return "invalid_pack_zip", f"zip 条目名过长：{raw[:40]}…"
    if "\x00" in raw:
        return "invalid_pack_zip", "zip 条目名含 NUL 字符"
    name = raw.replace("\\", "/")
    if name.startswith("/"):
        return "invalid_pack_zip", f"zip 条目使用绝对路径：{raw}"
    if len(name) >= 2 and name[1] == ":" and name[0].isalpha():
        return "invalid_pack_zip", f"zip 条目含 Windows 盘符：{raw}"
    for part in name.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            return "invalid_pack_zip", f"zip 条目路径含 ..：{raw}"
        if ":" in part:
            return "invalid_pack_zip", f"zip 条目含 NTFS 数据流分隔符：{raw}"
        base = part.split(".", 1)[0].upper()
        if base in _WINDOWS_RESERVED:
            return "invalid_pack_zip", f"zip 条目是 Windows 保留设备名：{raw}"
        if part.endswith(".") or part.endswith(" "):
            return "invalid_pack_zip", f"zip 条目名以点/空格结尾（Windows 会裁剪）：{raw}"
    if not name.strip("/"):
        return "invalid_pack_zip", f"zip 条目名规范化后为空：{raw}"
    return None


def validate_pack_zip(
    data: bytes, *, expected_pack_id: str = "", settings: Any = None
) -> dict:
    """服务端 zip 安全校验（mod 契约 §2 / §6.8.1：坏包在上传时就被挡住）。

    与客户端 ``SafeZip`` 同一套规则（条目数 / 单文件 / 总大小 / 压缩比 / 名字 /
    链接 / 加密 / 未知算法 / ZIP64 / 可执行文件 / 隐藏文件），外加「根目录必须有
    ``pack.json`` 且 ``id`` 与 packId 一致」（``expected_pack_id`` 非空时校验）。

    Returns:
        ``{"id": str, "files": int, "uncompressed": int}``

    Raises:
        FashionServiceError: ``invalid_pack_zip`` / ``pack_too_large``
    """
    if not data:
        raise FashionServiceError("empty_asset", "上传内容为空")
    if len(data) > MAX_PACK_BYTES:
        raise FashionServiceError(
            "pack_too_large",
            f"单个时装包不能超过 {MAX_PACK_BYTES // (1024 * 1024)}MB",
            status_code=413,
        )
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise FashionServiceError("invalid_pack_zip", "不是合法的 zip 文件")
    if _zip64_or_broken(data):
        raise FashionServiceError("invalid_pack_zip", "不支持 ZIP64 格式（客户端会直接拒绝）")

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except Exception as exc:  # pragma: no cover - is_zipfile 已挡掉绝大多数
        raise FashionServiceError("invalid_pack_zip", f"zip 结构损坏：{exc}") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > ZIP_MAX_ENTRIES:
            raise FashionServiceError(
                "invalid_pack_zip", f"zip 条目数超过 {ZIP_MAX_ENTRIES}"
            )
        seen: set[str] = set()
        file_paths: set[str] = set()
        dir_paths: set[str] = set()
        manifest_info: Optional[zipfile.ZipInfo] = None
        total = 0
        for info in infos:
            problem = _zip_entry_problem(info)
            if problem:
                raise FashionServiceError(*problem)
            # 规范化条目名（反斜杠归一、丢弃空段与 "."），后续所有比较都用它
            name = "/".join(
                part
                for part in (info.filename or "").replace("\\", "/").split("/")
                if part and part != "."
            )
            lowered = name.lower()
            # 仅大小写不同的重复条目：Windows / macOS 上会互相覆盖
            if lowered in seen:
                raise FashionServiceError(
                    "invalid_pack_zip", f"zip 条目重复（大小写不敏感）：{info.filename}"
                )
            seen.add(lowered)
            # 符号链接 / 设备 / FIFO 等非普通文件。只判**文件类型位**：zipfile.writestr
            # 写的 external_attr 只有权限位（0o600）没有 S_IFREG，用 S_ISREG 判断会把
            # 所有正常条目误判成符号链接。
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            file_type = unix_mode & 0o170000  # S_IFMT
            if file_type and file_type not in (stat.S_IFREG, stat.S_IFDIR):
                raise FashionServiceError(
                    "invalid_pack_zip", f"zip 条目不是普通文件（可能是符号链接）：{info.filename}"
                )
            if info.flag_bits & 0x1:
                raise FashionServiceError("invalid_pack_zip", "不支持加密 zip")
            if info.compress_type not in ZIP_ALLOWED_METHODS:
                raise FashionServiceError(
                    "invalid_pack_zip",
                    f"不支持压缩算法 {info.compress_type}（只允许 stored / deflate）",
                )
            if info.is_dir():
                dir_paths.add(lowered)
                continue
            if name == "pack.json":
                # 根目录清单（大小写敏感：客户端安装后按 "pack.json" 精确读取）
                manifest_info = info
            if info.file_size > ZIP_MAX_ENTRY_BYTES:
                raise FashionServiceError(
                    "invalid_pack_zip",
                    f"单个条目解压后超过 {ZIP_MAX_ENTRY_BYTES // (1024 * 1024)}MB：{info.filename}",
                )
            total += info.file_size
            if total > ZIP_MAX_TOTAL_BYTES:
                raise FashionServiceError("invalid_pack_zip", "解压后总大小超过上限")
            ext = Path(name).suffix.lower()
            if ext in FORBIDDEN_PACK_EXTS:
                raise FashionServiceError(
                    "invalid_pack_zip", f"时装包不允许包含可执行文件：{info.filename}"
                )
            parts = [p for p in name.split("/") if p]
            if any(p.startswith(".") for p in parts):
                raise FashionServiceError(
                    "invalid_pack_zip", f"时装包不允许包含隐藏文件：{info.filename}"
                )
            file_paths.add(lowered)

        # 文件与目录同名冲突（a 与 a/b）
        for path in file_paths:
            segments = path.split("/")
            for idx in range(1, len(segments)):
                parent = "/".join(segments[:idx])
                if parent in file_paths:
                    raise FashionServiceError(
                        "invalid_pack_zip", f"zip 里文件与目录同名：{parent}"
                    )
        for path in file_paths:
            if path in dir_paths:
                raise FashionServiceError(
                    "invalid_pack_zip", f"zip 里文件与目录同名：{path}"
                )

        # zip bomb：解压后总量很大但压缩比异常
        if total > ZIP_RATIO_MIN_UNCOMPRESSED and len(data) > 0:
            if total / len(data) > ZIP_RATIO_LIMIT:
                raise FashionServiceError(
                    "invalid_pack_zip", "zip 压缩比异常（疑似 zip bomb）"
                )

        # 包结构：根目录必须有 pack.json，且 id 与清单 id 一致（mod 契约 §2）。
        if manifest_info is None:
            raise FashionServiceError(
                "invalid_pack_zip", "时装包根目录必须有 pack.json（文件名大小写敏感）"
            )
        if manifest_info.file_size > ZIP_MAX_MANIFEST_BYTES:
            raise FashionServiceError(
                "invalid_pack_zip",
                f"pack.json 不能超过 {ZIP_MAX_MANIFEST_BYTES // 1024}KB",
            )
        try:
            manifest = archive.read(manifest_info)
            parsed = json.loads(manifest.decode("utf-8"))
        except Exception as exc:
            raise FashionServiceError(
                "invalid_pack_zip", f"pack.json 无法解析：{exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise FashionServiceError("invalid_pack_zip", "pack.json 必须是对象")
        manifest_id = str(parsed.get("id") or "").strip()
        if not manifest_id:
            raise FashionServiceError("invalid_pack_zip", "pack.json 缺少 id")
        if expected_pack_id and manifest_id.lower() != str(expected_pack_id).strip().lower():
            raise FashionServiceError(
                "pack_id_mismatch",
                f"pack.json 的 id（{manifest_id}）与 packId（{expected_pack_id}）不一致",
            )

    return {"id": manifest_id, "files": len(file_paths), "uncompressed": total}


def _pack_row_payload(row: Any, settings: Any = None) -> Optional[dict]:
    """把一条 ``.zip`` 资产行转成清单项；不可下发的条目返回 None（客户端也会丢弃）。"""
    pack_id = str(getattr(row, "pack_id", "") or "").strip()
    version = str(getattr(row, "version", "") or "").strip()
    sha256 = str(getattr(row, "sha256", "") or "").strip().lower()
    try:
        size = int(getattr(row, "bytes", 0) or 0)
    except (TypeError, ValueError):
        size = 0
    if not is_safe_pack_id(pack_id) or not PACK_VERSION_RE.match(version):
        return None
    if len(sha256) != 64 or size <= 0:
        return None
    payload = {
        "id": pack_id,
        "version": version,
        "sha256": sha256,
        "bytes": size,
        "url": pack_download_url(pack_id, version),
        "publishedAt": _dt_millis(getattr(row, "created_at", None)),
    }
    signature, key_id = pack_signature(pack_id, version, sha256, settings=settings)
    if signature:
        payload["signature"] = signature
        payload["signingKeyId"] = key_id
    return payload


def _pack_snapshot(db: Any, settings: Any = None) -> dict:
    """包清单快照：``{"packs": [...], "versions": {packId小写: version}}``。

    同一 ``(pack_id, version)`` 有多条 zip 记录时取 ``created_at`` 最新的一条；
    缓存 TTL 复用 catalog 的（默认 30s），管理端写入后 ``invalidate_caches()`` 立即失效。
    """
    cfg = _settings(settings)
    ttl = _catalog_cache_ttl(cfg)
    now = time.time()
    if ttl > 0:
        with _CACHE_LOCK:
            hit = _PACK_CACHE.get("snapshot")
            if hit and hit[0] > now:
                return hit[1]

    # ⚠️ 2026-11-01 事故（§六.106）同源优化：只在 SQL 里取 .zip 行。
    # 原实现 `db.query(FashionAssetModel).all()` 会把**全部**资产行（生产 5000+）
    # 物化成 ORM 对象再在 Python 里过滤 —— 而 /packs 与 resolve/loadout 的
    # packVersion 都读这份快照，缓存一过期就在请求线程里做一次全表物化。
    # SQLite 的 LIKE 对 ASCII 大小写不敏感，`func.lower()` 再兜一层保证跨库一致。
    rows = (
        db.query(FashionAssetModel)
        .filter(func.lower(FashionAssetModel.filename).like("%.zip"))
        .all()
        or []
    )
    zips = [
        row
        for row in rows
        if str(getattr(row, "filename", "") or "").lower().endswith(".zip")
    ]
    zips.sort(key=lambda r: _dt_sort_key(getattr(r, "created_at", None)), reverse=True)
    packs: list[dict] = []
    versions: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()
    for row in zips:
        payload = _pack_row_payload(row, cfg)
        if payload is None:
            continue
        key = (payload["id"].lower(), payload["version"])
        if key in seen:
            continue
        seen.add(key)
        packs.append(payload)
        # 同一 packId 只认最新的一条（客户端也按 (packId, sha256) 判重）
        versions.setdefault(payload["id"].lower(), payload["version"])

    packs.sort(key=lambda item: (item["id"].lower(), item["version"]))
    snapshot = {"packs": packs, "versions": versions}
    if ttl > 0:
        with _CACHE_LOCK:
            _PACK_CACHE["snapshot"] = (now + ttl, snapshot)
    return snapshot


def list_packs(db: Any, *, settings: Any = None) -> list[dict]:
    """``GET /packs`` 的清单（公开）。"""
    return [dict(item) for item in _pack_snapshot(db, settings)["packs"]]


def pack_versions(db: Any, *, settings: Any = None) -> dict[str, str]:
    """``packId(小写) → 当前 version``，供 resolve / loadout 回填 ``packVersion``。"""
    return dict(_pack_snapshot(db, settings)["versions"])


def get_pack_asset(
    db: Any, pack_id: Any, version: Any, *, settings: Any = None
) -> Optional[dict]:
    """按 (packId, version) 找可下载的 zip 资产行。

    先做**严格格式校验**再查库：非法 id/version 直接当不存在（防路径穿越 / 注入）。
    """
    if not is_safe_pack_id(pack_id):
        return None
    try:
        version_norm = normalize_pack_version(version)
    except FashionServiceError:
        return None
    pack_norm = str(pack_id).strip()
    rows = (
        db.query(FashionAssetModel)
        .filter_by(pack_id=pack_norm, version=version_norm)
        .all()
        or []
    )
    rows = [
        row
        for row in rows
        if str(getattr(row, "filename", "") or "").lower().endswith(".zip")
    ]
    if not rows:
        return None
    rows.sort(key=lambda r: _dt_sort_key(getattr(r, "created_at", None)), reverse=True)
    for row in rows:
        payload = _pack_row_payload(row, settings)
        if payload is not None:
            payload["storagePath"] = getattr(row, "storage_path", "") or ""
            return payload
    return None


def pack_entry(
    db: Any, pack_id: Any, version: Any, *, settings: Any = None
) -> Optional[dict]:
    """单个包的清单项（管理端上传后的返回值用；不含磁盘路径）。"""
    payload = get_pack_asset(db, pack_id, version, settings=settings)
    if payload is None:
        return None
    return {key: value for key, value in payload.items() if key != "storagePath"}


def delete_pack(db: Any, pack_id: Any, version: Any, *, settings: Any = None) -> bool:
    """下架一个包（删 DB 行，磁盘文件保留；sha256 内容寻址，重新上传会复用路径）。

    下架 = 从 ``/packs`` 消失（mod 契约 §6.8.3），已下载到玩家本地的包不受影响。
    若目录里有条目引用了这个 zip 的 sha256（管理端把 zip 当单件资产挂过），拒绝删除。
    """
    if not is_safe_pack_id(pack_id):
        raise FashionServiceError("invalid_pack_id", "packId 非法")
    version_norm = normalize_pack_version(version)
    pack_norm = str(pack_id).strip()
    rows = [
        row
        for row in (
            db.query(FashionAssetModel)
            .filter_by(pack_id=pack_norm, version=version_norm)
            .all()
            or []
        )
        if str(getattr(row, "filename", "") or "").lower().endswith(".zip")
    ]
    if not rows:
        return False
    digests = {str(getattr(row, "sha256", "") or "") for row in rows}
    referenced = [
        row
        for digest in digests
        if digest
        for row in (db.query(FashionCatalogModel).filter_by(sha256=digest).all() or [])
    ]
    if referenced:
        raise FashionServiceError(
            "pack_in_use",
            "该包的 sha256 仍被目录条目引用，请先调整目录再下架",
            status_code=409,
        )
    for row in rows:
        db.delete(row)
    db.commit()
    invalidate_caches()
    logger.info("fashion pack unpublished id={} version={}", pack_norm, version_norm)
    return True


# ── 资产 ────────────────────────────────────────────────────────────────

def _sanitize_filename(filename: str) -> str:
    """清洗上传文件名，**保留原始扩展名**。

    为什么不能整体替换：`[^A-Za-z0-9._-] → "-"` 会连扩展名的分隔点一起吃掉 ——
    中文/带空格的文件名（``深渊龙翼.zip``、``a b.png``）清洗后是 ``-.zip`` →
    ``strip("-.")`` → ``zip``，扩展名消失，于是被 :func:`_asset_ext` 判成
    "无扩展名" → ``unsupported_asset_type`` 400（运维只看到"不支持的资产类型"，
    完全猜不到是文件名语言的问题）。所以先摘出扩展名单独清洗，再拼回去。
    纯 ASCII 文件名的结果与旧实现完全一致（``dragon.png`` → ``dragon.png``）。

    Raises:
        FashionServiceError: ``invalid_filename``（清洗后既没有主名也没有扩展名）。
    """
    raw = Path(str(filename or "")).name
    suffix = Path(raw).suffix  # 含点（".zip"）；没有扩展名时为空串
    stem = raw[: -len(suffix)] if suffix else raw
    stem = _FILENAME_SAFE_RE.sub("-", stem).strip("-.")
    ext = _FILENAME_SAFE_RE.sub("", suffix).strip(".-")
    if not stem and not ext:
        raise FashionServiceError("invalid_filename", "文件名非法")
    if not ext:
        base = stem
    else:
        # 主名缺失（纯中文文件名）时给个占位，否则只剩 ".png" 这种以点开头的名字
        base = f"{stem or 'file'}.{ext}"
    if len(base) > 120:
        # 扩展名过长（异常文件名）时不再保留它，直接硬截断，保证结果 ≤120
        if ext and len(ext) < 100:
            keep = max(1, 120 - len(ext) - 1)
            base = f"{base[:keep]}.{ext}"
        else:
            base = base[:120]
    return base


def _asset_ext(filename: str) -> str:
    return Path(filename).suffix.lower()


def store_asset(
    db: Any,
    *,
    pack_id: str,
    version: str,
    filename: str,
    data: bytes,
    settings: Any = None,
    pack_zip: bool = False,
) -> dict:
    """资产落盘 + 记录（同 sha256 去重），返回资产元数据。

    ``pack_zip=True`` 表示这是一个**时装包**（整包 zip，会被 ``/packs`` 下发）：
    此时 packId / version 必须严格合法（不做静默清洗，否则清单里的 id 与客户端
    目录名对不上）。

    **所有 ``.zip`` 上传都按时装包校验**（结构安全 + 根目录 ``pack.json`` 且 id 与
    packId 一致）：这样 ``/packs`` 里出现的每一条都是客户端真能安装的包，坏包在
    服务端就被挡住，玩家端不会出现"安装失败"。单件资产请用 png/glb/json 等格式。
    """
    if not data:
        raise FashionServiceError("empty_asset", "上传内容为空")
    if len(data) > MAX_ASSET_BYTES:
        raise FashionServiceError(
            "asset_too_large", f"单个资产不能超过 {MAX_ASSET_BYTES // (1024 * 1024)}MB",
            status_code=413,
        )
    safe_name = _sanitize_filename(filename)
    ext = _asset_ext(safe_name)
    if ext not in ALLOWED_ASSET_EXTS:
        raise FashionServiceError(
            "unsupported_asset_type",
            f"不支持的资产类型 {ext or '(无扩展名)'}，允许：{', '.join(sorted(ALLOWED_ASSET_EXTS))}",
        )

    if pack_zip:
        # 包级下发：packId 会成为客户端目录名，必须原样合法（不做静默清洗）
        if not is_safe_pack_id(pack_id):
            raise FashionServiceError(
                "invalid_pack_id",
                "packId 只允许字母/数字/._-，长度 1-64，且不能是 . / .. / server / 点开头",
            )
        ver = normalize_pack_version(version)
        pack = str(pack_id).strip()
        if ext != ".zip":
            raise FashionServiceError("invalid_pack_zip", "时装包必须是 .zip 文件")
    else:
        pack = _FILENAME_SAFE_RE.sub("-", str(pack_id or "default")).strip("-.") or "default"
        ver = _FILENAME_SAFE_RE.sub("-", str(version or "1")).strip("-.") or "1"

    if ext == ".zip":
        # zip 一律当包处理：结构安全 + pack.json 存在且 id 与 packId 一致。
        # 这样 /packs 里的每一条都保证能被客户端安装（客户端会校验 pack.json.id）。
        if not is_safe_pack_id(pack):
            raise FashionServiceError(
                "invalid_pack_id",
                "packId 只允许字母/数字/._-，长度 1-64，且不能是 . / .. / server / 点开头",
            )
        validate_pack_zip(data, expected_pack_id=pack, settings=settings)

    digest = hashlib.sha256(data).hexdigest()

    # 去重键带上 pack/version：同一个 zip 内容以不同版本号发布时必须各占一行，
    # 否则第二个版本永远进不了 /packs 清单
    existing = (
        db.query(FashionAssetModel)
        .filter_by(sha256=digest, filename=safe_name, pack_id=pack, version=ver)
        .first()
    )
    if existing is not None:
        return _asset_row_to_dict(existing)

    rel_path = f"{pack}/{ver}/{digest[:16]}-{safe_name}"
    root = ensure_asset_root(settings)
    target = safe_join(root, rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 临时名带随机后缀：并发上传同一资产时不会互相覆盖
    tmp = target.with_name(f"{target.name}.{uuid_lib.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except Exception:
        # 写盘失败 / 磁盘满：清掉临时文件，别在资产目录里留垃圾
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:  # pragma: no cover - 清理失败不影响错误上抛
            pass
        raise

    row = FashionAssetModel(
        id=str(uuid_lib.uuid4()),
        pack_id=pack,
        version=ver,
        filename=safe_name,
        sha256=digest,
        bytes=len(data),
        storage_path=rel_path,
        created_at=utcnow(),
    )
    db.add(row)
    _commit_or_conflict(db, what="asset")
    # 新包 / 新资产立刻反映到 /packs 与 resolve 的 packVersion（不等 TTL 自愈）
    invalidate_caches()
    logger.info(
        "fashion asset stored pack={} version={} file={} bytes={}",
        pack, ver, safe_name, len(data),
    )
    return _asset_row_to_dict(row)


def _asset_row_to_dict(row: Any) -> dict:
    return {
        "id": getattr(row, "id", ""),
        "packId": getattr(row, "pack_id", "") or "",
        "version": getattr(row, "version", "") or "",
        "filename": getattr(row, "filename", "") or "",
        "sha256": getattr(row, "sha256", "") or "",
        "bytes": int(getattr(row, "bytes", 0) or 0),
        "storagePath": getattr(row, "storage_path", "") or "",
        "createdAt": iso(getattr(row, "created_at", None)),
    }


def list_assets(db: Any, pack_id: Optional[str] = None) -> list[dict]:
    query = db.query(FashionAssetModel)
    if pack_id:
        query = query.filter_by(pack_id=str(pack_id))
    rows = query.all() or []
    rows = sorted(
        rows,
        key=lambda r: _dt_sort_key(getattr(r, "created_at", None)),
        reverse=True,
    )
    return [_asset_row_to_dict(row) for row in rows]


def resolve_asset_file(
    db: Any, asset_key: str, *, settings: Any = None
) -> Optional[dict]:
    """把 ``assetKey`` 解析成磁盘文件。

    解析顺序：
    1. catalog.asset_key → catalog.sha256 → fashion_asset.storage_path（上传流）
    2. catalog.asset_key 直接作为相对路径（本地静态目录起步流）

    Returns:
        ``{"path": Path, "sha256": str, "bytes": int, "etag": str}`` 或 None
    """
    key = str(asset_key or "").strip()
    if not key or len(key) > 512:
        # 超长 key 直接当不存在：既防 DB 全表扫，也避免把异常长路径传进 safe_join
        return None
    try:
        catalog_row = get_catalog_by_asset_key(db, key)
    except Exception:  # DB 不可用不应让下载 500
        catalog_row = None

    if catalog_row is not None and not bool(getattr(catalog_row, "enabled", True)):
        return None

    root = resolve_asset_root(settings)
    digest = str(getattr(catalog_row, "sha256", "") or "") if catalog_row is not None else ""

    if digest:
        asset_row = (
            db.query(FashionAssetModel)
            .filter_by(sha256=digest)
            .first()
        )
        if asset_row is not None:
            try:
                path = safe_join(root, getattr(asset_row, "storage_path", ""))
            except FashionServiceError:
                path = None
            if path is not None and path.is_file():
                return {
                    "path": path,
                    "sha256": digest,
                    "bytes": path.stat().st_size,
                    "etag": f'"{digest}"',
                }

    # 回退：只允许「目录里登记过、但没有 sha256」的手工静态资产（docs §6.2 的
    # 「本地静态目录起步流」），或「确实在 fashion_asset 表里登记过的 storage_path」
    # （管理端资产列表预览直接用 storagePath 拼 URL）。安全（审计 v1.2 P2-3）：
    # 不再把任意相对路径当资产公开——否则 asset root 下的 README / DB / 误放文件
    # 都能被匿名读取，且已下架资产可绕开 catalog 的 enabled 检查。
    if catalog_row is None and not asset_storage_path_exists(db, key):
        return None
    if _asset_ext(key) not in ALLOWED_ASSET_EXTS:
        logger.warning("fashion: 拒绝非资产扩展名的 assetKey {}", key)
        return None
    try:
        path = safe_join(root, key)
    except FashionServiceError:
        logger.warning("fashion: 拒绝非法 assetKey {}", key)
        return None
    # 双保险：safe_join 内部已 resolve 并校验过，这里再显式确认一次落在 asset root 内
    try:
        path = path.resolve()
    except OSError:  # pragma: no cover - 极端文件系统错误
        return None
    root_resolved = root.resolve()
    if path != root_resolved and root_resolved not in path.parents:
        logger.warning("fashion: assetKey 越界 {}", key)
        return None
    if path.is_file():
        size = path.stat().st_size
        mtime = int(path.stat().st_mtime)
        etag = digest or f"{size}-{mtime}"
        return {"path": path, "sha256": digest, "bytes": size, "etag": f'"{etag}"'}
    return None
