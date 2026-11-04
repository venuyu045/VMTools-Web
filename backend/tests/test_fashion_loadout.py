"""时装业务层单测 —— 穿戴配置读写、批量 resolve、资产落盘、alembic 迁移声明。

离线可跑：SQLAlchemy 用 ``_stub_env`` 的 stub，数据库用本文件的 ``FakeSession``
（内存表 + 最小查询语义），资产落盘用 ``tempfile``，**不碰真实 SQLite / 网络**。

跑法（仓库根目录）::

    python3 -m unittest discover -s backend/tests -p 'test_fashion_*.py' -v
    cd backend && python3 -m unittest tests.test_fashion_loadout -v
"""
from __future__ import annotations

import importlib.util
import io
import os
import stat
import sys
import tempfile
import types
import unittest
import uuid as uuid_lib
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _stub_env import install, quiet_stub_noise  # noqa: E402

install()
quiet_stub_noise()

from vmtools_next.core import fashion_service as svc  # noqa: E402
from vmtools_next.data.models.fashion import (  # noqa: E402
    FashionAssetModel,
    FashionCatalogModel,
    FashionIdentityModel,
    FashionLoadoutModel,
)

UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"
UUID_C = "33333333-3333-3333-3333-333333333333"


# ── 假 SQLAlchemy Session（内存表 + filter/filter_by/in_ 语义） ─────────

