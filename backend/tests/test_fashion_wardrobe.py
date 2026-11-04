"""「我的衣柜」收藏 API 单测（service + 路由接线 + 迁移）。

背景：商店页（/fashion/store）与衣柜页（/fashion/wardrobe）此前共用「全站目录」，
用户要求只有**主动加入衣柜**的时装才出现在衣柜里，且支持单件/批量删除。后端为此
新增 ``fashion_wardrobe`` 表 + 4 个接口（见 task-1 契约）。

离线可跑：本机没装 fastapi/sqlalchemy，走 ``_stub_env`` 的 stub + 本文件的
``FakeSession``（与 test_fashion_loadout.py / test_fashion_api.py 同一套写法）。

路由层断言：stub 的 ``_Router`` 没有 ``.head``，直接 import ``routers/fashion.py``
会炸（这也是 test_fashion_api / test_fashion_http 在离线环境里的既有错误），
所以 :func:`_import_fashion_router` 在导入期间临时补一个 ``.head`` 再立刻还原，
只在**本文件**生效，不污染其它测试模块。

跑法::

    cd backend && python3 -m unittest tests.test_fashion_wardrobe -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _stub_env import install, quiet_stub_noise  # noqa: E402

install()
quiet_stub_noise()

from vmtools_next.core import fashion_service as svc  # noqa: E402
from vmtools_next.core.fashion_service import FashionServiceError  # noqa: E402
from vmtools_next.data.models.fashion import (  # noqa: E402
    FashionCatalogModel,
    FashionWardrobeModel,
)

USER_A = "user-aaaa-1111"
USER_B = "user-bbbb-2222"

# 契约里 WardrobeItemOut 的字段集合（对外 camelCase，一个字都不能少/多）
_WARDROBE_ITEM_KEYS = {
    "cosmeticId", "name", "category", "packId", "assetKey",
    "sha256", "bytes", "sortOrder", "addedAt", "missing",
}


# ── 最小假 Session（内存表 + filter_by 语义，同 test_fashion_loadout 的写法） ──

class _FakeQuery:
    def __init__(self, session: "FakeSession", model):
        self.session = session
        self.model = model
        self._preds: list[tuple] = []

    def filter_by(self, **kwargs):
        for key, value in kwargs.items():
            self._preds.append(("eq", key, value))
        return self

    def filter(self, *criteria):
        for crit in criteria:
            if isinstance(crit, tuple) and crit and crit[0] in ("eq", "ne", "in"):
                self._preds.append(crit)
        return self

    def order_by(self, *args):
        return self

    def limit(self, count):
        return self

    def _match(self, row) -> bool:
        for op, name, value in self._preds:
            actual = getattr(row, name, None)
            if op == "eq" and actual != value:
                return False
            if op == "ne" and actual == value:
                return False
            if op == "in" and actual not in value:
                return False
        return True

    def all(self):
        return [row for row in self.session.rows(self.model) if self._match(row)]

    def first(self):
        rows = self.all()
        return rows[0] if rows else None

    def count(self):
        return len(self.all())

    def delete(self):
        rows = self.all()
        for row in rows:
            self.session.delete(row)
        return len(rows)


class FakeSession:
    """最小 Session：add/delete/commit/rollback/query。"""

    def __init__(self):
        self.tables: dict[type, list] = {}
        self.commits = 0
        self.rollbacks = 0

    def rows(self, model) -> list:
        return self.tables.setdefault(model, [])

    def query(self, model):
        return _FakeQuery(self, model)

    def add(self, obj):
        rows = self.rows(type(obj))
        if obj not in rows:
            rows.append(obj)

    def delete(self, obj):
        rows = self.rows(type(obj))
        if obj in rows:
            rows.remove(obj)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def flush(self):
        pass

    def close(self):
        pass

    def get(self, model, pk):
        for row in self.rows(model):
            if getattr(row, "id", None) == pk:
                return row
        return None


def make_settings(**overrides):
    """目录缓存 TTL 设 0：单测里每个用例自己 seed 目录，缓存只会串味。"""
    base = dict(catalog_cache_ttl_seconds=0)
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _User:
    """``deps.get_current_user`` 返回值的替身（路由函数只要 ``.id``）。"""

    def __init__(self, user_id: str):
        self.id = user_id
        self.role = "user"
        self.status = "approved"


def _attr(obj, *names):
    """兼容 stub BaseModel（原样 setattr）与真 pydantic（字段名/别名）两种取值。"""
    for name in names:
        if isinstance(obj, dict):
            if obj.get(name) is not None:
                return obj[name]
            continue
        value = getattr(obj, name, None)
        if value is not None and value is not Ellipsis:
            return value
    return None


def _http_code(exc: BaseException) -> str:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return str(detail.get("code", ""))
    return ""


_ROUTER_MODULE = None


def _import_fashion_router():
    """导入 ``routers/fashion.py``；stub 的 ``_Router`` 缺 ``.head`` 时临时补上。"""
    global _ROUTER_MODULE
    if _ROUTER_MODULE is not None:
        return _ROUTER_MODULE
    from _stub_env import _Router

    patched = not hasattr(_Router, "head")
    if patched:
        _Router.head = _Router.get
    try:
        from vmtools_next.api.routers import fashion as api_module
    finally:
        if patched:
            try:
                del _Router.head
            except AttributeError:  # pragma: no cover - 已被别处还原
                pass
    _ROUTER_MODULE = api_module
    return api_module


# ── 夹具 ────────────────────────────────────────────────────────────────

class _Base(unittest.TestCase):
    """三件目录时装 + 空衣柜。"""

    def setUp(self):
        svc.invalidate_caches()
        self.settings = make_settings()
        self.db = FakeSession()
        svc.upsert_catalog_item(self.db, {
            "cosmeticId": "cape_dragon",
            "packId": "pack1",
            "name": "龙翼披风",
            "category": "cape",
            "assetKey": "cape/dragon.png",
            "sortOrder": 1,
        })
        svc.upsert_catalog_item(self.db, {
            "cosmeticId": "wings_angel",
            "packId": "pack1",
            "name": "天使之翼",
            "category": "wings",
            "assetKey": "wings/angel.glb",
            "sortOrder": 2,
        })
        svc.upsert_catalog_item(self.db, {
            "cosmeticId": "hat_crown",
            "packId": "pack1",
            "name": "王冠",
            "category": "hat",
            "assetKey": "hat/crown.png",
            "sortOrder": 3,
        })

    # -- helpers --
    def add(self, cosmetic_id, user_id=USER_A):
        return svc.add_wardrobe_item(self.db, user_id, cosmetic_id)

    def list_items(self, user_id=USER_A):
        return svc.list_wardrobe(self.db, user_id, settings=self.settings)

    def rows(self, user_id=None):
        rows = list(self.db.rows(FashionWardrobeModel))
        if user_id is not None:
            rows = [row for row in rows if row.user_id == user_id]
        return rows

    def row(self, cosmetic_id, user_id=USER_A):
        for row in self.rows(user_id):
            if row.cosmetic_id == cosmetic_id:
                return row
        return None

    def set_added_at(self, cosmetic_id, value, user_id=USER_A):
        row = self.row(cosmetic_id, user_id)
        self.assertIsNotNone(row, f"{cosmetic_id} 不在衣柜里")
        row.added_at = value
        return row


# ── 加入衣柜 ────────────────────────────────────────────────────────────

class TestAddWardrobeItem(_Base):
    def test_add_persists_and_returns_item(self):
        item, added = self.add("cape_dragon")

        self.assertTrue(added)
        self.assertEqual(item["cosmeticId"], "cape_dragon")
        self.assertEqual(item["name"], "龙翼披风")
        self.assertEqual(item["category"], "cape")
        self.assertEqual(item["packId"], "pack1")
        self.assertEqual(item["assetKey"], "cape/dragon.png")
        self.assertEqual(item["sha256"], "")
        self.assertEqual(item["bytes"], 0)
        self.assertEqual(item["sortOrder"], 1)
        self.assertFalse(item["missing"])
        self.assertIsInstance(item["addedAt"], str)
        self.assertTrue(item["addedAt"].endswith("+00:00"))
        # 对外字段与契约完全一致（不多不少）
        self.assertEqual(set(item), _WARDROBE_ITEM_KEYS)

        self.assertEqual(len(self.rows(USER_A)), 1)
        self.assertEqual(self.rows(USER_A)[0].cosmetic_id, "cape_dragon")

    def test_add_is_idempotent(self):
        first, first_added = self.add("cape_dragon")
        second, second_added = self.add("cape_dragon")

        self.assertTrue(first_added)
        self.assertFalse(second_added, "重复加入应返回 added=False（幂等，不报错）")
        self.assertEqual(second["cosmeticId"], first["cosmeticId"])
        self.assertEqual(second["addedAt"], first["addedAt"], "幂等时不能刷新加入时间")
        self.assertEqual(len(self.rows(USER_A)), 1, "唯一约束下不能出现重复行")

    def test_add_unknown_cosmetic_404(self):
        with self.assertRaises(FashionServiceError) as ctx:
            self.add("no_such_cosmetic")
        self.assertEqual(ctx.exception.code, "cosmetic_not_found")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self.rows(USER_A), [])

    def test_add_disabled_cosmetic_404(self):
        svc.upsert_catalog_item(self.db, {
            "cosmeticId": "cape_dragon", "enabled": False,
        })
        with self.assertRaises(FashionServiceError) as ctx:
            self.add("cape_dragon")
        self.assertEqual(ctx.exception.code, "cosmetic_not_found")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self.rows(USER_A), [])

    def test_add_empty_cosmetic_id_400(self):
        for bad in ("", "   ", None):
            with self.assertRaises(FashionServiceError) as ctx:
                self.add(bad)
            self.assertEqual(ctx.exception.code, "invalid_cosmetic_id")
            self.assertEqual(ctx.exception.status_code, 400)

    def test_add_overlong_cosmetic_id_400(self):
        with self.assertRaises(FashionServiceError) as ctx:
            self.add("a" * 129)
        self.assertEqual(ctx.exception.code, "invalid_cosmetic_id")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_add_accepts_global_id_alias_of_legacy_catalog(self):
        """目录是历史简单 ID（packId=pack1）时，传全局 ID 也能加入且原样保留。

        商店页拿到的是目录里的原值；保留玩家传入的形态，删除时用同一个值即可命中。
        """
        item, added = self.add("pack1:cape_dragon")
        self.assertTrue(added)
        self.assertEqual(item["cosmeticId"], "pack1:cape_dragon")
        self.assertEqual(item["name"], "龙翼披风")
        self.assertFalse(item["missing"])
        # 列表里同一形态也能解析到目录元数据（_catalog_map 的别名兜底）
        listed = self.list_items()
        self.assertEqual([it["cosmeticId"] for it in listed], ["pack1:cape_dragon"])
        self.assertFalse(listed[0]["missing"])

    def test_add_normalizes_legacy_simple_id_to_catalog_global_id(self):
        """目录是**全局 ID** 而调用方传的是简单 ID（别名）时，落库必须是全局 ID。

        为什么：mod 的 ``WardrobeCollection`` 拿 ``cosmeticId`` 跟本地已装包的
        ``CosmeticDefinition#id()``（永远是 ``packId:localId``）比对。若衣柜里存
        简单 ID，同一 ``localId`` 分布在多个包时短 ID 别名有歧义 → 玩家在游戏里
        明明收藏了却匹配不上（卡片消失 / 重复）。所以落库统一走
        ``canonical_loadout_cosmetic_id``，与 ``put_loadout`` 同一套规则。
        """
        svc.upsert_catalog_item(self.db, {
            "cosmeticId": "pack2:angel_wing",
            "packId": "pack2",
            "name": "天使翼（全局 ID）",
            "category": "wings",
            "assetKey": "pack2/angel.glb",
        })

        item, added = self.add("angel_wing")  # 传简单 ID（历史别名）
        self.assertTrue(added)
        self.assertEqual(item["cosmeticId"], "pack2:angel_wing", "必须落库全局 ID")
        self.assertEqual(item["name"], "天使翼（全局 ID）")
        self.assertEqual([row.cosmetic_id for row in self.rows(USER_A)], ["pack2:angel_wing"])

        # 再按全局 ID 加一次：仍是幂等（不能因为形态不同插出第二条）
        again, added_again = self.add("pack2:angel_wing")
        self.assertFalse(added_again)
        self.assertEqual(again["cosmeticId"], "pack2:angel_wing")
        self.assertEqual(len(self.rows(USER_A)), 1)

    def test_add_legacy_row_then_global_id_is_still_idempotent(self):
        """旧版本已按简单 ID 存过的行：再按全局 ID 加一次也不能插出第二条。

        且这一次会把行**就地升级**成全局 ID（``canonical_loadout_cosmetic_id`` 规则 2：
        目录是简单 ID + 上报前缀与 packId 一致 → 保留上报的全局 ID），这样
        ``/wardrobe/me`` 逐步收敛到 mod 能匹配的形态，又不会多出一条记录。
        """
        self.add("cape_dragon")  # 目录是简单 ID + packId=pack1
        self.assertEqual([row.cosmetic_id for row in self.rows(USER_A)], ["cape_dragon"])

        item, added = self.add("pack1:cape_dragon")
        self.assertFalse(added, "同一件时装（不同 ID 形态）不该重复插入")
        self.assertEqual(item["cosmeticId"], "pack1:cape_dragon", "就地升级为全局 ID")
        self.assertEqual([row.cosmetic_id for row in self.rows(USER_A)], ["pack1:cape_dragon"])
        self.assertEqual(len(self.rows(USER_A)), 1)


    def test_add_upgrade_collision_does_not_500(self):
        """库里同时存在简单 ID 与规范 ID 两条同一件时，就地升级撞唯一约束不能 500。

        极端场景（历史并发写入，或管理端把目录 ID 从简单形态迁移成全局 ID 后玩家又
        加了一次）：升级 ``cosmetic_id`` 会撞 ``(user_id, cosmetic_id)`` 唯一约束。
        加入衣柜是幂等接口 —— 应回滚并返回已存在的那条，而不是把 IntegrityError
        抛成 500（前端只会看到"服务器错误"，且永远加不进去）。
        """
        from sqlalchemy.exc import IntegrityError

        now = datetime.now(timezone.utc)
        rows = self.db.rows(FashionWardrobeModel)
        rows.append(
            FashionWardrobeModel(
                id="w-legacy", user_id=USER_A, cosmetic_id="cape_dragon", added_at=now
            )
        )
        rows.append(
            FashionWardrobeModel(
                id="w-global",
                user_id=USER_A,
                cosmetic_id="pack1:cape_dragon",
                added_at=now,
            )
        )

        original_commit = self.db.commit
        state = {"raised": False}

        def commit():
            if not state["raised"]:
                state["raised"] = True
                raise IntegrityError(
                    "UPDATE fashion_wardrobe", {}, Exception("UNIQUE constraint failed")
                )
            return original_commit()

        self.db.commit = commit
        item, added = self.add("pack1:cape_dragon")

        self.assertFalse(added, "撞唯一约束时应按「已存在」处理（幂等）")
        self.assertEqual(item["cosmeticId"], "pack1:cape_dragon")
        self.assertEqual(len(rows), 2, "不能因为回滚把已有行删掉或新增第三条")
        self.assertEqual(self.db.rollbacks, 1, "必须回滚失败的升级事务")


# ── 衣柜列表 ────────────────────────────────────────────────────────────

class TestListWardrobe(_Base):
    def test_empty_wardrobe_returns_empty_list(self):
        self.assertEqual(self.list_items(), [])

    def test_list_fields_and_order(self):
        base = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
        self.add("cape_dragon")
        self.set_added_at("cape_dragon", base)
        self.add("wings_angel")
        # 故意混一个 naive datetime：SQLite 读回就是 naive，排序不能 TypeError
        self.set_added_at("wings_angel", base + timedelta(minutes=1))
        self.add("hat_crown")
        self.set_added_at("hat_crown", base + timedelta(minutes=2))

        items = self.list_items()
        self.assertEqual(
            [it["cosmeticId"] for it in items],
            ["hat_crown", "wings_angel", "cape_dragon"],
            "必须按 addedAt 倒序（最近加入的排最前）",
        )
        first = items[0]
        self.assertEqual(set(first), _WARDROBE_ITEM_KEYS)
        self.assertEqual(first["name"], "王冠")
        self.assertEqual(first["category"], "hat")
        self.assertEqual(first["packId"], "pack1")
        self.assertEqual(first["assetKey"], "hat/crown.png")
        self.assertEqual(first["sortOrder"], 3)
        self.assertFalse(first["missing"])
        self.assertEqual(
            first["addedAt"], "2026-11-01T00:02:00+00:00",
            "naive datetime 要按 UTC 渲染（svc.iso 的约定）",
        )

    def test_catalog_deleted_item_is_missing_but_still_listed(self):
        self.add("cape_dragon")
        self.assertTrue(svc.delete_catalog_item(self.db, "cape_dragon"))

        items = self.list_items()
        self.assertEqual(len(items), 1, "目录被删也要返回条目，玩家才删得掉")
        item = items[0]
        self.assertEqual(item["cosmeticId"], "cape_dragon")
        self.assertTrue(item["missing"])
        self.assertEqual(item["name"], "", "目录整条没了，没有残留值可用 → 空串")
        self.assertEqual(item["category"], "other")
        self.assertEqual(item["assetKey"], "")
        self.assertEqual(item["sortOrder"], 0)

    def test_catalog_disabled_item_is_missing_with_residual_name(self):
        self.add("cape_dragon")
        svc.upsert_catalog_item(self.db, {
            "cosmeticId": "cape_dragon", "enabled": False,
        })

        items = self.list_items()
        self.assertEqual(len(items), 1, "已停用的条目同样要返回")
        self.assertTrue(items[0]["missing"])
        self.assertEqual(items[0]["name"], "龙翼披风", "name 用目录里的残留值")

    def test_list_is_isolated_per_user(self):
        self.add("cape_dragon", USER_A)
        self.add("wings_angel", USER_B)

        self.assertEqual([it["cosmeticId"] for it in self.list_items(USER_A)],
                         ["cape_dragon"])
        self.assertEqual([it["cosmeticId"] for it in self.list_items(USER_B)],
                         ["wings_angel"])
        self.assertEqual(self.list_items("user-never-used"), [])


# ── 删除 ────────────────────────────────────────────────────────────────

class TestRemoveWardrobeItem(_Base):
    def test_remove_single(self):
        self.add("cape_dragon")
        self.assertTrue(svc.remove_wardrobe_item(self.db, USER_A, "cape_dragon"))
        self.assertEqual(self.rows(USER_A), [])
        self.assertEqual(self.list_items(), [])

    def test_remove_missing_returns_false(self):
        self.add("cape_dragon")
        self.assertFalse(svc.remove_wardrobe_item(self.db, USER_A, "wings_angel"))
        self.assertFalse(svc.remove_wardrobe_item(self.db, USER_A, ""))
        self.assertEqual(len(self.rows(USER_A)), 1, "删不存在的不能误删别的")

    def test_remove_cannot_touch_other_user(self):
        self.add("cape_dragon", USER_A)
        self.add("cape_dragon", USER_B)

        self.assertTrue(svc.remove_wardrobe_item(self.db, USER_B, "cape_dragon"))
        self.assertEqual(len(self.rows(USER_B)), 0)
        self.assertEqual(len(self.rows(USER_A)), 1, "A 的衣柜不能被 B 删掉")


class TestBatchRemoveWardrobeItems(_Base):
    def test_batch_remove_counts(self):
        self.add("cape_dragon")
        self.add("wings_angel")
        self.add("hat_crown")

        removed = svc.remove_wardrobe_items(
            self.db, USER_A, ["cape_dragon", "wings_angel"]
        )
        self.assertEqual(removed, 2)
        self.assertEqual([it["cosmeticId"] for it in self.list_items()], ["hat_crown"])

    def test_batch_remove_dedupes_and_ignores_unknown(self):
        self.add("cape_dragon")
        self.add("wings_angel")

        removed = svc.remove_wardrobe_items(
            self.db, USER_A,
            ["cape_dragon", "cape_dragon", "  ", "no_such_cosmetic", "cape_dragon"],
        )
        self.assertEqual(removed, 1, "重复 id 只算一次；不存在的 id 静默忽略")
        self.assertEqual([it["cosmeticId"] for it in self.list_items()], ["wings_angel"])

    def test_batch_remove_empty_selection_400(self):
        for bad in ([], ["", "   "], None):
            with self.assertRaises(FashionServiceError) as ctx:
                svc.remove_wardrobe_items(self.db, USER_A, bad)
            self.assertEqual(ctx.exception.code, "empty_selection")
            self.assertEqual(ctx.exception.status_code, 400)

    def test_batch_remove_too_many_400(self):
        ids = [f"cosmetic_{i}" for i in range(svc.MAX_WARDROBE_BATCH_DELETE + 1)]
        with self.assertRaises(FashionServiceError) as ctx:
            svc.remove_wardrobe_items(self.db, USER_A, ids)
        self.assertEqual(ctx.exception.code, "too_many_items")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_batch_remove_limit_is_checked_after_dedupe(self):
        """上限按「去重后」算：600 个相同 id 去重成 1 个，不该被判 too_many_items。"""
        ids = ["cape_dragon"] * 600
        self.add("cape_dragon")
        self.assertEqual(svc.remove_wardrobe_items(self.db, USER_A, ids), 1)

    def test_batch_remove_is_isolated_per_user(self):
        for cosmetic_id in ("cape_dragon", "wings_angel", "hat_crown"):
            self.add(cosmetic_id, USER_A)
            self.add(cosmetic_id, USER_B)

        removed = svc.remove_wardrobe_items(
            self.db, USER_B, ["cape_dragon", "wings_angel", "hat_crown"]
        )
        self.assertEqual(removed, 3)
        self.assertEqual(len(self.rows(USER_B)), 0)
        self.assertEqual(len(self.rows(USER_A)), 3, "批量删除不能跨用户")


class TestNormalizeWardrobeIds(unittest.TestCase):
    def test_strips_dedupes_and_drops_impossible_ids(self):
        self.assertEqual(
            svc.normalize_wardrobe_ids(
                [" a ", "a", "", "  ", None, "b", "x" * 129, "c"]
            ),
            ["a", "b", "c"],
        )


# ── 路由层接线（导入期间临时补 _Router.head） ───────────────────────────

class TestWardrobeApiWiring(_Base):
    @classmethod
    def setUpClass(cls):
        try:
            cls.api = _import_fashion_router()
        except Exception as exc:  # pragma: no cover - 取决于离线 stub 的完整度
            raise unittest.SkipTest(f"无法导入路由模块：{exc}")

    def test_four_endpoints_registered(self):
        # 真 fastapi.APIRouter 的 route.path 带 router prefix（/api/fashion），
        # _stub_env 的假 _Router 不带；断言前统一剥掉前缀，两种环境都能跑。
        prefix = "/api/fashion"

        def _strip(path: str) -> str:
            path = str(path or "")
            return path[len(prefix):] if path.startswith(prefix) else path

        table = set()
        for route in getattr(self.api.router, "routes", []):
            if isinstance(route, dict):  # _stub_env 的假 APIRouter
                table.add((route["method"].upper(), _strip(route["path"])))
            else:  # 真 fastapi.APIRoute
                for method in getattr(route, "methods", set()) or set():
                    table.add((method.upper(), _strip(route.path)))

        self.assertIn(("GET", "/wardrobe"), table)
        self.assertIn(("POST", "/wardrobe"), table)
        self.assertIn(("POST", "/wardrobe/batch-delete"), table)
        self.assertIn(("DELETE", "/wardrobe/{cosmetic_id}"), table)
        # mod 端拉收藏（fashion token 或网页 JWT 都行）
        self.assertIn(("GET", "/wardrobe/me"), table)

    def test_route_list_returns_items(self):
        self.add("cape_dragon")
        response = self.api.list_wardrobe(user=_User(USER_A), db=self.db)
        items = _attr(response, "items")
        self.assertEqual(len(items), 1)
        self.assertEqual(_attr(items[0], "cosmetic_id", "cosmeticId"), "cape_dragon")

    def test_route_add_returns_added_flag(self):
        first = self.api.add_wardrobe_item(
            body=self.api.WardrobeAddRequest(cosmetic_id="cape_dragon"),
            user=_User(USER_A),
            db=self.db,
        )
        second = self.api.add_wardrobe_item(
            body=self.api.WardrobeAddRequest(cosmetic_id="cape_dragon"),
            user=_User(USER_A),
            db=self.db,
        )
        self.assertTrue(_attr(first, "added"))
        self.assertFalse(_attr(second, "added"), "重复加入仍是 200，只是 added=False")
        self.assertEqual(
            _attr(_attr(second, "item"), "cosmetic_id", "cosmeticId"), "cape_dragon"
        )

    def test_route_add_translates_404(self):
        try:
            self.api.add_wardrobe_item(
                body=self.api.WardrobeAddRequest(cosmetic_id="no_such_cosmetic"),
                user=_User(USER_A),
                db=self.db,
            )
        except Exception as exc:  # stub 的 HTTPException 不是真 fastapi 的类
            self.assertEqual(getattr(exc, "status_code", None), 404)
            self.assertEqual(_http_code(exc), "cosmetic_not_found")
        else:
            self.fail("未知时装应当抛 404 cosmetic_not_found")

    def test_route_batch_delete_empty_selection_400(self):
        try:
            self.api.batch_delete_wardrobe_items(
                body=self.api.WardrobeBatchDeleteRequest(cosmetic_ids=[]),
                user=_User(USER_A),
                db=self.db,
            )
        except Exception as exc:
            self.assertEqual(getattr(exc, "status_code", None), 400)
            self.assertEqual(_http_code(exc), "empty_selection")
        else:
            self.fail("空数组应当抛 400 empty_selection")

    def test_route_delete_reports_removed(self):
        self.add("cape_dragon")
        hit = self.api.delete_wardrobe_item(
            cosmetic_id="cape_dragon", user=_User(USER_A), db=self.db
        )
        miss = self.api.delete_wardrobe_item(
            cosmetic_id="cape_dragon", user=_User(USER_A), db=self.db
        )
        self.assertTrue(_attr(hit, "removed"))
        self.assertFalse(_attr(miss, "removed"), "删不存在的仍是 200，removed=False")


# ── mod 端：GET /wardrobe/me ────────────────────────────────────────────

class _Principal:
    """``routers.get_principal`` 返回值的替身（路由只要 ``.user_id``）。

    真实 principal 是 dataclass：identity_id / user_id / via / mc_uuid / mc_name。
    """

    def __init__(self, user_id=None, identity_id=None, via="mod"):
        self.user_id = user_id
        self.identity_id = identity_id
        self.via = via
        self.mc_uuid = "12345678-1234-5678-1234-567812345678"
        self.mc_name = "Tester"


class TestWardrobeMeForClient(_Base):
    """mod 拉收藏：``linked`` 语义 + 精简字段（契约 docs/fashion-api.md §5.5）。"""

    @classmethod
    def setUpClass(cls):
        try:
            cls.api = _import_fashion_router()
        except Exception as exc:  # pragma: no cover - 取决于离线 stub 的完整度
            raise unittest.SkipTest(f"无法导入路由模块：{exc}")

    def me(self, principal):
        return self.api.my_wardrobe_for_client(principal=principal, db=self.db)

    def test_unlinked_identity_returns_linked_false(self):
        self.add("cape_dragon")
        response = self.me(_Principal(user_id=None, identity_id="identity-1"))
        self.assertFalse(_attr(response, "linked"), "没绑定网站账号 → linked=False")
        self.assertEqual(_attr(response, "items"), [],
                         "linked=False 时不能把「衣柜是空的」当成真话")

    def test_linked_returns_compact_items(self):
        self.add("cape_dragon")
        self.add("wings_angel")
        response = self.me(_Principal(user_id=USER_A, identity_id="identity-1"))

        self.assertTrue(_attr(response, "linked"))
        items = _attr(response, "items")
        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(_attr(first, "cosmetic_id", "cosmeticId"), "wings_angel")
        self.assertEqual(_attr(first, "name"), "天使之翼")
        self.assertEqual(_attr(first, "category"), "wings")
        self.assertEqual(_attr(first, "pack_id", "packId"), "pack1")
        self.assertFalse(_attr(first, "missing"))

    def test_compact_items_do_not_leak_asset_metadata(self):
        self.add("cape_dragon")
        item = _attr(self.me(_Principal(user_id=USER_A)), "items")[0]
        # mod 不需要资产元数据（走 /loadouts/resolve 与包级下发），别塞进来
        self.assertIsNone(_attr(item, "asset_key", "assetKey"))
        self.assertIsNone(_attr(item, "sha256"))
        self.assertIsNone(_attr(item, "bytes"))
        self.assertIsNone(_attr(item, "sort_order", "sortOrder"))
        self.assertIsNone(_attr(item, "added_at", "addedAt"))

    def test_linked_but_empty_wardrobe_stays_linked(self):
        response = self.me(_Principal(user_id=USER_A))
        self.assertTrue(_attr(response, "linked"),
                        "已绑定但衣柜为空 → linked=True + 空列表（界面显示「衣柜是空的」引导）")
        self.assertEqual(_attr(response, "items"), [])

    def test_missing_flag_propagates(self):
        self.add("cape_dragon")
        svc.upsert_catalog_item(self.db, {"cosmeticId": "cape_dragon", "enabled": False})
        item = _attr(self.me(_Principal(user_id=USER_A)), "items")[0]
        self.assertTrue(_attr(item, "missing"), "已下架的条目要标 missing，mod 会禁止穿戴")

    def test_isolated_per_user(self):
        self.add("cape_dragon", USER_A)
        self.add("wings_angel", USER_B)
        ids_a = [
            _attr(it, "cosmetic_id", "cosmeticId")
            for it in _attr(self.me(_Principal(user_id=USER_A)), "items")
        ]
        ids_b = [
            _attr(it, "cosmetic_id", "cosmeticId")
            for it in _attr(self.me(_Principal(user_id=USER_B)), "items")
        ]
        self.assertEqual(ids_a, ["cape_dragon"])
        self.assertEqual(ids_b, ["wings_angel"])

    def test_web_principal_also_works(self):
        """网页 JWT 走的也是 get_principal（via="web"），同样要能拉到自己的收藏。"""
        self.add("hat_crown")
        response = self.me(_Principal(user_id=USER_A, via="web"))
        self.assertTrue(_attr(response, "linked"))
        self.assertEqual(
            _attr(_attr(response, "items")[0], "cosmetic_id", "cosmeticId"), "hat_crown"
        )


# ── alembic 迁移 ────────────────────────────────────────────────────────

class _FakeOp:
    def __init__(self):
        self.created: list[str] = []
        self.columns: list = []
        self.indexes: list[tuple[str, str]] = []
        self.dropped: list[str] = []

    def get_bind(self):
        return object()

    def create_table(self, name, *columns, **kwargs):
        self.created.append(name)
        self.columns.extend(columns)

    def create_index(self, name, table, columns, **kwargs):
        self.indexes.append((name, table))

    def drop_table(self, name):
        self.dropped.append(name)


class TestWardrobeMigration(unittest.TestCase):
    MIGRATION = os.path.join(
        _HERE, os.pardir, "alembic", "versions", "20261101_fashion_wardrobe.py"
    )

    def _load(self):
        op = _FakeOp()
        alembic_stub = types.ModuleType("alembic")
        alembic_stub.op = op
        previous = sys.modules.get("alembic")
        sys.modules["alembic"] = alembic_stub
        try:
            spec = importlib.util.spec_from_file_location(
                "fashion_wardrobe_migration_under_test", os.path.abspath(self.MIGRATION)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            if previous is None:
                sys.modules.pop("alembic", None)
            else:
                sys.modules["alembic"] = previous
        return module, op

    def test_upgrade_creates_table_and_indexes(self):
        module, op = self._load()
        module._existing_tables = lambda: set()
        module.upgrade()

        self.assertEqual(op.created, ["fashion_wardrobe"])
        self.assertEqual(
            {table for _name, table in op.indexes}, {"fashion_wardrobe"}
        )
        self.assertEqual(len(op.indexes), 2, "user_id / cosmetic_id 各一个索引")

    def test_upgrade_is_idempotent(self):
        module, op = self._load()
        module._existing_tables = lambda: {"fashion_wardrobe"}
        module.upgrade()
        self.assertEqual(op.created, [], "表已存在时不应重复建表（create_all 已建过）")

    def test_revision_graph(self):
        module, _op = self._load()
        self.assertEqual(module.revision, "20261101_fashion_wardrobe")
        self.assertEqual(module.down_revision, "20261028_fashion")

    def test_downgrade_drops_table(self):
        module, op = self._load()
        module._existing_tables = lambda: {"fashion_wardrobe"}
        module.downgrade()
        self.assertEqual(op.dropped, ["fashion_wardrobe"])

    def test_downgrade_is_idempotent(self):
        module, op = self._load()
        module._existing_tables = lambda: set()
        module.downgrade()
        self.assertEqual(op.dropped, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
