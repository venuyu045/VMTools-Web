"""时装接口的 **HTTP 级**集成测试（审计 E1，P1）。

为什么要单开一个文件
--------------------
其它时装测试都是「直接调用路由函数 + ``FakeSession``」（``test_fashion_api.py``），
中间件、依赖注入、``require_site_admin`` 的 401/403、``FileResponse`` 的真实响应头、
SQLAlchemy 的唯一约束 / SQLite naive datetime **全都没被覆盖**。本文件用
``fastapi.testclient.TestClient`` 跑真实 HTTP 语义（进程内，不联网），数据库是
``tmpdir`` 下的 SQLite，资产目录也是临时的，配置全部走 ``VMT_*`` 环境变量。

覆盖（对应审计 §2 ⑤ 的 E1/E3/E4 与 E5 建议用例）：

- 管理端未授权：无 token → 401，非 site_admin → 403，site_admin → 200
- ``GET /packs`` 200 形状（``packs`` 数组 + 字段名/``publishedAt`` 毫秒/无签名）
- ``HEAD /packs/{id}/{v}/download`` 头（``Content-Length`` == 清单 ``bytes``、
  ``Content-Type: application/zip``、**无 Location**）
- ``PUT /loadout`` 的部分成功语义（``warnings``）与全局 ID 落库
- 配私钥 → ``/packs`` 带 ``signature`` + ``signingKeyId``；没装 ``cryptography``
  → 仍 200、只是不带签名（绝不 500）

注意：``test_fashion_api.py`` / ``test_fashion_loadout.py`` 会把
``FashionIdentityModel.mc_uuid``、``FashionLoadoutModel.identity_id`` 换成 ``_Expr``
桩（给 FakeSession 用），那是**全局类属性打桩**。真实 SQLite 查询前必须换回
SQLAlchemy 的 InstrumentedAttribute，跑完再换回去，否则会互相污染。
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import unittest
import uuid as uuid_lib
import zipfile
from datetime import datetime, timezone
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _stub_env import install, quiet_stub_noise  # noqa: E402

install()
quiet_stub_noise()

from test_fashion_signature import (  # noqa: E402  （复用「没装 cryptography」的模拟）
    FIXED_SEED,
    HAVE_CRYPTOGRAPHY,
    cryptography_missing,
    seed_b64,
)

from vmtools_next.api import deps  # noqa: E402
from vmtools_next.api.routers import fashion as fashion_router_module  # noqa: E402
from vmtools_next.core import fashion_auth  # noqa: E402
from vmtools_next.core import fashion_service as svc  # noqa: E402
from vmtools_next.data.db import Base  # noqa: E402
from vmtools_next.data.models.fashion import (  # noqa: E402
    FashionIdentityModel,
    FashionLoadoutModel,
)

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    _HAS_HTTP_STACK = hasattr(FastAPI, "include_router") and hasattr(
        TestClient, "get"
    )
except Exception:  # pragma: no cover - 离线 stub 环境
    _HAS_HTTP_STACK = False

# 配置里 secret_key 有长度/占位值校验，给一个明显是测试用的 64 位 hex
TEST_SECRET_KEY = "0123456789abcdef" * 4
MC_UUID = "11111111-1111-1111-1111-111111111111"
PACK_ID = "example"
PACK_VERSION = "1.0.0"
GLOBAL_COSMETIC_ID = "pack1:cape_dragon"


def make_pack_zip(pack_id: str = PACK_ID) -> bytes:
    """一个能通过 ``validate_pack_zip`` 的最小合法时装包。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "pack.json",
            json.dumps({"id": pack_id, "name": "示例包", "version": PACK_VERSION}),
        )
        archive.writestr("assets/backboard.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    return buf.getvalue()


def _restore_mapped_columns():
    """把被其它测试模块替换成 ``_Expr`` 的模型列换回真实映射属性。

    ``FashionIdentityModel.mc_uuid`` / ``FashionLoadoutModel.identity_id`` 在
    ``test_fashion_api.py`` / ``test_fashion_loadout.py`` 的模块级被换成了 ``_Expr``
    （stub 环境里 ``Column.__eq__`` 会丢值，FakeSession 需要它）。那是**全局类属性**
    打桩，会污染本文件的真实 SQLite 查询（``filter_by(identity_id=…)`` 直接抛
    ``ArgumentError``）。

    原映射属性没丢：SQLAlchemy 的 ClassManager 里还留着 InstrumentedAttribute
    （``prop.class_attribute`` 是动态 ``getattr``，此时已经被桩覆盖，不能用）。
    """
    saved = []
    for model, name in (
        (FashionIdentityModel, "mc_uuid"),
        (FashionLoadoutModel, "identity_id"),
    ):
        saved.append((model, name, getattr(model, name, None)))
        setattr(model, name, model._sa_class_manager[name])
    return saved


def _put_columns_back(saved) -> None:
    for model, name, value in saved:
        setattr(model, name, value)


@unittest.skipUnless(_HAS_HTTP_STACK, "需要真实 FastAPI + httpx + SQLAlchemy")
class _HttpBase(unittest.TestCase):
    # 子类覆盖成 base64 私钥时，环境里就带上签名配置
    PACK_SIGNING_KEY_B64 = ""
    PACK_SIGNING_KEY_ID = "http-test-key"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.assets_dir = os.path.join(self.tmpdir, "assets")
        env = {
            "VMT_SERVER__SECRET_KEY": TEST_SECRET_KEY,
            "VMT_FASHION__ASSET_DIR": self.assets_dir,
            # 关掉进程内缓存：每个用例都改库，缓存会让断言互相干扰
            "VMT_FASHION__CATALOG_CACHE_TTL_SECONDS": "0",
            "VMT_FASHION__RESOLVE_CACHE_TTL_SECONDS": "0",
        }
        if self.PACK_SIGNING_KEY_B64:
            env["VMT_FASHION__PACK_SIGNING_KEY_B64"] = self.PACK_SIGNING_KEY_B64
            env["VMT_FASHION__PACK_SIGNING_KEY_ID"] = self.PACK_SIGNING_KEY_ID
        self._env_patcher = mock.patch.dict(os.environ, env)
        self._env_patcher.start()

        from vmtools_next.config import get_config

        get_config.cache_clear()
        svc.invalidate_caches()
        self._saved_columns = _restore_mapped_columns()

        self.engine = create_engine(
            f"sqlite:///{os.path.join(self.tmpdir, 'test.db')}",
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)

        self.app = FastAPI()
        self.app.include_router(fashion_router_module.router)

        def _get_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[deps.get_db] = _get_db
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        _put_columns_back(self._saved_columns)
        self._env_patcher.stop()
        from vmtools_next.config import get_config

        get_config.cache_clear()
        svc.invalidate_caches()
        self._tmp.cleanup()

    # ── 夹具 ────────────────────────────────────────────────────────────
    def session(self):
        return self.Session()

    def create_identity(self, mc_uuid: str = MC_UUID, mc_name: str = "Steve"):
        """建一条已验证身份并签发 mod token，返回 ``(identity_id, token)``。"""
        now = datetime.now(timezone.utc)
        identity_id = str(uuid_lib.uuid4())
        db = self.session()
        try:
            db.add(
                FashionIdentityModel(
                    id=identity_id,
                    mc_uuid=mc_uuid,
                    mc_name=mc_name,
                    auth_type="mojang",
                    auth_server="",
                    created_at=now,
                    verified_at=now,
                    last_seen_at=now,
                )
            )
            db.commit()
        finally:
            db.close()
        token, _ttl = fashion_auth.issue_token(
            identity_id, mc_uuid=mc_uuid, mc_name=mc_name
        )
        return identity_id, token

    def auth_headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def as_admin(self, role: str = "site_admin"):
        """跳过真实 JWT：直接覆盖 get_current_user（require_site_admin 的依赖）。"""
        user = mock.Mock()
        user.role = role
        user.status = "approved"
        user.id = "user-1"
        self.app.dependency_overrides[deps.get_current_user] = lambda: user
        self.addCleanup(
            self.app.dependency_overrides.pop, deps.get_current_user, None
        )
        return user

    def create_catalog_item(self, payload: dict) -> dict:
        db = self.session()
        try:
            return svc.upsert_catalog_item(db, payload)
        finally:
            db.close()

    def store_pack(self, data: bytes | None = None) -> bytes:
        data = make_pack_zip() if data is None else data
        db = self.session()
        try:
            svc.store_asset(
                db,
                pack_id=PACK_ID,
                version=PACK_VERSION,
                filename=f"{PACK_ID}.zip",
                data=data,
                pack_zip=True,
            )
        finally:
            db.close()
        return data




# ── 管理端鉴权（require_site_admin 的 401 / 403） ──────────────────────

class TestAdminAuthHttp(_HttpBase):
    def test_admin_catalog_without_token_is_401(self):
        response = self.client.get("/api/fashion/admin/catalog")
        self.assertEqual(response.status_code, 401)

    def test_admin_catalog_with_non_admin_is_403(self):
        self.as_admin(role="user")
        response = self.client.get("/api/fashion/admin/catalog")
        self.assertEqual(response.status_code, 403)

    def test_admin_catalog_with_site_admin_is_200(self):
        self.as_admin(role="site_admin")
        response = self.client.get("/api/fashion/admin/catalog")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": []})

    def test_admin_create_catalog_global_id_roundtrip(self):
        self.as_admin()
        response = self.client.post(
            "/api/fashion/admin/catalog",
            json={
                "cosmeticId": GLOBAL_COSMETIC_ID,
                "packId": "pack1",
                "name": "龙翼披风",
                "category": "cape",
                "assetKey": "cape/dragon.png",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["cosmeticId"], GLOBAL_COSMETIC_ID)
        self.assertEqual(body["packId"], "pack1")

    def test_admin_create_catalog_prefix_mismatch_is_400(self):
        """A1/A2 契约：冒号前缀必须等于 packId，否则客户端会去下一个不存在的包。"""
        self.as_admin()
        response = self.client.post(
            "/api/fashion/admin/catalog",
            json={"cosmeticId": "pack2:cape_dragon", "packId": "pack1"},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"]["code"], "invalid_cosmetic_id")

    def test_admin_create_catalog_malformed_global_id_is_400(self):
        self.as_admin()
        for cosmetic_id in ("pack1:", ":cape", "pack1:a:b"):
            with self.subTest(cosmeticId=cosmetic_id):
                response = self.client.post(
                    "/api/fashion/admin/catalog",
                    json={"cosmeticId": cosmetic_id, "packId": "pack1"},
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(
                    response.json()["detail"]["code"], "invalid_cosmetic_id"
                )


# ── /packs 清单（200 形状 + 签名字段） ─────────────────────────────────

class TestPacksManifestHttp(_HttpBase):
    def test_packs_empty_is_200_and_documented_shape(self):
        response = self.client.get("/api/fashion/packs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"packs": []})
        # mod 有 10 分钟本地 TTL，服务端给 5 分钟 max-age
        self.assertIn("max-age=300", response.headers.get("cache-control", ""))

    def test_packs_manifest_fields(self):
        data = self.store_pack()
        response = self.client.get("/api/fashion/packs")
        self.assertEqual(response.status_code, 200)
        packs = response.json()["packs"]
        self.assertEqual(len(packs), 1)
        pack = packs[0]
        self.assertEqual(
            set(pack),
            {"id", "version", "sha256", "bytes", "url", "publishedAt"},
        )
        self.assertEqual(pack["id"], PACK_ID)
        self.assertEqual(pack["version"], PACK_VERSION)
        self.assertEqual(pack["bytes"], len(data))
        self.assertEqual(pack["url"], f"/packs/{PACK_ID}/{PACK_VERSION}/download")
        # publishedAt 是毫秒时间戳 number（客户端 getAsLong，ISO 字符串会变 0）
        self.assertIsInstance(pack["publishedAt"], int)
        self.assertGreater(pack["publishedAt"], 0)
        self.assertNotIn("signature", pack)
        self.assertNotIn("signingKeyId", pack)


@unittest.skipUnless(
    HAVE_CRYPTOGRAPHY, "需要 cryptography（pip install '.[signing]'）；未装时签名会降级"
)
class TestPacksManifestSignatureHttp(_HttpBase):
    PACK_SIGNING_KEY_B64 = seed_b64()

    def test_packs_manifest_has_signature_and_key_id(self):
        self.store_pack()
        response = self.client.get("/api/fashion/packs")
        self.assertEqual(response.status_code, 200)
        pack = response.json()["packs"][0]
        self.assertIn("signature", pack)
        self.assertEqual(pack["signingKeyId"], self.PACK_SIGNING_KEY_ID)
        signature = base64.b64decode(pack["signature"])
        self.assertEqual(len(signature), 64)
        if HAVE_CRYPTOGRAPHY:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )

            Ed25519PrivateKey.from_private_bytes(FIXED_SEED).public_key().verify(
                signature,
                svc.pack_canonical_string(pack["id"], pack["version"], pack["sha256"]),
            )


class TestPacksManifestWithoutCryptographyHttp(_HttpBase):
    """D1 的现场：配了私钥但生产没装 cryptography —— 必须降级，不能 500。"""

    PACK_SIGNING_KEY_B64 = seed_b64()

    def test_packs_still_200_without_signature(self):
        self.store_pack()
        with cryptography_missing():
            response = self.client.get("/api/fashion/packs")
        self.assertEqual(response.status_code, 200, response.text)
        pack = response.json()["packs"][0]
        self.assertNotIn("signature", pack)
        self.assertNotIn("signingKeyId", pack)


# ── 下载（HEAD 头 / 404 形状） ─────────────────────────────────────────

class TestPackDownloadHttp(_HttpBase):
    def test_head_matches_get_headers(self):
        """E3/E4：文档承诺 ``curl -sI`` 自测，HEAD 必须与 GET 的头一致。"""
        data = self.store_pack()
        path = f"/api/fashion/packs/{PACK_ID}/{PACK_VERSION}/download"
        head = self.client.head(path)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.headers["content-length"], str(len(data)))
        self.assertEqual(head.headers["content-type"], "application/zip")
        self.assertNotIn("location", head.headers)
        self.assertEqual(head.headers["x-content-type-options"], "nosniff")
        self.assertTrue(head.headers.get("etag", "").startswith('"'))

        get = self.client.get(path)
        self.assertEqual(get.status_code, 200)
        self.assertEqual(get.headers["content-length"], str(len(data)))
        self.assertEqual(get.content, data)
        self.assertNotIn("location", get.headers)
        self.assertEqual(get.headers["etag"], head.headers["etag"])

    def test_download_unknown_pack_is_json_404(self):
        response = self.client.get(
            f"/api/fashion/packs/nope/9.9.9/download"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "pack_not_found")


# ── 穿戴配置（部分成功语义 + 全局 ID 落库） ────────────────────────────

class TestLoadoutHttp(_HttpBase):
    def test_put_loadout_without_token_is_401(self):
        response = self.client.put(
            "/api/fashion/loadout",
            json={"items": [{"slot": "cape", "cosmeticId": GLOBAL_COSMETIC_ID}]},
        )
        self.assertEqual(response.status_code, 401)

    def test_put_loadout_unknown_cosmetic_returns_warnings(self):
        _identity, token = self.create_identity()
        response = self.client.put(
            "/api/fashion/loadout",
            json={"items": [{"slot": "cape", "cosmeticId": "example:crown"}]},
            headers=self.auth_headers(token),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["updatedAt"])
        self.assertEqual(
            body["warnings"],
            [
                {
                    "slot": "cape",
                    "cosmeticId": "example:crown",
                    "code": "unknown_cosmetic",
                }
            ],
        )

    def test_put_loadout_stores_global_id_and_reads_back(self):
        """mod 上报全局 ID → 落库/回读都是全局 ID（客户端靠冒号前缀整包下发）。"""
        self.create_catalog_item(
            {
                "cosmeticId": GLOBAL_COSMETIC_ID,
                "packId": "pack1",
                "name": "龙翼披风",
                "category": "cape",
                "assetKey": "cape/dragon.png",
            }
        )
        _identity, token = self.create_identity()
        headers = self.auth_headers(token)
        response = self.client.put(
            "/api/fashion/loadout",
            json={"items": [{"slot": "cape", "cosmeticId": GLOBAL_COSMETIC_ID, "scale": 1.5}]},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["warnings"], [])

        response = self.client.get("/api/fashion/loadout/me", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["cosmeticId"], GLOBAL_COSMETIC_ID)
        self.assertEqual(items[0]["assetKey"], "cape/dragon.png")
        self.assertEqual(items[0]["scale"], 1.5)
        self.assertFalse(items[0]["missing"])
        self.assertEqual(items[0]["packId"], "pack1")

    def test_put_loadout_scale_omitted_keeps_saved_scale(self):
        """``scale`` 省略 = 沿用已存值（mod 不上报 scale，见站点文档 §5.1）。"""
        self.create_catalog_item(
            {"cosmeticId": GLOBAL_COSMETIC_ID, "packId": "pack1", "category": "cape"}
        )
        _identity, token = self.create_identity()
        headers = self.auth_headers(token)
        self.client.put(
            "/api/fashion/loadout",
            json={"items": [{"slot": "cape", "cosmeticId": GLOBAL_COSMETIC_ID, "scale": 2.5}]},
            headers=headers,
        )
        response = self.client.put(
            "/api/fashion/loadout",
            json={"items": [{"slot": "cape", "cosmeticId": GLOBAL_COSMETIC_ID}]},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.get("/api/fashion/loadout/me", headers=headers)
        self.assertEqual(response.json()["items"][0]["scale"], 2.5)


class TestPingTokenRejected(_HttpBase):
    """v1.2：时装路由仍只接受 ``aud=fashion`` —— ping token 调时装接口必须 401。

    ``/api/ping`` 业务路由反过来接受两种受众，但**单向**放宽：独立 VMPing 的
    ``aud="ping"`` token 不能被时装接口当登录态复用。
    """

    def test_ping_token_cannot_call_fashion_catalog(self):
        token, _ttl = fashion_auth.issue_token(
            MC_UUID, mc_uuid=MC_UUID, mc_name="Steve", audience="ping"
        )
        response = self.client.get("/api/fashion/catalog", headers=self.auth_headers(token))
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["detail"]["code"], "invalid_token")

    def test_fashion_token_can_call_fashion_catalog(self):
        """对照：同一路径用 fashion token 正常（确保上面的 401 是受众判定而非路径问题）。"""
        _identity, token = self.create_identity()
        response = self.client.get("/api/fashion/catalog", headers=self.auth_headers(token))
        self.assertEqual(response.status_code, 200, response.text)

    def test_ping_token_cannot_call_wardrobe_me(self):
        """``GET /wardrobe/me`` 走 ``get_principal``（时装 token 或网页 JWT）：
        ping token 既不是 fashion token（aud 不符）也不是网页 JWT（带 aud），必须 401。"""
        token, _ttl = fashion_auth.issue_token(
            MC_UUID, mc_uuid=MC_UUID, mc_name="Steve", audience="ping"
        )
        response = self.client.get(
            "/api/fashion/wardrobe/me", headers=self.auth_headers(token)
        )
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["detail"]["code"], "invalid_token")

    def test_fashion_token_can_call_wardrobe_me(self):
        """对照：fashion token 拉衣柜正常；未绑定网站账号 → ``linked=false``（200 不是错误）。"""
        _identity, token = self.create_identity()
        response = self.client.get(
            "/api/fashion/wardrobe/me", headers=self.auth_headers(token)
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"linked": False, "items": []})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