class _Expr:
    """列表达式替身：stub 环境里 ``Column.__eq__`` 返回自身、值会丢，所以单测里
    把参与 ``filter(...in_(...))`` 的列换成能捕获比较意图的对象。"""

    def __init__(self, name: str):
        self.name = name

    def __eq__(self, other):  # noqa: D105
        return ("eq", self.name, other)

    def __ne__(self, other):  # noqa: D105
        return ("ne", self.name, other)

    def in_(self, values):
        return ("in", self.name, list(values))

    def asc(self):
        return ("asc", self.name)

    def desc(self):
        return ("desc", self.name)

    __hash__ = object.__hash__


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
    """最小 Session：add/delete/commit/query/get。"""

    def __init__(self):
        self.tables: dict[type, list] = {}
        self.commits = 0
        self.query_count = 0
        self.deleted: list = []

    def rows(self, model) -> list:
        return self.tables.setdefault(model, [])

    def query(self, model):
        self.query_count += 1
        return _FakeQuery(self, model)

    def add(self, obj):
        rows = self.rows(type(obj))
        if obj not in rows:
            rows.append(obj)

    def add_all(self, objs):
        for obj in objs:
            self.add(obj)

    def delete(self, obj):
        rows = self.rows(type(obj))
        if obj in rows:
            rows.remove(obj)
        self.deleted.append(obj)

    def commit(self):
        self.commits += 1

    def flush(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def get(self, model, pk):
        for row in self.rows(model):
            if getattr(row, "id", None) == pk or getattr(row, "cosmetic_id", None) == pk:
                return row
        return None

    def execute(self, *args, **kwargs):
        return None


# 让 `filter(Model.col.in_(...))` 在 stub 环境里可被 FakeQuery 解释
FashionIdentityModel.mc_uuid = _Expr("mc_uuid")
FashionLoadoutModel.identity_id = _Expr("identity_id")


def make_settings(**overrides):
    base = dict(
        enabled=True,
        yggdrasil_bases=[],
        mojang_session_server="https://sessionserver.mojang.com",
        challenge_ttl_seconds=120,
        token_ttl_seconds=3600,
        http_timeout_seconds=5.0,
        device_code_ttl_seconds=600,
        device_poll_interval_seconds=3,
        asset_dir="data/fashion-assets",
        catalog_cache_ttl_seconds=30,
        resolve_max_uuids=200,
        resolve_cache_ttl_seconds=5,
        max_loadout_items=8,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


# ── 夹具 ────────────────────────────────────────────────────────────────

class _Base(unittest.TestCase):
    def setUp(self):
        svc.invalidate_caches()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(asset_dir=self.tmp.name)
        self.db = FakeSession()
        svc.upsert_catalog_item(
            self.db,
            {
                "cosmeticId": "cape_dragon",
                "packId": "pack1",
                "name": "龙翼披风",
                "category": "cape",
                "assetKey": "cape/dragon.png",
                "sortOrder": 1,
            },
        )
        svc.upsert_catalog_item(
            self.db,
            {
                "cosmeticId": "wings_angel",
                "packId": "pack1",
                "name": "天使之翼",
                "category": "wings",
                "assetKey": "wings/angel.glb",
                "sortOrder": 2,
            },
        )
        self.identity = svc.upsert_identity(
            self.db,
            mc_uuid=UUID_A,
            mc_name="Steve",
            auth_type="mojang",
        )

    def identity_for(self, mc_uuid: str, name: str = "Alex"):
        return svc.upsert_identity(
            self.db, mc_uuid=mc_uuid, mc_name=name, auth_type="mojang"
        )


# ── 穿戴配置 ────────────────────────────────────────────────────────────

class TestLoadoutCRUD(_Base):
    def test_put_then_get(self):
        updated_at = svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "cape", "cosmeticId": "cape_dragon", "scale": 1.5}],
            settings=self.settings,
        )
        self.assertIsNotNone(updated_at)

        items = svc.get_loadout(self.db, self.identity.id)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["slot"], "cape")
        self.assertEqual(item["cosmeticId"], "cape_dragon")
        self.assertEqual(item["assetKey"], "cape/dragon.png")
        self.assertEqual(item["scale"], 1.5)
        self.assertFalse(item["missing"])

    def test_put_accepts_mod_global_id_and_stores_global_id(self):
        """mod 上报 pack1:cape_dragon、目录里是简单 ID → **落库仍是全局 ID**。

        回归（审计 A2，P0）：旧实现把上报的全局 ID 改写成目录里的简单 ID 落库，
        客户端 ``AssetSyncService.requestMissing`` 因为「没有冒号前缀」直接 return
        → 这件时装永远不会被整包下发，其他玩家也看不到。
        """
        svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "cape", "cosmeticId": "pack1:cape_dragon", "scale": 1.0}],
            settings=self.settings,
        )
        items = svc.get_loadout(self.db, self.identity.id)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["cosmeticId"], "pack1:cape_dragon")
        # 目录元数据仍要解析出来（别名表兜底），否则网页衣柜/渲染会缺 assetKey
        self.assertEqual(items[0]["assetKey"], "cape/dragon.png")
        self.assertEqual(items[0]["packId"], "pack1")
        self.assertFalse(items[0]["missing"])

    def test_put_keeps_global_id_in_resolve(self):
        """落库的全局 ID 要能被热路径 resolve 解析（其他玩家看得到）。"""
        svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "cape", "cosmeticId": "pack1:cape_dragon", "scale": 1.0}],
            settings=self.settings,
        )
        payload = svc.resolve_loadouts(
            self.db, [UUID_A], settings=self.settings
        )
        items = payload["loadouts"][UUID_A]["items"]
        self.assertEqual(items[0]["cosmeticId"], "pack1:cape_dragon")
        self.assertEqual(items[0]["assetKey"], "cape/dragon.png")

    def test_put_simple_id_upgrades_to_catalog_global_id(self):
        """目录是全局 ID、旧客户端上报简单 ID → 落库升级成目录的全局 ID。"""
        svc.upsert_catalog_item(
            self.db, {"cosmeticId": "pack2:wings_blue", "category": "wings"}
        )
        svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "wings", "cosmeticId": "wings_blue", "scale": 1.0}],
            settings=self.settings,
        )
        items = svc.get_loadout(self.db, self.identity.id)
        self.assertEqual(items[0]["cosmeticId"], "pack2:wings_blue")
        self.assertFalse(items[0]["missing"])

    def test_put_simple_id_stays_simple_for_legacy_catalog(self):
        """两边都是简单 ID（历史数据）→ 保持简单 ID，不擅自改写（向后兼容）。"""
        svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "cape", "cosmeticId": "cape_dragon", "scale": 1.0}],
            settings=self.settings,
        )
        items = svc.get_loadout(self.db, self.identity.id)
        self.assertEqual(items[0]["cosmeticId"], "cape_dragon")

    def test_put_wrong_pack_prefix_falls_back_to_catalog_id(self):
        """审计 v1.2 P2-1：上报前缀与目录 packId 不一致 → 回目录 ID，不能静默丢件。"""
        _updated, warnings = svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "cape", "cosmeticId": "pack2:cape_dragon", "scale": 1.0}],
            settings=self.settings,
        )
        self.assertEqual(warnings, [])
        items = svc.get_loadout(self.db, self.identity.id)
        self.assertEqual(items[0]["cosmeticId"], "cape_dragon")
        self.assertFalse(items[0]["missing"])
        self.assertEqual(items[0]["assetKey"], "cape/dragon.png")

    def test_put_global_id_with_empty_catalog_pack_id_falls_back(self):
        """目录是 legacy 简单 ID 且 packId 为空 → 回目录 ID，保证 loadout/me 能解析。"""
        svc.upsert_catalog_item(
            self.db,
            {"cosmeticId": "crown", "category": "hat", "assetKey": "hat/crown.png"},
        )
        svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "hat", "cosmeticId": "example:crown", "scale": 1.0}],
            settings=self.settings,
        )
        items = svc.get_loadout(self.db, self.identity.id)
        self.assertEqual(items[0]["cosmeticId"], "crown")
        self.assertFalse(items[0]["missing"])
        self.assertEqual(items[0]["assetKey"], "hat/crown.png")

    def test_put_disabled_global_id_warns(self):
        svc.upsert_catalog_item(
            self.db, {"cosmeticId": "pack3:hat_off", "category": "hat", "enabled": False}
        )
        _updated, warnings = svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "hat", "cosmeticId": "pack3:hat_off"}],
            settings=self.settings,
        )
        self.assertEqual(warnings[0]["code"], "cosmetic_disabled")
        self.assertEqual(warnings[0]["cosmeticId"], "pack3:hat_off")

    def test_put_without_scale_preserves_saved_scale(self):
        """mod 不带 scale 上报时，不能把网页衣柜保存的缩放重置为 1.0。"""
        svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "cape", "cosmeticId": "cape_dragon", "scale": 2.5}],
            settings=self.settings,
        )
        svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "cape", "cosmeticId": "cape_dragon"}],
            settings=self.settings,
        )
        items = svc.get_loadout(self.db, self.identity.id)
        self.assertEqual(items[0]["scale"], 2.5)

    def test_put_is_full_replace(self):
        svc.put_loadout(
            self.db,
            self.identity.id,
            [
                {"slot": "cape", "cosmeticId": "cape_dragon", "scale": 1.0},
                {"slot": "wings", "cosmeticId": "wings_angel", "scale": 1.0},
            ],
            settings=self.settings,
        )
        self.assertEqual(len(svc.get_loadout(self.db, self.identity.id)), 2)

        svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "cape", "cosmeticId": "wings_angel", "scale": 2.0}],
            settings=self.settings,
        )
        items = svc.get_loadout(self.db, self.identity.id)
        self.assertEqual([it["slot"] for it in items], ["cape"])
        self.assertEqual(items[0]["cosmeticId"], "wings_angel")
        # 被卸下的 wings 槽位应删除，而不是残留
        self.assertEqual(len(svc.get_loadout_rows(self.db, self.identity.id)), 1)

    def test_invalid_slot_rejected(self):
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.put_loadout(
                self.db,
                self.identity.id,
                [{"slot": "hat2", "cosmeticId": "cape_dragon"}],
                settings=self.settings,
            )
        self.assertEqual(ctx.exception.code, "invalid_slot")

    def test_duplicate_slot_rejected(self):
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.put_loadout(
                self.db,
                self.identity.id,
                [
                    {"slot": "cape", "cosmeticId": "cape_dragon"},
                    {"slot": "cape", "cosmeticId": "wings_angel"},
                ],
                settings=self.settings,
            )
        self.assertEqual(ctx.exception.code, "duplicate_slot")

    def test_unknown_cosmetic_is_skipped_with_warning(self):
        """未知时装只跳过该条：否则 mod 自带的本地包会让整条 PUT 404。"""
        _, warnings = svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "cape", "cosmeticId": "example:crown"}],
            settings=self.settings,
        )
        self.assertEqual(
            warnings,
            [{"slot": "cape", "cosmeticId": "example:crown", "code": "unknown_cosmetic"}],
        )
        self.assertEqual(svc.get_loadout(self.db, self.identity.id, settings=self.settings), [])

    def test_unknown_cosmetic_does_not_block_other_items(self):
        _, warnings = svc.put_loadout(
            self.db,
            self.identity.id,
            [
                {"slot": "cape", "cosmeticId": "example:crown"},
                {"slot": "wings", "cosmeticId": "wings_angel"},
            ],
            settings=self.settings,
        )
        self.assertEqual([item["code"] for item in warnings], ["unknown_cosmetic"])
        slots = [item["slot"] for item in svc.get_loadout(self.db, self.identity.id, settings=self.settings)]
        self.assertEqual(slots, ["wings"])

    def test_disabled_cosmetic_is_skipped_with_warning(self):
        svc.upsert_catalog_item(
            self.db, {"cosmeticId": "cape_dragon", "enabled": False}
        )
        _, warnings = svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "cape", "cosmeticId": "cape_dragon"}],
            settings=self.settings,
        )
        self.assertEqual(warnings[0]["code"], "cosmetic_disabled")
        self.assertEqual(svc.get_loadout(self.db, self.identity.id, settings=self.settings), [])

    def test_structural_errors_still_reject_whole_request(self):
        """请求体本身非法仍然整条 4xx（部分成功只针对"时装不存在/已下架"）。"""
        cases = {
            "invalid_slot": [{"slot": "boots", "cosmeticId": "cape_dragon"}],
            "duplicate_slot": [
                {"slot": "cape", "cosmeticId": "cape_dragon"},
                {"slot": "cape", "cosmeticId": "wings_angel"},
            ],
            "invalid_cosmetic_id": [{"slot": "cape", "cosmeticId": "  "}],
            "invalid_scale": [{"slot": "cape", "cosmeticId": "cape_dragon", "scale": "abc"}],
        }
        for code, items in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(svc.FashionServiceError) as ctx:
                    svc.put_loadout(self.db, self.identity.id, items, settings=self.settings)
                self.assertEqual(ctx.exception.code, code)
                self.assertGreaterEqual(ctx.exception.status_code, 400)

    def test_too_many_items_rejected(self):
        settings = make_settings(asset_dir=self.tmp.name, max_loadout_items=1)
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.put_loadout(
                self.db,
                self.identity.id,
                [
                    {"slot": "cape", "cosmeticId": "cape_dragon"},
                    {"slot": "wings", "cosmeticId": "wings_angel"},
                ],
                settings=settings,
            )
        self.assertEqual(ctx.exception.code, "too_many_items")

    def test_scale_is_clamped(self):
        svc.put_loadout(
            self.db,
            self.identity.id,
            [
                {"slot": "cape", "cosmeticId": "cape_dragon", "scale": 99},
                {"slot": "wings", "cosmeticId": "wings_angel", "scale": 0.01},
            ],
            settings=self.settings,
        )
        by_slot = {it["slot"]: it for it in svc.get_loadout(self.db, self.identity.id)}
        self.assertEqual(by_slot["cape"]["scale"], svc.MAX_SCALE)
        self.assertEqual(by_slot["wings"]["scale"], svc.MIN_SCALE)

    def test_invalid_scale_rejected(self):
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.put_loadout(
                self.db,
                self.identity.id,
                [{"slot": "cape", "cosmeticId": "cape_dragon", "scale": "abc"}],
                settings=self.settings,
            )
        self.assertEqual(ctx.exception.code, "invalid_scale")

    def test_loadout_marks_missing_when_catalog_deleted(self):
        svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "cape", "cosmeticId": "cape_dragon"}],
            settings=self.settings,
        )
        svc.delete_catalog_item(self.db, "cape_dragon")
        items = svc.get_loadout(self.db, self.identity.id)
        self.assertTrue(items[0]["missing"])
        self.assertEqual(items[0]["assetKey"], "")


