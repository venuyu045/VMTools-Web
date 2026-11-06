"""芒果块代币购买时装 —— 后端集成测试（真实 SQLite + FastAPI TestClient）。

与 ``test_fashion_*.py`` 里那些「离线 stub + FakeSession」单测互补：本文件用
**真实 SQLAlchemy / FastAPI / pydantic** 跑 HTTP 全链路，重点验证「离线 stub 测不到」
的部分：

- 数据库唯一约束（重复购买不产生第二条 ownership）
- 条件 UPDATE 原子扣款（余额不会被扣成负数）
- 事务原子性（扣款 / 所有权 / 流水 / 衣柜 同一个 commit）
- response_model 的 camelCase 序列化、402/403/409/429/501 的状态码与错误码
- Alembic 迁移的幂等 upgrade / downgrade（另一组用例）
- 历史衣柜回填、防绕过（wardrobe / loadout）

跑法::

    cd backend && .venv/bin/pytest tests/test_fashion_mango.py -q

环境变量在 fixture 里 patch，不依赖外部 config.yaml（``VMT_SERVER__SECRET_KEY``
必须由测试显式给出，否则 config 强校验会拒绝加载）。
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
import types
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ── 环境变量必须在 import vmtools_next.config 之前就位 ───────────────────
_TEST_SECRET = "0123456789abcdef0123456789abcdef"

from vmtools_next.api import deps  # noqa: E402
from vmtools_next.api.routers import fashion as fashion_router  # noqa: E402
from vmtools_next.core import fashion_service as svc  # noqa: E402
from vmtools_next.data.db import Base  # noqa: E402
from vmtools_next.data.models import (  # noqa: E402  注册全部表（含 users）
    FashionCatalogModel,
    FashionIdentityModel,
    FashionLoadoutModel,
    FashionMangoTransactionModel,
    FashionOwnershipModel,
    FashionWalletModel,
    FashionWardrobeModel,
    UserModel,
)

USER_A = "user-aaaa-1111"
USER_B = "user-bbbb-2222"
ADMIN = "user-admin-0001"

CID_FREE = "pack1:cape_free"
CID_PAID = "pack1:cape_dragon"
CID_PAID_2 = "pack1:wings_phoenix"
CID_LOCKED = "pack1:hat_locked"


class _User:
    """get_current_user / require_site_admin 的替身（跳过真实 JWT）。"""

    def __init__(self, uid: str, role: str = "user"):
        self.id = uid
        self.role = role
        self.status = "approved"


def _restore_mapped_columns():
    """把被其它测试模块全局替换成 ``_Expr`` 的模型列换回真实映射属性。

    ``test_fashion_api.py`` / ``test_fashion_loadout.py`` 在模块级把
    ``FashionIdentityModel.mc_uuid`` / ``FashionLoadoutModel.identity_id``
    换成 stub 对象；pytest 全量收集时那会污染本文件的真实 SQL 查询。
    """
    saved = []
    for model, name in (
        (FashionIdentityModel, "mc_uuid"),
        (FashionLoadoutModel, "identity_id"),
    ):
        saved.append((model, name, getattr(model, name, None)))
        try:
            setattr(model, name, model._sa_class_manager[name])
        except Exception:
            pass
    return saved


def _put_columns_back(saved):
    for model, name, value in saved:
        setattr(model, name, value)


def _catalog(cid, name, *, price=0, purchasable=True, enabled=True, pack="pack1", category="cape"):
    return FashionCatalogModel(
        cosmetic_id=cid,
        pack_id=pack,
        name=name,
        category=category,
        asset_key=f"packs/{pack}/{cid.split(':')[-1]}.png",
        sha256="a" * 64,
        bytes=1024,
        enabled=enabled,
        sort_order=0,
        price_mango=price,
        purchasable=purchasable,
        created_at=datetime.now(timezone.utc),
    )


class MangoApiCase(unittest.TestCase):
    """每个用例一套独立内存库；避免用例间余额/所有权互相污染。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        env = {
            "VMT_SERVER__SECRET_KEY": _TEST_SECRET,
            "VMT_FASHION__ASSET_DIR": os.path.join(self._tmp.name, "assets"),
            "VMT_FASHION__CATALOG_CACHE_TTL_SECONDS": "0",
            "VMT_FASHION__RESOLVE_CACHE_TTL_SECONDS": "0",
        }
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        from vmtools_next.config import get_config

        get_config.cache_clear()
        svc.invalidate_caches()
        fashion_router._PURCHASE_HITS.clear()
        self._saved_columns = _restore_mapped_columns()

        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            future=True,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self.state = {"user": _User(USER_A)}

        db = self.Session()
        try:
            db.add_all([
                UserModel(id=USER_A, game_id="alice", display_name="小明",
                          password_hash="x", role="user", status="approved"),
                UserModel(id=USER_B, game_id="bob", display_name="阿波",
                          password_hash="x", role="user", status="approved"),
                UserModel(id=ADMIN, game_id="root", display_name="管理员",
                          password_hash="x", role="site_admin", status="approved"),
                _catalog(CID_FREE, "免费披风", price=0),
                _catalog(CID_PAID, "龙纹披风", price=30),
                _catalog(CID_PAID_2, "凤凰之翼", price=50, category="wings"),
                _catalog(CID_LOCKED, "限定头饰", price=10, purchasable=False, category="hat"),
            ])
            db.commit()
        finally:
            db.close()

        self.app = FastAPI()
        self.app.include_router(fashion_router.router)

        def _get_db():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

        self.app.dependency_overrides[deps.get_db] = _get_db
        self.app.dependency_overrides[deps.get_current_user] = lambda: self.state["user"]
        self.app.dependency_overrides[deps.require_site_admin] = lambda: _User(ADMIN, "site_admin")
        self.app.dependency_overrides[fashion_router.get_principal] = (
            lambda: fashion_router.FashionPrincipal(
                identity_id=None, user_id=USER_A, via="web"
            )
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        _put_columns_back(self._saved_columns)
        self._env.stop()
        from vmtools_next.config import get_config

        get_config.cache_clear()
        svc.invalidate_caches()
        fashion_router._PURCHASE_HITS.clear()
        self._tmp.cleanup()

    # ── 工具 ────────────────────────────────────────────────────────────
    def as_admin(self):
        self.state["user"] = _User(ADMIN, "site_admin")

    def as_user(self, uid=USER_A):
        self.state["user"] = _User(uid)

    def adjust(self, uid, delta, note="测试发放"):
        self.as_admin()
        return self.client.post("/api/fashion/admin/wallet/adjust", json={
            "userId": uid, "delta": delta, "note": note,
        })

    def purchase(self, cid, key=None, uid=USER_A):
        self.as_user(uid)
        body = {"cosmeticId": cid}
        if key is not None:
            body["clientRequestId"] = key
        return self.client.post("/api/fashion/store/purchase", json=body)

    def wallet(self, uid=USER_A):
        self.as_user(uid)
        return self.client.get("/api/fashion/wallet")

    def rows(self, model):
        db = self.Session()
        try:
            return db.query(model).all()
        finally:
            db.close()

    def assertCode(self, resp, status, code):
        self.assertEqual(resp.status_code, status, resp.text)
        detail = resp.json().get("detail")
        if isinstance(detail, dict):
            self.assertEqual(detail.get("code"), code, resp.text)
        else:
            # 401/403 由 deps 产生的是英文字符串，本文件用例不走这条路径
            self.fail(f"期望结构化错误码 {code}，实际 detail={detail!r}")

    # ── 钱包 ────────────────────────────────────────────────────────────
    def test_wallet_first_access_creates_zero_balance(self):
        resp = self.wallet()
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["balance"], 0)
        self.assertEqual(body["currency"], "芒果块")
        self.assertEqual(body["totalEarned"], 0)
        self.assertEqual(body["totalSpent"], 0)
        # 真的落库了，不是内存假值
        self.assertEqual(len(self.rows(FashionWalletModel)), 1)

    def test_wallet_is_per_user(self):
        self.adjust(USER_A, 100)
        self.assertEqual(self.wallet(USER_A).json()["balance"], 100)
        self.assertEqual(self.wallet(USER_B).json()["balance"], 0)

    # ── 商店聚合 / mod 契约兼容 ─────────────────────────────────────────
    def test_store_items_shape(self):
        self.as_user()
        resp = self.client.get("/api/fashion/store/items")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["currency"], "芒果块")
        self.assertEqual(body["wallet"]["balance"], 0)
        self.assertFalse(body["rechargeAvailable"])
        by_id = {it["cosmeticId"]: it for it in body["items"]}
        self.assertEqual(by_id[CID_FREE]["priceMango"], 0)
        self.assertEqual(by_id[CID_PAID]["priceMango"], 30)
        self.assertTrue(by_id[CID_LOCKED]["purchasable"] is False)
        for item in body["items"]:
            self.assertFalse(item["owned"])
            self.assertFalse(item["inWardrobe"])

    def test_public_catalog_contract_unchanged(self):
        """GET /catalog 是 mod 契约：不能因为加了定价就多出用户态字段。"""
        self.as_user()
        resp = self.client.get("/api/fashion/catalog")
        self.assertEqual(resp.status_code, 200, resp.text)
        keys = set(resp.json()["items"][0].keys())
        self.assertNotIn("priceMango", keys)
        self.assertNotIn("owned", keys)
        self.assertIn("cosmeticId", keys)
        self.assertIn("assetKey", keys)

    # ── 充值占位 ────────────────────────────────────────────────────────
    def test_recharge_options_not_available(self):
        self.as_user()
        resp = self.client.get("/api/fashion/wallet/recharge/options")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertFalse(body["available"])
        self.assertEqual(body["currency"], "芒果块")
        self.assertEqual(body["channels"], [])
        self.assertIn("即将开放", body["notice"])

    def test_recharge_returns_501_and_does_not_credit(self):
        self.as_user()
        resp = self.client.post("/api/fashion/wallet/recharge", json={"amount": 999})
        self.assertCode(resp, 501, "recharge_not_available")
        self.assertEqual(self.wallet().json()["balance"], 0)
        self.assertEqual(len(self.rows(FashionMangoTransactionModel)), 0)

    # ── 免费领取 ────────────────────────────────────────────────────────
    def test_free_claim_writes_ownership_and_wardrobe(self):
        resp = self.purchase(CID_FREE)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["purchased"])
        self.assertTrue(body["free"])
        self.assertEqual(body["priceMango"], 0)
        self.assertEqual(body["balance"], 0)
        self.assertEqual(body["transactionId"], "")
        self.assertEqual(body["item"]["owned"], True)
        self.assertEqual(body["item"]["inWardrobe"], True)
        self.assertEqual(len(self.rows(FashionOwnershipModel)), 1)
        self.assertEqual(len(self.rows(FashionWardrobeModel)), 1)
        self.assertEqual(len(self.rows(FashionMangoTransactionModel)), 1)
        self.assertEqual(self.rows(FashionMangoTransactionModel)[0].delta, 0)

    def test_free_claim_is_idempotent(self):
        self.purchase(CID_FREE)
        resp = self.purchase(CID_FREE)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertFalse(body["purchased"])
        self.assertTrue(body["alreadyOwned"])
        self.assertEqual(body["balance"], 0)
        self.assertEqual(len(self.rows(FashionOwnershipModel)), 1)
        self.assertEqual(len(self.rows(FashionMangoTransactionModel)), 1)

    def test_free_item_can_still_join_wardrobe(self):
        """价格 0 的时装保持上线前行为：POST /wardrobe 免费加入。"""
        self.as_user()
        resp = self.client.post("/api/fashion/wardrobe", json={"cosmeticId": CID_FREE})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["added"])

    # ── 付费购买 ────────────────────────────────────────────────────────
    def test_paid_purchase_insufficient_mango(self):
        resp = self.purchase(CID_PAID)
        self.assertCode(resp, 402, "insufficient_mango")
        self.assertIn("芒果块不足", resp.json()["detail"]["message"])
        self.assertEqual(self.wallet().json()["balance"], 0)
        self.assertEqual(len(self.rows(FashionOwnershipModel)), 0)
        self.assertEqual(len(self.rows(FashionWardrobeModel)), 0)
        self.assertEqual(len(self.rows(FashionMangoTransactionModel)), 0)

    def test_admin_adjust_then_purchase_deducts_exactly_once(self):
        self.assertEqual(self.adjust(USER_A, 100).status_code, 200)
        self.assertEqual(self.wallet().json()["balance"], 100)

        resp = self.purchase(CID_PAID)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["purchased"])
        self.assertFalse(body["free"])
        self.assertEqual(body["priceMango"], 30)
        self.assertEqual(body["balance"], 70)
        self.assertTrue(body["transactionId"])
        self.assertEqual(self.wallet().json()["balance"], 70)
        self.assertEqual(self.wallet().json()["totalSpent"], 30)
        self.assertEqual(self.wallet().json()["totalEarned"], 100)
        self.assertEqual(len(self.rows(FashionWardrobeModel)), 1)
        self.assertEqual(len(self.rows(FashionOwnershipModel)), 1)

    def test_repeat_purchase_does_not_charge_twice(self):
        self.adjust(USER_A, 100)
        self.purchase(CID_PAID)
        resp = self.purchase(CID_PAID)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["alreadyOwned"])
        self.assertEqual(resp.json()["balance"], 70)
        self.assertEqual(len(self.rows(FashionMangoTransactionModel)), 2)  # 发放 + 购买各一条
        self.assertEqual(len(self.rows(FashionOwnershipModel)), 1)

    def test_client_request_id_is_idempotent(self):
        self.adjust(USER_A, 100)
        first = self.purchase(CID_PAID, key="order-key-001")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["purchased"])
        second = self.purchase(CID_PAID, key="order-key-001")
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["alreadyOwned"])
        self.assertEqual(second.json()["balance"], 70)
        self.assertEqual(len(self.rows(FashionMangoTransactionModel)), 2)

    def test_client_request_id_reused_for_other_item_is_409(self):
        self.adjust(USER_A, 200)
        self.purchase(CID_PAID, key="same-key")
        resp = self.purchase(CID_PAID_2, key="same-key")
        self.assertCode(resp, 409, "idempotency_key_conflict")
        self.assertEqual(self.wallet().json()["balance"], 170)  # 只扣了第一件

    def test_client_request_id_accepts_legacy_alias(self):
        """同一幂等键 + 同一件时装的「简单 ID 别名」不应误判 409。"""
        self.adjust(USER_A, 100)
        first = self.purchase(CID_PAID, key="alias-key")
        self.assertEqual(first.status_code, 200, first.text)
        second = self.purchase("cape_dragon", key="alias-key")
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["alreadyOwned"])
        self.assertEqual(second.json()["balance"], 70)
        self.assertEqual(len(self.rows(FashionOwnershipModel)), 1)

    def test_purchase_not_purchasable(self):
        self.adjust(USER_A, 100)
        resp = self.purchase(CID_LOCKED)
        self.assertCode(resp, 409, "not_purchasable")
        self.assertEqual(self.wallet().json()["balance"], 100)

    def test_purchase_unknown_cosmetic(self):
        resp = self.purchase("pack1:does_not_exist")
        self.assertCode(resp, 404, "cosmetic_not_found")

    def test_purchase_empty_cosmetic_id(self):
        resp = self.purchase("")
        self.assertCode(resp, 400, "invalid_cosmetic_id")

    def test_purchase_invalid_client_request_id(self):
        resp = self.purchase(CID_FREE, key="has space & illegal")
        self.assertCode(resp, 400, "invalid_client_request_id")

    # ── 防绕过 ──────────────────────────────────────────────────────────
    def test_wardrobe_add_blocked_for_unowned_paid_item(self):
        self.as_user()
        resp = self.client.post("/api/fashion/wardrobe", json={"cosmeticId": CID_PAID})
        self.assertCode(resp, 403, "purchase_required")
        self.assertIn("30 芒果块", resp.json()["detail"]["message"])
        self.assertEqual(len(self.rows(FashionWardrobeModel)), 0)

    def test_wardrobe_add_allowed_after_purchase(self):
        self.adjust(USER_A, 100)
        self.purchase(CID_PAID)
        # 先从衣柜删掉，再点「加入衣柜」应免费补回（权利还在 ownership）
        self.as_user()
        self.client.delete(f"/api/fashion/wardrobe/{CID_PAID}")
        resp = self.client.post("/api/fashion/wardrobe", json={"cosmeticId": CID_PAID})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(self.rows(FashionWardrobeModel)), 1)
        self.assertEqual(self.wallet().json()["balance"], 70)  # 没有二次扣款

    def test_loadout_skips_unowned_paid_item(self):
        """PUT /loadout 对未拥有的付费条目 skip + warning，不整条失败。"""
        db = self.Session()
        try:
            identity_id = str(uuid.uuid4())
            db.add(FashionIdentityModel(
                id=identity_id,
                mc_uuid="11111111-1111-1111-1111-111111111111",
                mc_name="Steve",
                auth_type="mojang", auth_server="",
                user_id=USER_A,
                created_at=datetime.now(timezone.utc),
            ))
            db.commit()
        finally:
            db.close()
        self.app.dependency_overrides[fashion_router.get_principal] = (
            lambda: fashion_router.FashionPrincipal(
                identity_id=identity_id, user_id=USER_A, via="mod"
            )
        )
        self.as_user()
        resp = self.client.put("/api/fashion/loadout", json={
            "items": [
                {"slot": "cape", "cosmeticId": CID_PAID, "scale": 1.0},
                {"slot": "hat", "cosmeticId": CID_FREE, "scale": 1.0},
            ]
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        warnings = resp.json()["warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["code"], "not_owned")
        self.assertEqual(warnings[0]["cosmeticId"], CID_PAID)
        self.assertEqual(len(self.rows(FashionLoadoutModel)), 1)  # 免费的仍写入

    def test_unlinked_mod_token_loadout_skips_ownership_check(self):
        """未绑定网站账号的 mod token（user_id=None）不做 not_owned 校验（文档 §14）。"""
        db = self.Session()
        try:
            identity_id = str(uuid.uuid4())
            db.add(FashionIdentityModel(
                id=identity_id,
                mc_uuid="33333333-3333-3333-3333-333333333333",
                mc_name="Alex",
                auth_type="mojang", auth_server="",
                user_id=None,
                created_at=datetime.now(timezone.utc),
            ))
            db.commit()
        finally:
            db.close()
        self.app.dependency_overrides[fashion_router.get_principal] = (
            lambda: fashion_router.FashionPrincipal(
                identity_id=identity_id, user_id=None, via="mod"
            )
        )
        self.as_user()
        resp = self.client.put("/api/fashion/loadout", json={
            "items": [{"slot": "cape", "cosmeticId": CID_PAID, "scale": 1.0}],
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["warnings"], [])
        self.assertEqual(len(self.rows(FashionLoadoutModel)), 1)

    # ── 管理端 ──────────────────────────────────────────────────────────
    def test_admin_adjust_negative_cannot_overdraw(self):
        self.adjust(USER_A, 20)
        resp = self.adjust(USER_A, -50)
        self.assertCode(resp, 400, "insufficient_mango")
        self.assertEqual(self.wallet().json()["balance"], 20)

    def test_admin_adjust_negative_ok(self):
        self.adjust(USER_A, 100)
        resp = self.adjust(USER_A, -40)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["balance"], 60)
        self.assertEqual(resp.json()["delta"], -40)
        self.assertEqual(self.wallet().json()["balance"], 60)

    def test_admin_adjust_invalid_delta(self):
        for delta in (0, 2_000_000):
            resp = self.adjust(USER_A, delta)
            self.assertEqual(resp.status_code, 400, resp.text)
            self.assertEqual(resp.json()["detail"]["code"], "invalid_amount")

    def test_admin_adjust_unknown_user(self):
        resp = self.adjust("no-such-user", 10)
        self.assertCode(resp, 404, "user_not_found")

    def test_admin_wallet_users_search(self):
        self.adjust(USER_A, 77)
        self.as_admin()
        resp = self.client.get("/api/fashion/admin/wallet/users", params={"keyword": "alice"})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["items"][0]["userId"], USER_A)
        self.assertEqual(body["items"][0]["balance"], 77)
        all_resp = self.client.get("/api/fashion/admin/wallet/users")
        self.assertEqual(all_resp.json()["total"], 3)

    def test_admin_grant_cosmetic(self):
        self.as_admin()
        resp = self.client.post("/api/fashion/admin/wallet/grant-cosmetic", json={
            "userId": USER_A, "cosmeticId": CID_PAID, "note": "活动赠送",
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["granted"])
        self.assertEqual(len(self.rows(FashionOwnershipModel)), 1)
        self.assertEqual(len(self.rows(FashionWardrobeModel)), 1)
        self.assertEqual(self.wallet().json()["balance"], 0)  # 不扣币
        # 再发放一次是幂等的
        again = self.client.post("/api/fashion/admin/wallet/grant-cosmetic", json={
            "userId": USER_A, "cosmeticId": CID_PAID,
        })
        self.assertFalse(again.json()["granted"])
        self.assertEqual(len(self.rows(FashionOwnershipModel)), 1)

    def test_admin_catalog_price_update(self):
        self.as_admin()
        resp = self.client.put(f"/api/fashion/admin/catalog/{CID_FREE}", json={
            "priceMango": 42, "purchasable": True,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["priceMango"], 42)
        self.assertEqual(resp.json()["cosmeticId"], CID_FREE)
        # 商店立刻能看到新价；旧的免费行为随价格变化
        self.as_user()
        items = self.client.get("/api/fashion/store/items").json()["items"]
        self.assertEqual(next(i for i in items if i["cosmeticId"] == CID_FREE)["priceMango"], 42)
        blocked = self.client.post("/api/fashion/wardrobe", json={"cosmeticId": CID_FREE})
        self.assertCode(blocked, 403, "purchase_required")

    def test_admin_catalog_invalid_price(self):
        self.as_admin()
        resp = self.client.put(f"/api/fashion/admin/catalog/{CID_FREE}", json={
            "priceMango": -1,
        })
        self.assertCode(resp, 400, "invalid_price")
        too_big = self.client.put(f"/api/fashion/admin/catalog/{CID_FREE}", json={
            "priceMango": 9_999_999,
        })
        self.assertCode(too_big, 400, "invalid_price")

    # ── 流水 / 订单 ─────────────────────────────────────────────────────
    def test_transactions_and_orders(self):
        self.adjust(USER_A, 100, note="活动奖励")
        self.purchase(CID_PAID)
        self.as_user()
        tx = self.client.get("/api/fashion/wallet/transactions").json()
        self.assertEqual(tx["total"], 2)
        self.assertEqual(tx["items"][0]["type"], "purchase")
        self.assertEqual(tx["items"][0]["delta"], -30)
        self.assertEqual(tx["items"][0]["balanceAfter"], 70)
        self.assertEqual(tx["items"][0]["amountAbs"], 30)
        self.assertEqual(tx["items"][0]["cosmeticId"], CID_PAID)
        self.assertEqual(tx["items"][1]["type"], "admin_adjust")
        self.assertEqual(tx["items"][1]["delta"], 100)
        self.assertEqual(tx["items"][1]["note"], "活动奖励")

        orders = self.client.get("/api/fashion/store/orders").json()
        self.assertEqual(orders["total"], 1)
        self.assertEqual(orders["items"][0]["cosmeticId"], CID_PAID)
        self.assertEqual(orders["items"][0]["pricePaid"], 30)
        self.assertEqual(orders["items"][0]["source"], "purchase")
        self.assertFalse(orders["items"][0]["missing"])

    def test_pagination_limits(self):
        self.as_user()
        # limit 超上限由 service 收敛到 100；offset 负值由 FastAPI Query(ge=0) 挡成 422
        # （前端 fashionErrorMessage 对 422 已有中文兜底）
        resp = self.client.get("/api/fashion/wallet/transactions", params={"limit": 999})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["limit"], 100)
        self.assertEqual(resp.json()["offset"], 0)
        bad = self.client.get("/api/fashion/wallet/transactions", params={"offset": -5})
        self.assertEqual(bad.status_code, 422)

    # ── 历史回填 ────────────────────────────────────────────────────────
    def test_legacy_wardrobe_backfill(self):
        """上线前收藏的付费时装自动继承所有权，不追溯收费、不锁住玩家。"""
        db = self.Session()
        try:
            db.add(FashionWardrobeModel(
                id=str(uuid.uuid4()), user_id=USER_A,
                cosmetic_id=CID_PAID, added_at=datetime.now(timezone.utc),
            ))
            db.commit()
        finally:
            db.close()
        self.as_user()
        items = self.client.get("/api/fashion/store/items").json()["items"]
        legacy = next(i for i in items if i["cosmeticId"] == CID_PAID)
        self.assertTrue(legacy["owned"])
        self.assertTrue(legacy["inWardrobe"])
        self.assertEqual(self.wallet().json()["balance"], 0)
        # 从衣柜删掉后仍能免费重新加入（权利已继承）
        self.client.delete(f"/api/fashion/wardrobe/{CID_PAID}")
        resp = self.client.post("/api/fashion/wardrobe", json={"cosmeticId": CID_PAID})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.wallet().json()["balance"], 0)

    # ── 限流 ────────────────────────────────────────────────────────────
    def test_purchase_rate_limit(self):
        self.adjust(USER_A, 1000)
        self.as_user()
        # 前 20 次放行（免费领取，不扣款），第 21 次 429
        for i in range(svc.PURCHASE_RATE_LIMIT):
            resp = self.client.post("/api/fashion/store/purchase", json={
                "cosmeticId": CID_FREE,
            })
            self.assertEqual(resp.status_code, 200, resp.text)
        resp = self.client.post("/api/fashion/store/purchase", json={
            "cosmeticId": CID_FREE,
        })
        self.assertCode(resp, 429, "purchase_rate_limited")
        self.assertIn("Retry-After", resp.headers)

    # ── 审计 P0 回归：别名碰撞不得绕过计费 ──────────────────────────────
    def _add_alias_collision_catalog(self):
        """构造「便宜简单 ID + 同名 localId 的昂贵全局 ID」这组歧义目录。"""
        db = self.Session()
        try:
            db.add(_catalog("crown", "便宜皇冠", price=1, pack="", category="hat"))
            db.add(_catalog("packX:crown", "贵皇冠 X", price=500, pack="packX", category="hat"))
            db.add(_catalog("packY:crown", "贵皇冠 Y", price=500, pack="packY", category="hat"))
            db.commit()
        finally:
            db.close()
        svc.invalidate_caches()

    def test_p0_cheap_simple_id_does_not_unlock_expensive_global_ids(self):
        """买 1 芒果块的 `crown` 后，`packX:crown`/`packY:crown` 仍必须付费。"""
        self._add_alias_collision_catalog()
        self.adjust(USER_A, 2000)

        cheap = self.purchase("crown")
        self.assertEqual(cheap.status_code, 200, cheap.text)
        self.assertEqual(cheap.json()["priceMango"], 1)
        self.assertEqual(cheap.json()["balance"], 1999)

        for cid in ("packX:crown", "packY:crown"):
            resp = self.purchase(cid)
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertTrue(resp.json()["purchased"], f"{cid} 被误判成已拥有")
            self.assertFalse(resp.json()["alreadyOwned"])
            self.assertEqual(resp.json()["priceMango"], 500)
        self.assertEqual(self.wallet().json()["balance"], 999)  # 2000-1-500-500

        # 商店页也不能把贵的标成已拥有
        self.as_user()
        by_id = {
            i["cosmeticId"]: i
            for i in self.client.get("/api/fashion/store/items").json()["items"]
        }
        self.assertTrue(by_id["crown"]["owned"])
        for cid in ("packX:crown", "packY:crown"):
            self.assertTrue(by_id[cid]["owned"])  # 刚买过
            self.assertEqual(by_id[cid]["priceMango"], 500)

    def test_p0_legacy_simple_wardrobe_row_does_not_unlock_all_packs(self):
        """历史简单 ID 衣柜行不能把全部同名 globalId 付费条目判成已拥有。"""
        db = self.Session()
        try:
            db.add(FashionWardrobeModel(
                id=str(uuid.uuid4()), user_id=USER_A,
                cosmetic_id="crown", added_at=datetime.now(timezone.utc),
            ))
            db.commit()
        finally:
            db.close()
        self._add_alias_collision_catalog()
        self.adjust(USER_A, 2000)

        # 简单 ID 目录条目存在时，`crown` 精确命中它（不是全局条目的别名）
        self.as_user()
        by_id = {
            i["cosmeticId"]: i
            for i in self.client.get("/api/fashion/store/items").json()["items"]
        }
        self.assertTrue(by_id["crown"]["owned"])
        unlocked = [c for c in ("packX:crown", "packY:crown") if by_id[c]["owned"]]
        self.assertLessEqual(len(unlocked), 1, "别名回填最多只能继承一件，不能全部解锁")
        for cid in ("packX:crown", "packY:crown"):
            if not by_id[cid]["owned"]:
                resp = self.purchase(cid)
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertTrue(resp.json()["purchased"])

    # ── 审计 P1：幂等键按用户隔离 ───────────────────────────────────────
    def test_client_request_id_is_scoped_per_user(self):
        self.adjust(USER_A, 100)
        self.adjust(USER_B, 100)
        first = self.purchase(CID_FREE, key="shared-key", uid=USER_A)
        second = self.purchase(CID_FREE, key="shared-key", uid=USER_B)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(first.json()["purchased"])
        self.assertTrue(second.json()["purchased"])
        self.assertEqual(self.wallet(USER_A).json()["balance"], 100)
        self.assertEqual(self.wallet(USER_B).json()["balance"], 100)

    def test_paid_client_request_id_is_scoped_per_user(self):
        self.adjust(USER_A, 100)
        self.adjust(USER_B, 100)
        a = self.purchase(CID_PAID, key="paid-shared", uid=USER_A)
        b = self.purchase(CID_PAID, key="paid-shared", uid=USER_B)
        self.assertEqual(a.status_code, 200, a.text)
        self.assertEqual(b.status_code, 200, b.text)
        self.assertEqual(self.wallet(USER_A).json()["balance"], 70)
        self.assertEqual(self.wallet(USER_B).json()["balance"], 70)

    # ── 审计 P2：幂等响应语义与 purchasable 顺序 ─────────────────────────
    def test_idempotent_repeat_keeps_free_and_price_consistent(self):
        first_free = self.purchase(CID_FREE)
        repeat_free = self.purchase(CID_FREE)
        self.assertEqual(first_free.json()["free"], True)
        self.assertEqual(repeat_free.json()["free"], True)
        self.assertEqual(repeat_free.json()["priceMango"], 0)

        self.adjust(USER_A, 100)
        first = self.purchase(CID_PAID, key="receipt-key")
        repeat = self.purchase(CID_PAID, key="receipt-key")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(repeat.status_code, 200, repeat.text)
        self.assertFalse(repeat.json()["free"])
        self.assertEqual(repeat.json()["priceMango"], 30)
        self.assertEqual(repeat.json()["balance"], first.json()["balance"])
        self.assertEqual(repeat.json()["transactionId"], first.json()["transactionId"])
        self.assertTrue(repeat.json()["transactionId"])

    def test_already_owned_stays_idempotent_when_marked_not_purchasable(self):
        self.adjust(USER_A, 100)
        self.assertEqual(self.purchase(CID_PAID).status_code, 200)
        self.as_admin()
        disabled = self.client.put(
            f"/api/fashion/admin/catalog/{CID_PAID}", json={"purchasable": False}
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.as_user()
        self.client.delete(f"/api/fashion/wardrobe/{CID_PAID}")
        resp = self.purchase(CID_PAID)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["alreadyOwned"])
        self.assertEqual(resp.json()["balance"], 70)  # 没有二次扣款
        self.assertEqual(len(self.rows(FashionWardrobeModel)), 1)  # 衣柜被补回

    def test_admin_endpoints_reject_regular_user_with_real_dependency(self):
        """去掉 require_site_admin 的 override，用真实权限依赖守回归线。"""
        saved = self.app.dependency_overrides.pop(deps.require_site_admin, None)
        try:
            self.as_user()  # 普通 user 角色
            cases = [
                ("get", "/api/fashion/admin/wallet/users", None),
                ("post", "/api/fashion/admin/wallet/adjust",
                 {"userId": USER_A, "delta": 10}),
                ("post", "/api/fashion/admin/wallet/grant-cosmetic",
                 {"userId": USER_A, "cosmeticId": CID_FREE}),
                ("get", "/api/fashion/admin/catalog", None),
            ]
            for method, path, body in cases:
                resp = getattr(self.client, method)(path, json=body) if body \
                    else getattr(self.client, method)(path)
                self.assertEqual(resp.status_code, 403, f"{method} {path}: {resp.text}")
        finally:
            if saved is not None:
                self.app.dependency_overrides[deps.require_site_admin] = saved

    def test_keyword_length_is_bounded(self):
        self.as_admin()
        resp = self.client.get(
            "/api/fashion/admin/wallet/users", params={"keyword": "x" * 5000}
        )
        self.assertEqual(resp.status_code, 422)

    def test_boolean_price_is_rejected(self):
        self.as_admin()
        resp = self.client.put(
            f"/api/fashion/admin/catalog/{CID_FREE}", json={"priceMango": True}
        )
        self.assertEqual(resp.status_code, 422)


# ── 并发扣款（真实文件型 SQLite + WAL，双线程）────────────────────────────

class MangoConcurrencyCase(unittest.TestCase):
    """验证「条件 UPDATE + 唯一约束」在真并发下不超扣、不重复扣。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        env = {
            "VMT_SERVER__SECRET_KEY": _TEST_SECRET,
            "VMT_FASHION__ASSET_DIR": os.path.join(self._tmp.name, "assets"),
            "VMT_FASHION__CATALOG_CACHE_TTL_SECONDS": "0",
            "VMT_FASHION__RESOLVE_CACHE_TTL_SECONDS": "0",
        }
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        from vmtools_next.config import get_config

        get_config.cache_clear()
        svc.invalidate_caches()
        fashion_router._PURCHASE_HITS.clear()
        self._saved_columns = _restore_mapped_columns()

        db_path = os.path.join(self._tmp.name, "concurrent.db")
        self.engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False}, future=True
        )

        @event.listens_for(self.engine, "connect")
        def _pragma(dbapi_conn, _rec):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        db = self.Session()
        try:
            db.add_all([
                UserModel(id=USER_A, game_id="alice", display_name="小明",
                          password_hash="x", role="user", status="approved"),
                _catalog(CID_PAID, "龙纹披风", price=30),
                _catalog(CID_PAID_2, "凤凰之翼", price=30, category="wings"),
                FashionWalletModel(user_id=USER_A, balance=30, total_earned=30,
                                   total_spent=0, updated_at=datetime.now(timezone.utc)),
            ])
            db.commit()
        finally:
            db.close()

        self.app = FastAPI()
        self.app.include_router(fashion_router.router)

        def _get_db():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

        self.app.dependency_overrides[deps.get_db] = _get_db
        self.app.dependency_overrides[deps.get_current_user] = lambda: _User(USER_A)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        _put_columns_back(self._saved_columns)
        self._env.stop()
        from vmtools_next.config import get_config

        get_config.cache_clear()
        svc.invalidate_caches()
        fashion_router._PURCHASE_HITS.clear()
        self.engine.dispose()
        self._tmp.cleanup()

    def _balance(self):
        db = self.Session()
        try:
            return int(getattr(db.query(FashionWalletModel).filter_by(
                user_id=USER_A).first(), "balance", 0) or 0)
        finally:
            db.close()

    def test_concurrent_same_item_is_charged_once(self):
        import threading

        barrier = threading.Barrier(2)
        results: dict[int, tuple] = {}

        def worker(i):
            barrier.wait()
            resp = self.client.post("/api/fashion/store/purchase", json={
                "cosmeticId": CID_PAID,
            })
            results[i] = (resp.status_code, resp.json())

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(code == 200 for code, _ in results.values()),
                        str(results))
        purchased = sum(1 for _, body in results.values() if body.get("purchased"))
        self.assertEqual(purchased, 1, f"只应扣一次，实际 {results}")
        self.assertEqual(self._balance(), 0)

    def test_concurrent_different_items_never_overdraw(self):
        import threading

        barrier = threading.Barrier(2)
        results: dict[str, tuple] = {}

        def worker(cid):
            barrier.wait()
            resp = self.client.post("/api/fashion/store/purchase", json={
                "cosmeticId": cid,
            })
            results[cid] = (resp.status_code, resp.json())

        threads = [threading.Thread(target=worker, args=(cid,))
                   for cid in (CID_PAID, CID_PAID_2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(len(results), 2)
        ok = [cid for cid, (code, _) in results.items() if code == 200]
        insufficient = [
            cid for cid, (code, body) in results.items()
            if code == 402 and body.get("detail", {}).get("code") == "insufficient_mango"
        ]
        self.assertEqual(len(ok), 1, str(results))
        self.assertEqual(len(insufficient), 1, str(results))
        self.assertGreaterEqual(self._balance(), 0)
        self.assertEqual(self._balance(), 0)
        # 并发输家的「还差多少」不能是 0（审计 P2-7）
        short_msg = results[insufficient[0]][1]["detail"]["message"]
        self.assertIn("还差 30 芒果块", short_msg)


# ── Alembic 迁移（幂等 / 回填 / 逆序）────────────────────────────────────

class MangoMigrationCase(unittest.TestCase):
    """用真 SQLite + 真 alembic Operations 跑 upgrade/downgrade。

    验证的是调研里点名的坑：``create_all`` 不给老表加列，所以迁移必须真的
    ``add_column``；以及「上线前衣柜收藏自动继承所有权」的 SQL 回填。
    """

    MIGRATION = (
        Path(__file__).resolve().parents[1]
        / "alembic" / "versions" / "20261105_fashion_mango.py"
    )

    def _load_with_op(self, op_obj):
        import sys

        spec = importlib.util.spec_from_file_location("_mango_migration_live", self.MIGRATION)
        module = importlib.util.module_from_spec(spec)
        real = sys.modules.get("alembic")
        fake = types.ModuleType("alembic")
        fake.op = op_obj
        sys.modules["alembic"] = fake
        try:
            spec.loader.exec_module(module)
        finally:
            if real is not None:
                sys.modules["alembic"] = real
            else:
                sys.modules.pop("alembic", None)
        return module

    def test_revision_chain(self):
        module = self._load_with_op(types.SimpleNamespace())
        self.assertEqual(module.revision, "20261105_fashion_mango")
        self.assertEqual(module.down_revision, "20261103_fashion_asset_idx")

    def test_upgrade_downgrade_on_legacy_db(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        conn = engine.connect()
        try:
            # ── 构造「v1.1 之前」的老库 ──
            conn.execute(text(
                "CREATE TABLE users (id VARCHAR PRIMARY KEY, game_id VARCHAR, "
                "display_name VARCHAR, role VARCHAR, status VARCHAR, password_hash VARCHAR)"
            ))
            conn.execute(text(
                "CREATE TABLE fashion_catalog (cosmetic_id VARCHAR PRIMARY KEY, "
                "pack_id VARCHAR, name VARCHAR, category VARCHAR, asset_key VARCHAR, "
                "sha256 VARCHAR, bytes INTEGER, enabled BOOLEAN, sort_order INTEGER, created_at DATETIME)"
            ))
            conn.execute(text(
                "CREATE TABLE fashion_wardrobe (id VARCHAR PRIMARY KEY, user_id VARCHAR, "
                "cosmetic_id VARCHAR, added_at DATETIME)"
            ))
            conn.execute(text(
                "INSERT INTO users (id, game_id, role, status, password_hash) "
                "VALUES ('u1', 'alice', 'user', 'approved', 'x')"
            ))
            conn.execute(text(
                "INSERT INTO fashion_catalog (cosmetic_id, name, enabled) "
                "VALUES ('pack1:cape', '披风', 1)"
            ))
            conn.execute(text(
                "INSERT INTO fashion_wardrobe (id, user_id, cosmetic_id, added_at) "
                "VALUES ('w1', 'u1', 'pack1:cape', '2026-01-01 00:00:00')"
            ))
            conn.commit()

            ops = Operations(MigrationContext.configure(conn))
            module = self._load_with_op(ops)

            # ── upgrade：补列 + 建表 + 回填 ──
            module.upgrade()
            conn.commit()

            catalog_columns = {c["name"] for c in inspect(conn).get_columns("fashion_catalog")}
            self.assertIn("price_mango", catalog_columns)
            self.assertIn("purchasable", catalog_columns)

            tables = set(inspect(conn).get_table_names())
            for table in ("fashion_wallet", "fashion_ownership", "fashion_mango_transaction"):
                self.assertIn(table, tables)

            ownership = conn.execute(text(
                "SELECT user_id, cosmetic_id, price_paid, source FROM fashion_ownership"
            )).fetchall()
            self.assertEqual(len(ownership), 1)
            self.assertEqual(ownership[0][0], "u1")
            self.assertEqual(ownership[0][1], "pack1:cape")
            self.assertEqual(ownership[0][2], 0)  # 不追溯收费
            self.assertEqual(ownership[0][3], "legacy_wardrobe")

            wallets = conn.execute(text(
                "SELECT user_id, balance FROM fashion_wallet"
            )).fetchall()
            self.assertEqual(len(wallets), 1)
            self.assertEqual(wallets[0][0], "u1")
            self.assertEqual(wallets[0][1], 0)  # 不凭空发币

            # 再跑一次必须幂等（不重复回填、不炸）
            module.upgrade()
            conn.commit()
            self.assertEqual(
                conn.execute(text("SELECT COUNT(*) FROM fashion_ownership")).scalar(), 1
            )
            self.assertEqual(
                conn.execute(text("SELECT COUNT(*) FROM fashion_wallet")).scalar(), 1
            )

            # ── downgrade：逆序删表 + 删列 ──
            module.downgrade()
            conn.commit()
            tables_after = set(inspect(conn).get_table_names())
            for table in ("fashion_wallet", "fashion_ownership", "fashion_mango_transaction"):
                self.assertNotIn(table, tables_after)
            cols_after = {c["name"] for c in inspect(conn).get_columns("fashion_catalog")}
            self.assertNotIn("price_mango", cols_after)
            self.assertNotIn("purchasable", cols_after)
        finally:
            conn.close()
            engine.dispose()

    def test_migration_source_contains_expected_operations(self):
        """静态兜底：迁移必须显式 add_column，不能依赖 create_all 补列。"""
        source = self.MIGRATION.read_text(encoding="utf-8")
        for needle in (
            "op.add_column", "price_mango", "purchasable",
            "fashion_wallet", "fashion_ownership", "fashion_mango_transaction",
            "legacy_wardrobe", "20261103_fashion_asset_idx", "batch_alter_table",
            # PostgreSQL 兼容：BOOLEAN 默认值必须方言无关、金额列必须 64 位
            "sa.true()", "sa.BigInteger()",
        ):
            self.assertIn(needle, source, f"迁移缺少 {needle}")
        # 不能退回 PG 会拒绝的 `DEFAULT 1` / 32 位 INTEGER
        self.assertNotIn('server_default=sa.text("1")', source)

    def test_lightweight_migration_adds_columns_without_alembic(self):
        """开发机直接跑 init_db()（没跑 alembic）时，兜底 ALTER 必须补上两列。"""
        from vmtools_next.data import db as db_module

        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        try:
            conn = engine.connect()
            conn.execute(text(
                "CREATE TABLE fashion_catalog (cosmetic_id VARCHAR PRIMARY KEY, "
                "pack_id VARCHAR, name VARCHAR, category VARCHAR, asset_key VARCHAR, "
                "sha256 VARCHAR, bytes INTEGER, enabled BOOLEAN, sort_order INTEGER, created_at DATETIME)"
            ))
            conn.commit()
            conn.close()

            # _run_lightweight_migrations 的 PRAGMA 块只在 SQLite URL 下执行；
            # 真实 init_db() 里 get_engine() 会设好该变量，这里直接调函数要补上。
            with mock.patch.object(db_module, "_DATABASE_URL", "sqlite:///:memory:"):
                db_module._run_lightweight_migrations(engine)
            with engine.connect() as check:
                columns = {
                    row[1] for row in check.execute(text("PRAGMA table_info(fashion_catalog)"))
                }
            self.assertIn("price_mango", columns)
            self.assertIn("purchasable", columns)

            # 再跑一次必须幂等（不会重复 ADD COLUMN 报错）
            with mock.patch.object(db_module, "_DATABASE_URL", "sqlite:///:memory:"):
                db_module._run_lightweight_migrations(engine)
            with engine.connect() as check:
                columns2 = {
                    row[1] for row in check.execute(text("PRAGMA table_info(fashion_catalog)"))
                }
            self.assertEqual(columns, columns2)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