# ── 批量 resolve（热路径） ──────────────────────────────────────────────

class TestResolve(_Base):
    def _seed(self):
        svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "cape", "cosmeticId": "cape_dragon", "scale": 1.25}],
            settings=self.settings,
        )

    def test_returns_asset_metadata_not_content(self):
        self._seed()
        payload = svc.resolve_loadouts(self.db, [UUID_A], settings=self.settings)
        self.assertEqual(payload["missing"], [])
        items = payload["loadouts"][UUID_A]["items"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["assetKey"], "cape/dragon.png")
        self.assertEqual(item["slot"], "cape")
        self.assertEqual(item["scale"], 1.25)
        # 绝不内联资产：返回项里不能出现 base64/data/内容字段
        for forbidden in ("data", "content", "base64", "bytes_data"):
            self.assertNotIn(forbidden, item)

    def test_unknown_uuid_goes_missing(self):
        self._seed()
        payload = svc.resolve_loadouts(
            self.db, [UUID_A, UUID_B], settings=self.settings
        )
        self.assertIn(UUID_A, payload["loadouts"])
        self.assertEqual(payload["missing"], [UUID_B])

    def test_identity_without_loadout_is_not_missing(self):
        self.identity_for(UUID_B, "Alex")
        payload = svc.resolve_loadouts(self.db, [UUID_B], settings=self.settings)
        self.assertIn(UUID_B, payload["loadouts"])
        self.assertEqual(payload["loadouts"][UUID_B]["items"], [])
        self.assertEqual(payload["missing"], [])

    def test_invalid_uuid_goes_missing(self):
        payload = svc.resolve_loadouts(
            self.db, ["not-a-uuid", UUID_A], settings=self.settings
        )
        self.assertIn("not-a-uuid", payload["missing"])
        self.assertNotIn("not-a-uuid", payload["loadouts"])

    def test_duplicate_uuids_deduped(self):
        payload = svc.resolve_loadouts(
            self.db, [UUID_A, UUID_A.upper()], settings=self.settings
        )
        self.assertEqual(list(payload["loadouts"]), [UUID_A])

    def test_limit_enforced(self):
        settings = make_settings(asset_dir=self.tmp.name, resolve_max_uuids=2)
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.resolve_loadouts(
                self.db, [UUID_A, UUID_B, UUID_C], settings=settings
            )
        self.assertEqual(ctx.exception.code, "too_many_uuids")

        ok = svc.resolve_loadouts(self.db, [UUID_A, UUID_B], settings=settings)
        # UUID_A 是 setUp 里已存在的身份（无穿戴 → items 空），UUID_B 不存在
        self.assertIn(UUID_A, ok["loadouts"])
        self.assertEqual(ok["missing"], [UUID_B])

    def test_cache_avoids_repeated_queries(self):
        self._seed()
        svc.resolve_loadouts(self.db, [UUID_A], settings=self.settings)
        before = self.db.query_count
        again = svc.resolve_loadouts(self.db, [UUID_A], settings=self.settings)
        self.assertEqual(self.db.query_count, before, "命中缓存时不应再查库")
        self.assertEqual(again["loadouts"][UUID_A]["items"][0]["assetKey"], "cape/dragon.png")

    def test_put_loadout_invalidates_resolve_cache(self):
        svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "cape", "cosmeticId": "cape_dragon"}],
            settings=self.settings,
        )
        svc.resolve_loadouts(self.db, [UUID_A], settings=self.settings)
        svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "wings", "cosmeticId": "wings_angel"}],
            settings=self.settings,
        )
        payload = svc.resolve_loadouts(self.db, [UUID_A], settings=self.settings)
        self.assertEqual(
            [it["slot"] for it in payload["loadouts"][UUID_A]["items"]], ["wings"]
        )


# ── 目录 ────────────────────────────────────────────────────────────────

class TestCatalog(_Base):
    def test_list_sorted_and_enabled_only(self):
        svc.upsert_catalog_item(
            self.db, {"cosmeticId": "aura_glow", "category": "aura", "sortOrder": 0}
        )
        svc.upsert_catalog_item(
            self.db, {"cosmeticId": "hat_off", "category": "hat", "enabled": False}
        )
        items = svc.list_catalog(self.db, settings=self.settings)
        ids = [it["cosmeticId"] for it in items]
        self.assertEqual(ids, ["aura_glow", "cape_dragon", "wings_angel"])
        self.assertNotIn("hat_off", ids)

        all_items = svc.list_catalog(self.db, enabled_only=False, settings=self.settings)
        self.assertIn("hat_off", [it["cosmeticId"] for it in all_items])

    def test_filter_by_category(self):
        items = svc.list_catalog(self.db, category="wings", settings=self.settings)
        self.assertEqual([it["cosmeticId"] for it in items], ["wings_angel"])

    def test_cache_avoids_repeated_queries(self):
        svc.list_catalog(self.db, settings=self.settings)
        before = self.db.query_count
        svc.list_catalog(self.db, settings=self.settings)
        self.assertEqual(self.db.query_count, before)

    def test_write_invalidates_cache(self):
        svc.list_catalog(self.db, settings=self.settings)
        svc.upsert_catalog_item(self.db, {"cosmeticId": "pet_cat", "category": "pet"})
        ids = [it["cosmeticId"] for it in svc.list_catalog(self.db, settings=self.settings)]
        self.assertIn("pet_cat", ids)

    def test_invalid_category_rejected(self):
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.upsert_catalog_item(self.db, {"cosmeticId": "x", "category": "sword"})
        self.assertEqual(ctx.exception.code, "invalid_category")

    def test_asset_key_traversal_rejected(self):
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.upsert_catalog_item(
                self.db, {"cosmeticId": "evil", "assetKey": "../../etc/passwd"}
            )
        self.assertEqual(ctx.exception.code, "invalid_asset_key")


class TestCosmeticIdContract(_Base):
    """cosmeticId = ``<packId>:<localId>`` 契约（审计 A1/A2，P0）。

    客户端只认冒号前缀来决定整包下发（``AssetSyncService.requestMissing``），
    所以后端必须保证「前缀 == packId」，否则客户端会去下载一个不存在的包。
    """

    def test_global_id_with_matching_pack_id_ok(self):
        item = svc.upsert_catalog_item(
            self.db, {"cosmeticId": "pack1:cape_blue", "packId": "pack1"}
        )
        self.assertEqual(item["cosmeticId"], "pack1:cape_blue")
        self.assertEqual(item["packId"], "pack1")

    def test_global_id_prefix_mismatch_rejected(self):
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.upsert_catalog_item(
                self.db, {"cosmeticId": "pack2:cape_blue", "packId": "pack1"}
            )
        self.assertEqual(ctx.exception.code, "invalid_cosmetic_id")
        self.assertIn("pack1", ctx.exception.message)

    def test_global_id_infers_pack_id_when_omitted(self):
        """只给 cosmeticId 时按规则校验：前缀即 packId。"""
        item = svc.upsert_catalog_item(self.db, {"cosmeticId": "pack9:hat_red"})
        self.assertEqual(item["packId"], "pack9")

    def test_partial_update_keeps_pack_id_and_validates_prefix(self):
        # 只改 name：packId 不在 payload 里 → 用旧行的 pack1 校验前缀
        item = svc.upsert_catalog_item(
            self.db, {"cosmeticId": "pack1:cape_dragon", "name": "改名"}
        )
        self.assertEqual(item["packId"], "pack1")
        self.assertEqual(item["name"], "改名")

    def test_malformed_global_ids_rejected(self):
        cases = (
            ":cape_blue",            # 前缀为空
            "pack1:",                # localId 为空
            "pack1:cape:blue",       # 多个冒号
            "pack 1:cape_blue",      # 前缀含空格（不满足 packId 规则）
            ".hidden:cape_blue",     # 点开头 packId（客户端会当残留目录）
            "server:cape_blue",      # 保留 packId
            "pack1:cape blue",       # localId 含空格
        )
        for cosmetic_id in cases:
            with self.subTest(cosmeticId=cosmetic_id):
                with self.assertRaises(svc.FashionServiceError) as ctx:
                    svc.upsert_catalog_item(
                        self.db, {"cosmeticId": cosmetic_id, "packId": "pack1"}
                    )
                self.assertEqual(ctx.exception.code, "invalid_cosmetic_id")

    def test_invalid_pack_id_rejected(self):
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.upsert_catalog_item(
                self.db, {"cosmeticId": "legacy_hat", "packId": "pack 1"}
            )
        self.assertEqual(ctx.exception.code, "invalid_pack_id")

    def test_legacy_simple_id_still_accepted(self):
        """历史目录数据（简单 ID + packId）必须继续能写，否则旧数据无法维护。"""
        item = svc.upsert_catalog_item(
            self.db, {"cosmeticId": "legacy_backboard", "packId": "pack1"}
        )
        self.assertEqual(item["cosmeticId"], "legacy_backboard")
        self.assertEqual(item["packId"], "pack1")

    def test_empty_cosmetic_id_rejected(self):
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.upsert_catalog_item(self.db, {"cosmeticId": "   "})
        self.assertEqual(ctx.exception.code, "invalid_cosmetic_id")


# ── 资产 ────────────────────────────────────────────────────────────────

class TestAssets(_Base):
    def test_safe_join_blocks_traversal(self):
        for bad in ("../secret", "a/../../b", "/etc/passwd", "C:\\windows\\x", "", "  "):
            with self.assertRaises(svc.FashionServiceError):
                svc.safe_join(self.tmp_path(), bad)

    def test_sanitize_filename_keeps_extension_for_non_ascii_names(self):
        """中文/带空格的文件名清洗后必须**保留扩展名**。

        回归（v2.0.1 第 7 轮）：旧实现整体把非 ``[A-Za-z0-9._-]`` 替换成 ``-``，
        ``深渊龙翼.zip`` → ``-.zip`` → ``strip("-.")`` → ``zip``，扩展名被吃掉，
        上传直接 400 ``unsupported_asset_type``（"不支持的资产类型 (无扩展名)"）。
        """
        self.assertEqual(svc._sanitize_filename("深渊龙翼.zip"), "file.zip")
        self.assertEqual(svc._sanitize_filename("龙翼 披风.PNG"), "file.PNG")
        self.assertEqual(svc._sanitize_filename("a b.png"), "a-b.png")
        # 纯 ASCII 文件名行为不变
        self.assertEqual(svc._sanitize_filename("dragon.png"), "dragon.png")
        self.assertEqual(svc._sanitize_filename("wings.zip"), "wings.zip")
        self.assertEqual(svc._sanitize_filename("../../etc/passwd"), "passwd")
        # 既没有主名也没有扩展名 → invalid_filename（与旧行为一致）
        for bad in ("翅膀", "", "   ", "///"):
            with self.subTest(bad=bad):
                with self.assertRaises(svc.FashionServiceError) as ctx:
                    svc._sanitize_filename(bad)
                self.assertEqual(ctx.exception.code, "invalid_filename")
        # 超长文件名仍截到 120 字符以内且保住扩展名
        long_name = svc._sanitize_filename("a" * 300 + ".png")
        self.assertLessEqual(len(long_name), 120)
        self.assertTrue(long_name.endswith(".png"))

    def test_store_asset_accepts_non_ascii_filename(self):
        """中文文件名不再被当成"无扩展名"拒绝（.png 走扩展名白名单）。"""
        data = b"\x89PNG\r\n\x1a\nfake"
        asset = svc.store_asset(
            self.db,
            pack_id="pack1",
            version="1",
            filename="龙翼披风.png",
            data=data,
            settings=self.settings,
        )
        self.assertTrue(asset["storagePath"].endswith(".png"))
        stored = self.tmp_path() / asset["storagePath"]
        self.assertTrue(stored.is_file())

    def tmp_path(self):
        from pathlib import Path

        return Path(self.tmp.name)

    def test_store_and_resolve(self):
        data = b"\x89PNG\r\n\x1a\nfake-image-bytes"
        asset = svc.store_asset(
            self.db,
            pack_id="pack1",
            version="1",
            filename="dragon.png",
            data=data,
            settings=self.settings,
        )
        self.assertEqual(asset["bytes"], len(data))
        self.assertEqual(len(asset["sha256"]), 64)
        stored = self.tmp_path() / asset["storagePath"]
        self.assertTrue(stored.is_file(), "资产必须真的落盘")
        self.assertEqual(stored.read_bytes(), data)

        svc.upsert_catalog_item(
            self.db,
            {
                "cosmeticId": "cape_dragon",
                "assetKey": "cape/dragon.png",
                "sha256": asset["sha256"],
                "bytes": asset["bytes"],
            },
        )
        resolved = svc.resolve_asset_file(
            self.db, "cape/dragon.png", settings=self.settings
        )
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["path"], stored)
        self.assertEqual(resolved["sha256"], asset["sha256"])
        self.assertEqual(resolved["bytes"], len(data))

    def test_store_dedupes_same_content(self):
        data = b"same-bytes"
        first = svc.store_asset(
            self.db, pack_id="p", version="1", filename="a.png", data=data,
            settings=self.settings,
        )
        second = svc.store_asset(
            self.db, pack_id="p", version="1", filename="a.png", data=data,
            settings=self.settings,
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.db.rows(FashionAssetModel)), 1)

    def test_reject_bad_extension(self):
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.store_asset(
                self.db, pack_id="p", version="1", filename="shell.sh",
                data=b"#!/bin/sh", settings=self.settings,
            )
        self.assertEqual(ctx.exception.code, "unsupported_asset_type")

    def test_reject_empty(self):
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.store_asset(
                self.db, pack_id="p", version="1", filename="a.png", data=b"",
                settings=self.settings,
            )
        self.assertEqual(ctx.exception.code, "empty_asset")

    def test_fallback_to_static_file(self):
        manual = self.tmp_path() / "manual" / "hat.png"
        manual.parent.mkdir(parents=True, exist_ok=True)
        manual.write_bytes(b"static-file")
        svc.upsert_catalog_item(
            self.db,
            {"cosmeticId": "hat_static", "category": "hat", "assetKey": "manual/hat.png"},
        )
        resolved = svc.resolve_asset_file(self.db, "manual/hat.png", settings=self.settings)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["path"], manual)

    def test_traversal_key_returns_none(self):
        self.assertIsNone(
            svc.resolve_asset_file(self.db, "../../etc/passwd", settings=self.settings)
        )

    def test_disabled_catalog_asset_hidden(self):
        svc.upsert_catalog_item(
            self.db,
            {"cosmeticId": "cape_dragon", "enabled": False, "assetKey": "cape/dragon.png"},
        )
        self.assertIsNone(
            svc.resolve_asset_file(self.db, "cape/dragon.png", settings=self.settings)
        )

    def test_unknown_key_returns_none(self):
        self.assertIsNone(
            svc.resolve_asset_file(self.db, "no/such.png", settings=self.settings)
        )


# ── 包级下发：zip 静态校验 / 清单 / 签名 ────────────────────────────────

def make_zip(entries, *, compression=zipfile.ZIP_DEFLATED) -> bytes:
    """构造 zip 字节（测试辅助）。``entries``：文件名 → 内容（str/bytes）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def make_symlink_zip(link_name: str = "link.png") -> bytes:
    """构造一个含符号链接条目的 zip（Python 写不出真链接，只能手改 external_attr）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo(link_name)
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
        archive.writestr("pack.json", '{"id": "wings"}')
    return buffer.getvalue()


PACK_MANIFEST = '{"id": "wings", "version": "1.2.0", "name": "翅膀"}'


class TestPackZipValidation(unittest.TestCase):
    """上传时的 zip 安全校验（mod 契约 §2 / §6.8.1：坏包在服务端就该被挡住）。"""

    def test_valid_pack_passes(self):
        data = make_zip({
            "pack.json": PACK_MANIFEST,
            "cosmetics/crown/model.geo.json": "{}",
            "cosmetics/crown/texture.png": b"\x89PNG",
        })
        info = svc.validate_pack_zip(data, expected_pack_id="wings")
        self.assertEqual(info["id"], "wings")
        self.assertEqual(info["files"], 3)

    def test_manifest_filename_is_case_sensitive(self):
        """客户端安装后按 "pack.json" 精确读取：PACK.JSON 装上也读不到，必须拒。"""
        data = make_zip({"PACK.JSON": PACK_MANIFEST})
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(data)
        self.assertIn("pack.json", ctx.exception.message)

    def test_manifest_with_dot_slash_prefix_accepted(self):
        data = make_zip({"./pack.json": PACK_MANIFEST, "./cosmetics/a.png": b"x"})
        self.assertEqual(svc.validate_pack_zip(data, expected_pack_id="wings")["id"], "wings")

    def test_manifest_id_is_case_insensitive(self):
        data = make_zip({"pack.json": '{"id": "WINGS"}'})
        self.assertEqual(
            svc.validate_pack_zip(data, expected_pack_id="wings")["id"], "WINGS"
        )

    def test_manifest_required(self):
        data = make_zip({"cosmetics/crown/texture.png": b"x"})
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(data)
        self.assertEqual(ctx.exception.code, "invalid_pack_zip")
        self.assertIn("pack.json", ctx.exception.message)

    def test_manifest_id_mismatch_rejected(self):
        data = make_zip({"pack.json": '{"id": "wings"}'})
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(data, expected_pack_id="cape")
        self.assertEqual(ctx.exception.code, "pack_id_mismatch")

    def test_not_a_zip(self):
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(b"\x89PNG not a zip")
        self.assertEqual(ctx.exception.code, "invalid_pack_zip")

    def test_path_traversal_rejected(self):
        data = make_zip({"pack.json": PACK_MANIFEST, "../evil.png": b"x"})
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(data)
        self.assertIn("..", ctx.exception.message)

    def test_absolute_path_rejected(self):
        data = make_zip({"pack.json": PACK_MANIFEST, "/etc/passwd": b"x"})
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(data)
        self.assertIn("绝对路径", ctx.exception.message)

    def test_windows_drive_rejected(self):
        data = make_zip({"pack.json": PACK_MANIFEST, "C:/windows/x.png": b"x"})
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(data)
        self.assertIn("盘符", ctx.exception.message)

    def test_ntfs_alt_stream_rejected(self):
        data = make_zip({"pack.json": PACK_MANIFEST, "texture.png:evil": b"x"})
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(data)
        self.assertIn("NTFS", ctx.exception.message)

    def test_windows_reserved_name_rejected(self):
        data = make_zip({"pack.json": PACK_MANIFEST, "CON.txt": b"x"})
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(data)
        self.assertIn("保留设备名", ctx.exception.message)

    def test_symlink_rejected(self):
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(make_symlink_zip())
        self.assertIn("符号链接", ctx.exception.message)

    def test_executable_rejected(self):
        data = make_zip({"pack.json": PACK_MANIFEST, "cosmetics/evil.exe": b"MZ"})
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(data)
        self.assertIn("可执行文件", ctx.exception.message)

    def test_hidden_file_rejected(self):
        data = make_zip({"pack.json": PACK_MANIFEST, "cosmetics/.hidden.png": b"x"})
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(data)
        self.assertIn("隐藏文件", ctx.exception.message)

    def test_case_insensitive_duplicate_rejected(self):
        data = make_zip({
            "pack.json": PACK_MANIFEST,
            "cosmetics/A.png": b"a",
            "cosmetics/a.png": b"b",
        })
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(data)
        self.assertIn("重复", ctx.exception.message)

    def test_file_dir_conflict_rejected(self):
        data = make_zip({
            "pack.json": PACK_MANIFEST,
            "cosmetics/crown": b"i-am-a-file",
            "cosmetics/crown/texture.png": b"x",
        })
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(data)
        self.assertIn("同名", ctx.exception.message)

    def test_unsupported_compression_rejected(self):
        data = make_zip(
            {"pack.json": PACK_MANIFEST}, compression=zipfile.ZIP_BZIP2
        )
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(data)
        self.assertIn("压缩算法", ctx.exception.message)

    def test_too_many_entries_rejected(self):
        original = svc.ZIP_MAX_ENTRIES
        svc.ZIP_MAX_ENTRIES = 3
        self.addCleanup(setattr, svc, "ZIP_MAX_ENTRIES", original)
        data = make_zip({
            "pack.json": PACK_MANIFEST,
            "a.png": b"1",
            "b.png": b"2",
            "c.png": b"3",
        })
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(data)
        self.assertIn("条目数", ctx.exception.message)

    def test_entry_too_large_rejected(self):
        original = svc.ZIP_MAX_ENTRY_BYTES
        svc.ZIP_MAX_ENTRY_BYTES = 16
        self.addCleanup(setattr, svc, "ZIP_MAX_ENTRY_BYTES", original)
        data = make_zip({"pack.json": PACK_MANIFEST, "big.bin": b"x" * 64})
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(data)
        self.assertIn("单个条目", ctx.exception.message)

    def test_zip_bomb_ratio_rejected(self):
        original_min = svc.ZIP_RATIO_MIN_UNCOMPRESSED
        original_limit = svc.ZIP_RATIO_LIMIT
        svc.ZIP_RATIO_MIN_UNCOMPRESSED = 1024
        svc.ZIP_RATIO_LIMIT = 2.0
        self.addCleanup(setattr, svc, "ZIP_RATIO_MIN_UNCOMPRESSED", original_min)
        self.addCleanup(setattr, svc, "ZIP_RATIO_LIMIT", original_limit)
        data = make_zip({"pack.json": PACK_MANIFEST, "zeros.bin": b"\x00" * 20000})
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(data)
        self.assertIn("压缩比", ctx.exception.message)


class TestPackManifest(_Base):
    """``/packs`` 清单：派生自 .zip 资产行，字段必须符合 mod 契约。"""

    def make_pack(self, pack_id="wings", version="1.2.0", **overrides):
        manifest = '{"id": "%s"}' % pack_id
        data = make_zip({
            "pack.json": manifest,
            "cosmetics/crown/model.geo.json": "{}",
            "cosmetics/crown/texture.png": b"\x89PNG-fake",
        })
        return svc.store_asset(
            self.db,
            pack_id=pack_id,
            version=version,
            filename=f"{pack_id}.zip",
            data=data,
            settings=self.settings,
            pack_zip=True,
            **overrides,
        )

    def test_manifest_shape(self):
        self.make_pack()
        packs = svc.list_packs(self.db, settings=self.settings)
        self.assertEqual(len(packs), 1)
        item = packs[0]
        self.assertEqual(item["id"], "wings")
        self.assertEqual(item["version"], "1.2.0")
        self.assertEqual(len(item["sha256"]), 64)
        self.assertGreater(item["bytes"], 0)
        # 相对 mod 的 apiBaseUrl（.../api/fashion），不能带 /api/fashion 前缀
        self.assertEqual(item["url"], "/packs/wings/1.2.0/download")
        # publishedAt 必须是毫秒时间戳 number（客户端 getAsLong）
        self.assertIsInstance(item["publishedAt"], int)
        self.assertGreater(item["publishedAt"], 1_600_000_000_000)
        # 没配置签名私钥 → 不输出 signature / signingKeyId
        self.assertNotIn("signature", item)
        self.assertNotIn("signingKeyId", item)

    def test_pack_versions_feed_resolve_and_loadout(self):
        self.make_pack()
        svc.put_loadout(
            self.db,
            self.identity.id,
            [{"slot": "cape", "cosmeticId": "cape_dragon"}],
        )
        # 目录条目属于 pack1，没有对应 zip → packVersion 空串但 packId 有值
        resolved = svc.resolve_loadouts(self.db, [UUID_A], settings=self.settings)
        item = resolved["loadouts"][UUID_A]["items"][0]
        self.assertEqual(item["packId"], "pack1")
        self.assertEqual(item["packVersion"], "")

        # 给 pack1 发布一个 zip 后，resolve 与 loadout 都要带上版本
        self.make_pack(pack_id="pack1", version="2.0.0")
        svc.invalidate_caches()
        resolved = svc.resolve_loadouts(self.db, [UUID_A], settings=self.settings)
        item = resolved["loadouts"][UUID_A]["items"][0]
        self.assertEqual(item["packId"], "pack1")
        self.assertEqual(item["packVersion"], "2.0.0")

        loadout = svc.get_loadout(self.db, self.identity.id, settings=self.settings)
        self.assertEqual(loadout[0]["packId"], "pack1")
        self.assertEqual(loadout[0]["packVersion"], "2.0.0")

    def test_non_zip_assets_are_not_packs(self):
        svc.store_asset(
            self.db, pack_id="wings", version="1", filename="texture.png",
            data=b"\x89PNG", settings=self.settings,
        )
        self.assertEqual(svc.list_packs(self.db, settings=self.settings), [])

    def test_unsafe_rows_are_skipped(self):
        from vmtools_next.data.models.fashion import FashionAssetModel

        self.make_pack()
        # 直接插一条脏行（历史数据 / 手工 SQL）：packId 不安全 → 必须被清单忽略
        self.db.add(FashionAssetModel(
            id="dirty-1",
            pack_id="../evil",
            version="1",
            filename="evil.zip",
            sha256="a" * 64,
            bytes=10,
            storage_path="../evil.zip",
            created_at=svc.utcnow(),
        ))
        svc.invalidate_caches()
        packs = svc.list_packs(self.db, settings=self.settings)
        self.assertEqual([item["id"] for item in packs], ["wings"])

    def test_newest_version_listed_first_per_pack(self):
        self.make_pack(version="1.0.0")
        self.make_pack(version="2.0.0")
        packs = svc.list_packs(self.db, settings=self.settings)
        self.assertEqual([item["version"] for item in packs], ["1.0.0", "2.0.0"])
        versions = svc.pack_versions(self.db, settings=self.settings)
        self.assertEqual(versions["wings"], "2.0.0")

    def test_get_pack_asset_rejects_traversal(self):
        self.make_pack()
        for bad in ("..", "../wings", "/etc", "wings/../..", "server", ".hidden"):
            with self.subTest(pack_id=bad):
                self.assertIsNone(svc.get_pack_asset(self.db, bad, "1.2.0", settings=self.settings))
        self.assertIsNone(svc.get_pack_asset(self.db, "wings", "../1", settings=self.settings))
        self.assertIsNone(svc.get_pack_asset(self.db, "wings", "9.9.9", settings=self.settings))
        found = svc.get_pack_asset(self.db, "wings", "1.2.0", settings=self.settings)
        self.assertIsNotNone(found)
        self.assertTrue(found["storagePath"].endswith(".zip"))

    def test_delete_pack(self):
        self.make_pack()
        self.assertTrue(svc.delete_pack(self.db, "wings", "1.2.0", settings=self.settings))
        self.assertEqual(svc.list_packs(self.db, settings=self.settings), [])
        self.assertFalse(svc.delete_pack(self.db, "wings", "1.2.0", settings=self.settings))

    def test_delete_pack_refuses_when_catalog_references_sha256(self):
        asset = self.make_pack()
        svc.upsert_catalog_item(
            self.db, {"cosmeticId": "cape_dragon", "sha256": asset["sha256"]}
        )
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.delete_pack(self.db, "wings", "1.2.0", settings=self.settings)
        self.assertEqual(ctx.exception.code, "pack_in_use")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_delete_pack_validates_ids(self):
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.delete_pack(self.db, "../evil", "1")
        self.assertEqual(ctx.exception.code, "invalid_pack_id")
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.delete_pack(self.db, "wings", "../1")
        self.assertEqual(ctx.exception.code, "invalid_pack_version")

    def test_store_asset_pack_zip_requires_safe_pack_id(self):
        data = make_zip({"pack.json": PACK_MANIFEST})
        for bad in ("../wings", ".hidden", "server", "a" * 65, ""):
            with self.subTest(pack_id=bad):
                with self.assertRaises(svc.FashionServiceError) as ctx:
                    svc.store_asset(
                        self.db, pack_id=bad, version="1", filename="wings.zip",
                        data=data, settings=self.settings, pack_zip=True,
                    )
                self.assertIn(ctx.exception.code, ("invalid_pack_id", "invalid_pack_version"))

    def test_store_asset_dedupes_by_pack_and_version(self):
        data = make_zip({"pack.json": PACK_MANIFEST})
        first = svc.store_asset(
            self.db, pack_id="wings", version="1", filename="wings.zip",
            data=data, settings=self.settings, pack_zip=True,
        )
        second = svc.store_asset(
            self.db, pack_id="wings", version="2", filename="wings.zip",
            data=data, settings=self.settings, pack_zip=True,
        )
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(svc.list_packs(self.db, settings=self.settings)), 2)


    def test_zip_uploaded_as_plain_asset_is_validated_as_pack(self):
        """/admin/assets 传 .zip 也要按包装校验：否则 /packs 会下发装不上的包。"""
        data = make_zip({"pack.json": '{"id": "wings"}'})
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.store_asset(
                self.db, pack_id="default", version="1", filename="wings.zip",
                data=data, settings=self.settings,
            )
        self.assertEqual(ctx.exception.code, "pack_id_mismatch")
        # 对齐 packId 后可以发布
        asset = svc.store_asset(
            self.db, pack_id="wings", version="1", filename="wings.zip",
            data=data, settings=self.settings,
        )
        self.assertTrue(asset["storagePath"].endswith(".zip"))

    def test_store_pack_with_non_ascii_filename(self):
        """中文文件名上传时装包：清洗后必须仍是 ``.zip``（否则被判"无扩展名" 400）。"""
        data = make_zip({"pack.json": PACK_MANIFEST})
        asset = svc.store_asset(
            self.db, pack_id="wings", version="1.2.0", filename="深渊龙翼.zip",
            data=data, settings=self.settings, pack_zip=True,
        )
        self.assertTrue(asset["storagePath"].endswith(".zip"))
        self.assertEqual(len(svc.list_packs(self.db, settings=self.settings)), 1)

    def test_manifest_too_large_rejected(self):
        original = svc.ZIP_MAX_MANIFEST_BYTES
        svc.ZIP_MAX_MANIFEST_BYTES = 4
        self.addCleanup(setattr, svc, "ZIP_MAX_MANIFEST_BYTES", original)
        data = make_zip({"pack.json": '{"id": "wings"}'})
        with self.assertRaises(svc.FashionServiceError) as ctx:
            svc.validate_pack_zip(data)
        self.assertIn("pack.json", ctx.exception.message)

    def test_manifest_includes_signature_when_key_configured(self):
        self.make_pack()
        original = svc.pack_signature

        def fake_signature(pack_id, version, sha256, *, settings=None, signer=None):
            return "c2lnbmF0dXJl", "vmtools-official"

        svc.pack_signature = fake_signature
        self.addCleanup(setattr, svc, "pack_signature", original)
        svc.invalidate_caches()
        item = svc.list_packs(self.db, settings=self.settings)[0]
        self.assertEqual(item["signature"], "c2lnbmF0dXJl")
        self.assertEqual(item["signingKeyId"], "vmtools-official")


class TestPackSignature(unittest.TestCase):
    """Ed25519 签名的规范化字节串 / 编码（mod 契约 §6）。"""

    def test_canonical_string(self):
        canonical = svc.pack_canonical_string(" wings ", " 1.2.0 ", "ABC123")
        self.assertEqual(canonical, b"wings\n1.2.0\nabc123")
        # 三段之间单个 \n，结尾没有换行
        self.assertFalse(canonical.endswith(b"\n"))

    def test_signature_base64_and_key_id(self):
        settings = types.SimpleNamespace(
            pack_signing_key_b64="ignored-in-test",
            pack_signing_key_id="vmtools-official",
        )
        seen: list[bytes] = []

        def fake_signer(payload: bytes) -> bytes:
            seen.append(payload)
            return bytes(range(64))

        signature, key_id = svc.pack_signature(
            "wings", "1.2.0", "a" * 64, settings=settings, signer=fake_signer
        )
        self.assertEqual(seen, [svc.pack_canonical_string("wings", "1.2.0", "a" * 64)])
        self.assertEqual(key_id, "vmtools-official")
        # 标准 base64（不是 URL-safe），64 字节 → 88 字符（含一个 '='）
        self.assertEqual(len(signature), 88)
        self.assertNotIn("-", signature)
        self.assertNotIn("_", signature)
        import base64 as b64

        self.assertEqual(b64.b64decode(signature), bytes(range(64)))

    def test_no_key_means_no_signature(self):
        settings = types.SimpleNamespace()
        self.assertEqual(svc.pack_signature("wings", "1.2.0", "a" * 64, settings=settings), ("", ""))

    def test_broken_signer_degrades_to_empty(self):
        settings = types.SimpleNamespace(
            pack_signing_key_b64="not-a-real-key", pack_signing_key_id="x"
        )

        def broken(_payload: bytes) -> bytes:
            raise ValueError("boom")

        self.assertEqual(
            svc.pack_signature("wings", "1.2.0", "a" * 64, settings=settings, signer=broken),
            ("", ""),
        )


# ── ISO 时间格式（SQLite naive datetime 按 UTC 输出） ──────────────────

class TestIsoTimestamp(unittest.TestCase):
    def test_naive_datetime_is_rendered_as_utc(self):
        from datetime import datetime

        self.assertIsNone(svc.iso(None))
        self.assertEqual(
            svc.iso(datetime(2026, 1, 1, 12, 0, 0)),
            "2026-01-01T12:00:00+00:00",
        )


# ── alembic 迁移声明 ────────────────────────────────────────────────────

class _FakeOp:
    def __init__(self):
        self.created: list[str] = []
        self.indexes: list[tuple[str, str]] = []
        self.dropped: list[str] = []

    def get_bind(self):
        return object()

    def create_table(self, name, *columns, **kwargs):
        self.created.append(name)

    def create_index(self, name, table, columns, **kwargs):
        self.indexes.append((name, table))

    def drop_table(self, name):
        self.dropped.append(name)


class TestMigration(unittest.TestCase):
    MIGRATION = os.path.join(
        _HERE, os.pardir, "alembic", "versions", "20261028_fashion.py"
    )

    def _load(self):
        op = _FakeOp()
        alembic_stub = types.ModuleType("alembic")
        alembic_stub.op = op
        previous = sys.modules.get("alembic")
        sys.modules["alembic"] = alembic_stub
        try:
            spec = importlib.util.spec_from_file_location(
                "fashion_migration_under_test", os.path.abspath(self.MIGRATION)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            if previous is None:
                sys.modules.pop("alembic", None)
            else:
                sys.modules["alembic"] = previous
        return module, op

    def test_creates_all_four_tables(self):
        module, op = self._load()
        module._existing_tables = lambda: set()
        module.upgrade()
        self.assertEqual(
            op.created,
            ["fashion_identity", "fashion_catalog", "fashion_loadout", "fashion_asset"],
        )
        index_tables = {table for _name, table in op.indexes}
        self.assertEqual(
            index_tables,
            {"fashion_identity", "fashion_catalog", "fashion_loadout", "fashion_asset"},
        )
        # 唯一约束：一个身份一个槽位
        self.assertTrue(
            any(name == "uq_fashion_loadout_identity_slot" or "identity_slot" in name
                for name, _table in op.indexes)
        )

    def test_upgrade_is_idempotent(self):
        module, op = self._load()
        module._existing_tables = lambda: {
            "fashion_identity", "fashion_catalog", "fashion_loadout", "fashion_asset"
        }
        module.upgrade()
        self.assertEqual(op.created, [], "表已存在时不应重复建表（create_all 已建过）")

    def test_revision_graph(self):
        module, _op = self._load()
        self.assertTrue(module.revision)
        self.assertIsNone(module.down_revision)

    def test_downgrade_drops_in_reverse_order(self):
        module, op = self._load()
        module._existing_tables = lambda: {
            "fashion_identity", "fashion_catalog", "fashion_loadout", "fashion_asset"
        }
        module.downgrade()
        self.assertEqual(
            op.dropped,
            ["fashion_asset", "fashion_loadout", "fashion_catalog", "fashion_identity"],
        )


class _IndexOp:
    """只记录 create_index / drop_index 的 alembic op 替身（storage_path 索引迁移用）。"""

    def __init__(self):
        self.indexes: list[tuple[str, str, list]] = []
        self.dropped: list[tuple[str, str]] = []

    def get_bind(self):
        return object()  # sa.inspect(object()) 会抛 → 迁移按「探测不到」降级

    def create_index(self, name, table, columns, **kwargs):
        self.indexes.append((name, table, list(columns)))

    def drop_index(self, name, table_name=None, **kwargs):
        self.dropped.append((name, table_name))

    def create_table(self, *args, **kwargs):  # pragma: no cover - 本迁移不建表
        raise AssertionError("storage_path 索引迁移不应该建表")

    def drop_table(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("storage_path 索引迁移不应该删表")


class TestStoragePathIndexMigration(unittest.TestCase):
    """``ix_fashion_asset_storage_path`` 迁移（第 7 轮审查 M1）。

    背景：``storage_path`` 的 ``index=True`` 是 2026-11-01 事故（§六.106 全表扫描 →
    连接池打满 → 全站 504）的修复，但 ``create_all`` **不会**给已存在的表补索引，
    ``20261028_fashion`` 迁移也没建它 —— 于是「先 alembic 建表」的库（含生产）实际
    上没有这个索引。本用例锁住"必须补上、且幂等"。
    """

    MIGRATION = os.path.join(
        _HERE, os.pardir, "alembic", "versions",
        "20261103_fashion_asset_storage_path_index.py",
    )
    INDEX = "ix_fashion_asset_storage_path"

    def _load(self):
        op = _IndexOp()
        alembic_stub = types.ModuleType("alembic")
        alembic_stub.op = op
        previous = sys.modules.get("alembic")
        sys.modules["alembic"] = alembic_stub
        try:
            spec = importlib.util.spec_from_file_location(
                "fashion_asset_index_migration_under_test", os.path.abspath(self.MIGRATION)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            if previous is None:
                sys.modules.pop("alembic", None)
            else:
                sys.modules["alembic"] = previous
        return module, op

    def test_upgrade_creates_index_when_missing(self):
        module, op = self._load()
        module.upgrade()  # 探测不到索引（get_bind 是假对象）→ 按缺失处理
        self.assertEqual(op.indexes, [(self.INDEX, "fashion_asset", ["storage_path"])])

    def test_upgrade_is_idempotent_when_index_exists(self):
        module, op = self._load()
        module._index_names = lambda: {self.INDEX}
        module.upgrade()
        self.assertEqual(op.indexes, [], "索引已存在时不能重复创建（迁移要可重跑）")

    def test_upgrade_skips_when_table_missing(self):
        module, op = self._load()
        module._table_exists = lambda: False
        module.upgrade()
        self.assertEqual(op.indexes, [], "表还不存在时由 create_all 建表（自带该索引）")

    def test_revision_graph(self):
        module, _op = self._load()
        self.assertEqual(module.revision, "20261103_fashion_asset_idx")
        self.assertEqual(module.down_revision, "20261101_fashion_wardrobe")

    def test_downgrade_drops_index(self):
        module, op = self._load()
        module._index_names = lambda: {self.INDEX}
        module.downgrade()
        self.assertEqual(op.dropped, [(self.INDEX, "fashion_asset")])

    def test_downgrade_is_idempotent(self):
        module, op = self._load()
        module._index_names = lambda: set()
        module.downgrade()
        self.assertEqual(op.dropped, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
