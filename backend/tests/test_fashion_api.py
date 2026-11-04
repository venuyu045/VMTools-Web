"""时装 API 层接线单测 —— 直接调用路由函数（不启 FastAPI），验证端到端串联。

离线可跑：FastAPI 用 ``_stub_env`` 的 stub，DB 用内存 FakeSession，
``hasJoined`` 的 HTTP 调用被假 fetcher 打掉，配置用假的 ``get_config``。

这层测的不是 FastAPI 本身，而是**接线**：路由 → service → auth 之间的参数名、
返回结构、异常翻译（HTTPException.detail.code）是否对得上 —— 这类错误在没有
真依赖的机器上最容易漏。

跑法::

    cd backend && python3 -m unittest tests.test_fashion_api -v
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import time
import types
import unittest
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _stub_env import install, quiet_stub_noise  # noqa: E402

install()
quiet_stub_noise()

from test_fashion_auth import _ensure_jwt  # noqa: E402  （复用 jwt 替身安装）

_ensure_jwt()

import vmtools_next.config as config_module  # noqa: E402
from vmtools_next.api.routers import fashion as api  # noqa: E402
from vmtools_next.api.schemas import fashion as schemas  # noqa: E402
from vmtools_next.core import fashion_auth as fa  # noqa: E402
from vmtools_next.core import fashion_service as svc  # noqa: E402
from vmtools_next.data.models.auth import UserModel  # noqa: E402
from vmtools_next.data.models.fashion import (  # noqa: E402
    FashionCatalogModel,
    FashionIdentityModel,
    FashionLoadoutModel,
)

UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"
SECRET = "unit-test-secret-key-0123456789abcdef"


# ── 最小假 Session（同 test_fashion_loadout 的语义，独立一份避免耦合） ──

class _Expr:
    def __init__(self, name: str):
        self.name = name

    def __eq__(self, other):
        return ("eq", self.name, other)

    def in_(self, values):
        return ("in", self.name, list(values))

    __hash__ = object.__hash__


class _Query:
    def __init__(self, session, model):
        self.session = session
        self.model = model
        self.preds: list[tuple] = []

    def filter_by(self, **kwargs):
        for key, value in kwargs.items():
            self.preds.append(("eq", key, value))
        return self

    def filter(self, *criteria):
        for crit in criteria:
            if isinstance(crit, tuple) and crit and crit[0] in ("eq", "in"):
                self.preds.append(crit)
        return self

    def order_by(self, *args):
        return self

    def _match(self, row) -> bool:
        for op, name, value in self.preds:
            actual = getattr(row, name, None)
            if op == "eq" and actual != value:
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
        for row in self.all():
            self.session.delete(row)
        return 0


class FakeSession:
    def __init__(self):
        self.tables: dict[type, list] = {}
        self.commits = 0

    def rows(self, model):
        return self.tables.setdefault(model, [])

    def query(self, model):
        return _Query(self, model)

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

    def flush(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def get(self, model, pk):
        for row in self.rows(model):
            if getattr(row, "id", None) == pk:
                return row
        return None

    def execute(self, *args, **kwargs):
        return None


FashionIdentityModel.mc_uuid = _Expr("mc_uuid")
FashionLoadoutModel.identity_id = _Expr("identity_id")


def _attr(obj, *names):
    """兼容 stub BaseModel（原样 setattr）与真 pydantic（字段名/别名）两种取值。

    stub 环境下「带默认值 Field(...) 的字段」会在类上留一个 ``Ellipsis``/``None``
    哨兵，所以命中哨兵时继续尝试下一个候选名。
    """
    for name in names:
        if isinstance(obj, dict):
            if obj.get(name) is not None:
                return obj[name]
            continue
        value = getattr(obj, name, None)
        if value is not None and value is not Ellipsis:
            return value
    return None


def make_settings(**overrides):
    base = dict(
        enabled=True,
        yggdrasil_bases=["https://littleskin.cn/api/yggdrasil"],
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


class _Request:
    client = types.SimpleNamespace(host="127.0.0.1")


class _User:
    def __init__(self, user_id: str, role: str = "site_admin"):
        self.id = user_id
        self.role = role
        self.status = "approved"


def _http_code(exc: BaseException) -> str:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return str(detail.get("code", ""))
    return ""


class _Upload:
    """最小 UploadFile 替身：按块返回内容，可指定/省略 size。"""

    def __init__(self, filename: str, data: bytes, size=None):
        self.filename = filename
        self._data = data
        self._offset = 0
        self.size = size
        self.reads = 0

    async def read(self, n: int = -1) -> bytes:
        self.reads += 1
        if n is None or n < 0:
            chunk = self._data[self._offset:]
        else:
            chunk = self._data[self._offset:self._offset + n]
        self._offset += len(chunk)
        return chunk


def _make_pack_zip(pack_id: str = "wings", manifest_id: str = None) -> bytes:
    """构造一个结构合法的时装包 zip（根目录有 pack.json）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("pack.json", '{"id": "%s"}' % (manifest_id or pack_id))
        archive.writestr("cosmetics/crown/model.geo.json", "{}")
        archive.writestr("cosmetics/crown/texture.png", b"\x89PNG-fake")
    return buffer.getvalue()


class _HeadersResponse:
    """``Response`` 替身：只用到 headers 字典（保持离线 stub 环境可跑）。"""

    def __init__(self):
        self.headers = {}


class ApiBase(unittest.TestCase):
    def setUp(self):
        fa.reset_state()
        svc.invalidate_caches()
        api._CHALLENGE_HITS.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(asset_dir=self.tmp.name)
        self.db = FakeSession()

        fake_config = types.SimpleNamespace(
            fashion=self.settings,
            server=types.SimpleNamespace(secret_key=SECRET, jwt_algorithm="HS256"),
        )
        original = config_module.get_config
        config_module.get_config = lambda: fake_config
        self.addCleanup(setattr, config_module, "get_config", original)

        self._orig_fetch = fa._fetch_json
        self.addCleanup(setattr, fa, "_fetch_json", self._orig_fetch)

        svc.upsert_catalog_item(
            self.db,
            {
                "cosmeticId": "cape_dragon",
                "name": "龙翼披风",
                "category": "cape",
                "assetKey": "cape/dragon.png",
            },
        )
        self.identity = svc.upsert_identity(
            self.db, mc_uuid=UUID_A, mc_name="Steve", auth_type="mojang"
        )

    def principal(self, identity_id=None, user_id=None):
        return api.FashionPrincipal(
            identity_id=identity_id if identity_id is not None else self.identity.id,
            user_id=user_id,
            via="mod" if user_id is None else "web",
        )

    def install_fetcher(self, returned_uuid: str = UUID_A, name: str = "Steve"):
        calls = []

        async def _fetch(url, timeout):
            calls.append((url, timeout))
            return 200, {"id": returned_uuid, "name": name}

        fa._fetch_json = _fetch
        return calls


# ── 认证接线 ────────────────────────────────────────────────────────────

class TestAuthEndpoints(ApiBase):
    def test_challenge_then_verify(self):
        ch = api.auth_challenge(
            schemas.ChallengeRequest(mc_uuid=UUID_A, mc_name="Steve"), _Request()
        )
        server_id = _attr(ch, "server_id", "serverId")
        self.assertTrue(server_id)
        self.assertEqual(_attr(ch, "auth_type", "authType"), "mojang")

        calls = self.install_fetcher()
        resp = asyncio.run(
            api.auth_verify(
                schemas.VerifyRequest(
                    mc_uuid=UUID_A, mc_name="Steve", server_id=server_id, auth_server=""
                ),
                _Request(),
                db=self.db,
            )
        )
        token = _attr(resp, "token")
        self.assertTrue(token)
        self.assertEqual(_attr(_attr(resp, "identity"), "mc_uuid", "mcUuid"), UUID_A)
        self.assertEqual(len(calls), 1, "verify 应触发一次 hasJoined")
        self.assertIn("hasJoined", calls[0][0])
        self.assertEqual(calls[0][1], 5.0)

        # 身份已落库，token 可解析且 aud=fashion
        rows = self.db.rows(FashionIdentityModel)
        self.assertEqual(len(rows), 1)
        claims = fa.decode_token(token)
        self.assertEqual(claims["sub"], rows[0].id)

    def test_verify_rejects_uuid_mismatch(self):
        ch = api.auth_challenge(
            schemas.ChallengeRequest(mc_uuid=UUID_A, mc_name="Steve"), _Request()
        )
        server_id = _attr(ch, "server_id", "serverId")
        self.install_fetcher(returned_uuid=UUID_B)
        with self.assertRaises(Exception) as ctx:
            asyncio.run(
                api.auth_verify(
                    schemas.VerifyRequest(
                        mc_uuid=UUID_A, mc_name="Steve", server_id=server_id
                    ),
                    _Request(),
                    db=self.db,
                )
            )
        self.assertEqual(_http_code(ctx.exception), "uuid_mismatch")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 403)

    def test_verify_rejects_replayed_server_id(self):
        ch = api.auth_challenge(
            schemas.ChallengeRequest(mc_uuid=UUID_A, mc_name="Steve"), _Request()
        )
        server_id = _attr(ch, "server_id", "serverId")
        self.install_fetcher()
        body = schemas.VerifyRequest(mc_uuid=UUID_A, mc_name="Steve", server_id=server_id)
        asyncio.run(api.auth_verify(body, _Request(), db=self.db))
        with self.assertRaises(Exception) as ctx:
            asyncio.run(api.auth_verify(body, _Request(), db=self.db))
        self.assertEqual(_http_code(ctx.exception), "challenge_not_found")


# ── 目录 / 穿戴 / 解析 ──────────────────────────────────────────────────

class TestCatalogAndLoadoutEndpoints(ApiBase):
    def test_catalog_returns_items(self):
        resp = api.get_catalog(None, None, self.principal(), self.db)
        items = _attr(resp, "items")
        self.assertEqual(len(items), 1)
        self.assertEqual(_attr(items[0], "cosmetic_id", "cosmeticId"), "cape_dragon")

    def test_put_then_get_loadout(self):
        body = schemas.LoadoutPutRequest(
            items=[schemas.LoadoutItem(slot="cape", cosmeticId="cape_dragon", scale=1.5)]
        )
        put_resp = api.put_loadout(body, None, self.principal(), self.db)
        self.assertTrue(_attr(put_resp, "ok"))
        self.assertTrue(_attr(put_resp, "updated_at", "updatedAt"))

        get_resp = api.get_my_loadout(None, self.principal(), self.db)
        items = _attr(get_resp, "items")
        self.assertEqual(len(items), 1)
        self.assertEqual(_attr(items[0], "slot"), "cape")
        self.assertEqual(_attr(items[0], "cosmetic_id", "cosmeticId"), "cape_dragon")
        self.assertEqual(_attr(items[0], "scale"), 1.5)

    def test_put_loadout_skips_unknown_cosmetic_with_warning(self):
        """mod 本地包的 example:crown 不能把整条 PUT 打 404（其余时装照常同步）。"""
        body = schemas.LoadoutPutRequest(
            items=[
                schemas.LoadoutItem(slot="wings", cosmeticId="example:crown"),
                schemas.LoadoutItem(slot="cape", cosmeticId="cape_dragon"),
            ]
        )
        resp = api.put_loadout(body, None, self.principal(), self.db)
        self.assertTrue(_attr(resp, "ok"))
        warnings = _attr(resp, "warnings")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(_attr(warnings[0], "cosmetic_id", "cosmeticId"), "example:crown")
        self.assertEqual(_attr(warnings[0], "code"), "unknown_cosmetic")

        items = _attr(api.get_my_loadout(None, self.principal(), self.db), "items")
        self.assertEqual([_attr(item, "slot") for item in items], ["cape"])

    def test_put_loadout_still_rejects_structural_errors(self):
        body = schemas.LoadoutPutRequest(
            items=[schemas.LoadoutItem(slot="boots", cosmeticId="cape_dragon")]
        )
        with self.assertRaises(Exception) as ctx:
            api.put_loadout(body, None, self.principal(), self.db)
        self.assertEqual(_http_code(ctx.exception), "invalid_slot")

    def test_web_user_without_identity_gets_no_identity(self):
        principal = api.FashionPrincipal(identity_id=None, user_id="user-1", via="web")
        with self.assertRaises(Exception) as ctx:
            api.get_my_loadout(None, principal, self.db)
        self.assertEqual(_http_code(ctx.exception), "no_identity")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)

    def test_resolve_endpoint(self):
        body = schemas.LoadoutPutRequest(
            items=[schemas.LoadoutItem(slot="cape", cosmeticId="cape_dragon")]
        )
        api.put_loadout(body, None, self.principal(), self.db)

        resp = api.resolve_loadouts(
            schemas.ResolveRequest(mc_uuids=[UUID_A, UUID_B]), self.principal(), self.db
        )
        loadouts = _attr(resp, "loadouts")
        missing = _attr(resp, "missing")
        self.assertIn(UUID_A, loadouts)
        self.assertEqual(missing, [UUID_B])
        items = _attr(loadouts[UUID_A], "items")
        self.assertEqual(_attr(items[0], "asset_key", "assetKey"), "cape/dragon.png")

    def test_resolve_rejects_over_limit(self):
        body = schemas.ResolveRequest(mc_uuids=[f"{i:032x}" for i in range(201)])
        with self.assertRaises(Exception) as ctx:
            api.resolve_loadouts(body, self.principal(), self.db)
        self.assertEqual(_http_code(ctx.exception), "too_many_uuids")


# ── 设备码 ──────────────────────────────────────────────────────────────

class TestDeviceEndpoints(ApiBase):
    def test_start_approve_status(self):
        start = api.device_start(self.principal(), self.db)
        user_code = _attr(start, "user_code", "userCode")
        device_code = _attr(start, "device_code", "deviceCode")
        self.assertEqual(len(user_code), 6)

        approved = api.device_approve(
            schemas.DeviceApproveRequest(user_code=user_code),
            user=_User("user-1"),
            db=self.db,
        )
        self.assertTrue(_attr(approved, "ok"))
        self.assertEqual(self.identity.user_id, "user-1")

        status = api.device_status(device_code, self.principal(), self.db)
        self.assertEqual(_attr(status, "status"), "approved")

    def test_approve_unknown_code(self):
        with self.assertRaises(Exception) as ctx:
            api.device_approve(
                schemas.DeviceApproveRequest(user_code="ZZZZZZ"),
                user=_User("user-1"),
                db=self.db,
            )
        self.assertEqual(_http_code(ctx.exception), "device_not_found")


# ── 管理端 / 资产 ───────────────────────────────────────────────────────

class TestAdminEndpoints(ApiBase):
    def test_catalog_crud(self):
        created = api.admin_create_catalog(
            schemas.CatalogUpsertRequest(
                cosmeticId="wings_angel", category="wings", name="天使之翼"
            ),
            _User("admin-1"),
            self.db,
        )
        self.assertEqual(_attr(created, "cosmetic_id", "cosmeticId"), "wings_angel")

        updated = api.admin_update_catalog(
            "wings_angel",
            schemas.CatalogUpsertRequest(enabled=False),
            _User("admin-1"),
            self.db,
        )
        self.assertFalse(_attr(updated, "enabled"))

        deleted = api.admin_delete_catalog("wings_angel", _User("admin-1"), self.db)
        self.assertTrue(deleted["ok"])
        self.assertIsNone(svc.get_catalog_item(self.db, "wings_angel"))

    def test_catalog_partial_update_preserves_metadata(self):
        before = svc.get_catalog_item(self.db, "cape_dragon")
        before_fields = (before.pack_id, before.name, before.category, before.asset_key)
        api.admin_update_catalog(
            "cape_dragon",
            schemas.CatalogUpsertRequest(enabled=False),
            _User("admin-1"),
            self.db,
        )
        after = svc.get_catalog_item(self.db, "cape_dragon")
        self.assertEqual(
            (after.pack_id, after.name, after.category, after.asset_key),
            before_fields,
        )
        self.assertFalse(after.enabled)

    def test_upload_asset_links_catalog(self):
        payload = _Upload("dragon.png", b"\x89PNG-fake")
        resp = asyncio.run(
            api.admin_upload_asset(
                file=payload,
                pack_id="pack1",
                version="1",
                cosmetic_id="cape_dragon",
                _=_User("admin-1"),
                db=self.db,
            )
        )
        self.assertTrue(_attr(resp, "ok"))
        catalog_row = svc.get_catalog_item(self.db, "cape_dragon")
        self.assertEqual(len(catalog_row.sha256), 64)
        self.assertEqual(catalog_row.bytes, len(b"\x89PNG-fake"))

    def test_asset_404(self):
        with self.assertRaises(Exception) as ctx:
            api.get_asset("no/such.png", self.db)
        self.assertEqual(_http_code(ctx.exception), "asset_not_found")


# ── 安全回归（2026-10-28 审查修复） ─────────────────────────────────────

class TestSecurityRegressions(ApiBase):
    """针对本次审查发现的安全/逻辑问题，防止回归。"""

    def test_identity_id_cannot_access_unbound_other_identity(self):
        """两个 user_id 都是 None 时不能误判为同一 owner。"""
        principal = api.FashionPrincipal(
            identity_id="attacker-identity", user_id=None, via="mod"
        )
        with self.assertRaises(Exception) as ctx:
            api._resolve_identity(principal, self.db, identity_id=self.identity.id)
        self.assertEqual(getattr(ctx.exception, "status_code", None), 403)

    def test_identity_id_keeps_web_multi_identity_and_own_mod_path(self):
        second = svc.upsert_identity(
            self.db, mc_uuid=UUID_B, mc_name="Alex", auth_type="mojang"
        )
        svc.bind_identity_to_user(self.db, second, "user-1")
        # 网页同账号多身份：可切换到自己名下的第二个身份
        web = api.FashionPrincipal(identity_id=self.identity.id, user_id="user-1", via="web")
        got = api._resolve_identity(web, self.db, identity_id=second.id)
        self.assertEqual(got.id, second.id)
        # mod 自己的身份始终可访问
        mod = api.FashionPrincipal(identity_id=self.identity.id, user_id=None, via="mod")
        got = api._resolve_identity(mod, self.db, identity_id=self.identity.id)
        self.assertEqual(got.id, self.identity.id)

    def test_get_principal_rejects_disabled_web_user(self):
        import jwt

        token = jwt.encode({"sub": "banned-1"}, SECRET, algorithm="HS256")
        self.db.rows(UserModel).append(
            types.SimpleNamespace(id="banned-1", status="banned", role="user")
        )
        with self.assertRaises(Exception) as ctx:
            api.get_principal(
                credentials=types.SimpleNamespace(credentials=token), db=self.db
            )
        self.assertEqual(getattr(ctx.exception, "status_code", None), 403)

    def test_get_principal_accepts_approved_web_user(self):
        import jwt

        svc.bind_identity_to_user(self.db, self.identity, "user-1")
        token = jwt.encode({"sub": "user-1"}, SECRET, algorithm="HS256")
        self.db.rows(UserModel).append(
            types.SimpleNamespace(id="user-1", status="approved", role="user")
        )
        principal = api.get_principal(
            credentials=types.SimpleNamespace(credentials=token), db=self.db
        )
        self.assertEqual(principal.user_id, "user-1")
        self.assertEqual(principal.identity_id, self.identity.id)

    def test_device_approve_bound_identity_does_not_poison_code(self):
        svc.bind_identity_to_user(self.db, self.identity, "user-2")
        start = api.device_start(self.principal(), self.db)
        code = _attr(start, "user_code", "userCode")
        with self.assertRaises(Exception) as ctx:
            api.device_approve(
                schemas.DeviceApproveRequest(user_code=code),
                user=_User("user-1"),
                db=self.db,
            )
        self.assertEqual(_http_code(ctx.exception), "identity_bound_to_other")
        record = fa.find_device(code)
        self.assertIsNotNone(record)
        self.assertIsNone(record.approved_user_id, "失败绑定不能把设备码标记为已批准")

    def test_device_approve_accepts_device_code(self):
        start = api.device_start(self.principal(), self.db)
        device_code = _attr(start, "device_code", "deviceCode")
        resp = api.device_approve(
            schemas.DeviceApproveRequest(device_code=device_code),
            user=_User("user-1"),
            db=self.db,
        )
        self.assertTrue(_attr(resp, "ok"))
        self.assertEqual(self.identity.user_id, "user-1")

    def test_device_approve_is_rate_limited(self):
        api._DEVICE_APPROVE_HITS.clear()
        self.addCleanup(api._DEVICE_APPROVE_HITS.clear)
        for _ in range(api._DEVICE_APPROVE_LIMIT):
            with self.assertRaises(Exception) as ctx:
                api.device_approve(
                    schemas.DeviceApproveRequest(user_code="ZZZZZZ"),
                    user=_User("rate-user"),
                    db=self.db,
                )
            self.assertEqual(_http_code(ctx.exception), "device_not_found")
        with self.assertRaises(Exception) as ctx:
            api.device_approve(
                schemas.DeviceApproveRequest(user_code="ZZZZZZ"),
                user=_User("rate-user"),
                db=self.db,
            )
        self.assertEqual(_http_code(ctx.exception), "rate_limited")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 429)

    def test_upload_checks_size_before_reading(self):
        class _BigUpload:
            filename = "big.png"
            size = svc.MAX_ASSET_BYTES + 1
            read_called = False

            async def read(self, *args):
                self.read_called = True
                return b"x"

        payload = _BigUpload()
        with self.assertRaises(Exception) as ctx:
            asyncio.run(
                api.admin_upload_asset(
                    file=payload,
                    pack_id="pack1",
                    version="1",
                    cosmetic_id="",
                    _=_User("admin-1"),
                    db=self.db,
                )
            )
        self.assertEqual(_http_code(ctx.exception), "asset_too_large")
        self.assertFalse(payload.read_called, "超过 64MB 时不应把文件读进内存")


# ── 包级下发（mod 契约 §1/§2：GET /packs + 整包下载） ──────────────────

class TestPackEndpoints(ApiBase):
    """mod 自动下发的两个公开端点。契约不符会让客户端整条链路降级失效。"""

    def store_pack(self, pack_id="wings", version="1.2.0", manifest_id=None):
        return svc.store_asset(
            self.db,
            pack_id=pack_id,
            version=version,
            filename=f"{pack_id}.zip",
            data=_make_pack_zip(pack_id, manifest_id),
            settings=self.settings,
            pack_zip=True,
        )

    def test_packs_manifest_shape(self):
        self.store_pack()
        response = _HeadersResponse()
        resp = api.list_packs(response, self.db)
        packs = _attr(resp, "packs")
        self.assertEqual(len(packs), 1)
        item = packs[0]
        self.assertEqual(_attr(item, "id"), "wings")
        self.assertEqual(_attr(item, "version"), "1.2.0")
        self.assertEqual(len(_attr(item, "sha256")), 64)
        self.assertGreater(_attr(item, "bytes"), 0)
        # 相对 apiBaseUrl（.../api/fashion）的下载地址
        self.assertEqual(_attr(item, "url"), "/packs/wings/1.2.0/download")
        # publishedAt 是毫秒时间戳（number），不是 ISO-8601 字符串
        published = _attr(item, "published_at", "publishedAt")
        self.assertIsInstance(published, int)
        self.assertGreater(published, 1_600_000_000_000)
        self.assertEqual(response.headers.get("Cache-Control"), "public, max-age=300")
        # 未配置签名私钥 → 不输出 signature / signingKeyId
        self.assertIsNone(_attr(item, "signature"))
        self.assertIsNone(_attr(item, "signing_key_id", "signingKeyId"))

    def test_packs_empty_is_200_with_empty_array(self):
        resp = api.list_packs(_HeadersResponse(), self.db)
        self.assertEqual(_attr(resp, "packs"), [])

    def test_download_returns_zip_with_exact_length(self):
        asset = self.store_pack()
        response = api.download_pack("wings", "1.2.0", self.db)
        self.assertEqual(response.media_type, "application/zip")
        self.assertEqual(response.headers.get("ETag"), f'"{asset["sha256"]}"')
        # 不重定向：客户端 HttpClient.Redirect.NEVER
        self.assertNotIn("Location", response.headers)
        # Content-Length 由 FileResponse 按文件大小填，必须等于清单 bytes
        self.assertEqual(os.path.getsize(response.path), asset["bytes"])
        self.assertTrue(response.path.endswith(".zip"))

    def test_download_unknown_pack_is_404(self):
        self.store_pack()
        for pack_id, version in (
            ("wings", "9.9.9"),
            ("nope", "1.2.0"),
            ("..", "1"),
            ("wings", ".."),
            ("/etc", "1"),
            ("server", "1"),
        ):
            with self.subTest(pack_id=pack_id, version=version):
                with self.assertRaises(Exception) as ctx:
                    api.download_pack(pack_id, version, self.db)
                self.assertEqual(_http_code(ctx.exception), "pack_not_found")
                self.assertEqual(getattr(ctx.exception, "status_code", None), 404)

    def test_download_reports_missing_file_as_404(self):
        asset = self.store_pack()
        # 模拟"容器重建没挂持久卷"：DB 有行、磁盘没文件
        os.remove(os.path.join(self.tmp.name, asset["storagePath"]))
        with self.assertRaises(Exception) as ctx:
            api.download_pack("wings", "1.2.0", self.db)
        self.assertEqual(_http_code(ctx.exception), "pack_not_found")

    def test_admin_upload_and_delete_pack(self):
        payload = _Upload("wings.zip", _make_pack_zip("wings"))
        resp = asyncio.run(
            api.admin_upload_pack(
                file=payload, pack_id="wings", version="1.2.0",
                _=_User("admin-1"), db=self.db,
            )
        )
        self.assertTrue(_attr(resp, "ok"))
        self.assertEqual(_attr(_attr(resp, "pack"), "id"), "wings")
        self.assertEqual(len(_attr(api.list_packs(_HeadersResponse(), self.db), "packs")), 1)

        deleted = api.admin_delete_pack(
            "wings", "1.2.0", _=_User("admin-1"), db=self.db
        )
        self.assertTrue(deleted["ok"])
        self.assertEqual(_attr(api.list_packs(_HeadersResponse(), self.db), "packs"), [])
        with self.assertRaises(Exception) as ctx:
            api.admin_delete_pack("wings", "1.2.0", _=_User("admin-1"), db=self.db)
        self.assertEqual(_http_code(ctx.exception), "pack_not_found")

    def test_admin_upload_pack_rejects_bad_zip(self):
        cases = {
            "manifest 不匹配": _make_pack_zip("wings", manifest_id="other"),
            "不是 zip": b"definitely not a zip",
        }
        for label, data in cases.items():
            with self.subTest(case=label):
                payload = _Upload("wings.zip", data)
                with self.assertRaises(Exception) as ctx:
                    asyncio.run(
                        api.admin_upload_pack(
                            file=payload, pack_id="wings", version="1.2.0",
                            _=_User("admin-1"), db=self.db,
                        )
                    )
                self.assertEqual(
                    _http_code(ctx.exception), "pack_id_mismatch"
                    if label == "manifest 不匹配" else "invalid_pack_zip"
                )
        # 坏包一个都不能进清单 / 落盘
        self.assertEqual(_attr(api.list_packs(_HeadersResponse(), self.db), "packs"), [])

    def test_admin_delete_pack_in_use_is_409(self):
        asset = self.store_pack()
        svc.upsert_catalog_item(
            self.db, {"cosmeticId": "cape_dragon", "sha256": asset["sha256"]}
        )
        with self.assertRaises(Exception) as ctx:
            api.admin_delete_pack("wings", "1.2.0", _=_User("admin-1"), db=self.db)
        self.assertEqual(_http_code(ctx.exception), "pack_in_use")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 409)


# ── 资源耗尽 / 上传加固 ────────────────────────────────────────────────

class TestUploadHardening(ApiBase):
    def test_upload_without_size_is_capped_while_reading(self):
        """拿不到 file.size 时也必须分块读并提前 413（不能整个读进内存）。"""
        original = api.MAX_ASSET_BYTES
        api.MAX_ASSET_BYTES = 1024
        self.addCleanup(setattr, api, "MAX_ASSET_BYTES", original)
        payload = _Upload("big.png", b"x" * 100_000, size=None)
        with self.assertRaises(Exception) as ctx:
            asyncio.run(
                api.admin_upload_asset(
                    file=payload, pack_id="pack1", version="1",
                    cosmetic_id="", _=_User("admin-1"), db=self.db,
                )
            )
        self.assertEqual(_http_code(ctx.exception), "asset_too_large")
        self.assertEqual(payload.reads, 1, "超限后应立即停止读取")

    def test_upload_unknown_cosmetic_does_not_store_file(self):
        payload = _Upload("dragon.png", b"\x89PNG-fake")
        with self.assertRaises(Exception) as ctx:
            asyncio.run(
                api.admin_upload_asset(
                    file=payload, pack_id="pack1", version="1",
                    cosmetic_id="nope", _=_User("admin-1"), db=self.db,
                )
            )
        self.assertEqual(_http_code(ctx.exception), "cosmetic_not_found")
        from vmtools_next.data.models.fashion import FashionAssetModel

        self.assertEqual(self.db.rows(FashionAssetModel), [], "404 时不能留下孤儿资产")
        self.assertEqual(payload.reads, 0, "校验失败前不应读取文件")

    def test_device_start_is_rate_limited(self):
        api._DEVICE_START_HITS.clear()
        self.addCleanup(api._DEVICE_START_HITS.clear)
        for _ in range(api._DEVICE_START_LIMIT):
            api.device_start(self.principal(), self.db)
        with self.assertRaises(Exception) as ctx:
            api.device_start(self.principal(), self.db)
        self.assertEqual(_http_code(ctx.exception), "rate_limited")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 429)
        # 429 必须带 Retry-After（mod 的 RetryPolicy 按它等待）
        self.assertEqual(getattr(ctx.exception, "headers", {}).get("Retry-After"), "60")

    def test_challenge_rate_limit_sets_retry_after(self):
        api._CHALLENGE_HITS.clear()
        self.addCleanup(api._CHALLENGE_HITS.clear)
        body = schemas.ChallengeRequest(mc_uuid=UUID_A, mc_name="Steve")
        for _ in range(api._CHALLENGE_LIMIT):
            api.auth_challenge(body, _Request())
        with self.assertRaises(Exception) as ctx:
            api.auth_challenge(body, _Request())
        self.assertEqual(_http_code(ctx.exception), "rate_limited")
        self.assertEqual(getattr(ctx.exception, "headers", {}).get("Retry-After"), "60")


class TestRateLimiterAtomicity(unittest.TestCase):
    """限流表的读-改-写必须原子（第 7 轮审查 L3）。

    三个限流器（challenge / device-approve / device-start）挂在**同步路由**上，
    FastAPI 把它们丢进线程池并发执行。旧实现无锁：并发请求可能读到同一份 ``hits``
    快照，各自 append 后互相覆盖（丢更新）—— 放行数与记录数都会偏离 ``limit``：
    读到同一份快照 → 多个请求同时放行（绕过限流，``device/approve`` 的 10/min 是 6 位
    绑定码的防枚举控制）；写入互相覆盖 → 已计数的命中被抹掉（后续合法请求被误拒）。

    CPython 下这个临界区只有 ~1µs，而 GIL 默认 5ms 才切一次，真实现场很难撞上；
    本用例用一个**人为放慢的 store**（get/setitem 各 sleep 2ms）把窗口放大到必然命中，
    从而确定性地锁住「放行数 == limit 且记录数 == limit」这个不变量（实测把锁换成
    no-op 后 12 并发只放行 1 条、记录 1 条 —— 正是丢更新的表现）。
    """

    class _SlowStore(dict):
        """把读/写各放慢 2ms，放大竞态窗口（仅测试用）。"""

        DELAY = 0.002

        def get(self, key, default=None):
            value = super().get(key, default)
            time.sleep(self.DELAY)
            return value

        def __setitem__(self, key, value):
            time.sleep(self.DELAY)
            super().__setitem__(key, value)

    def test_concurrent_checks_never_exceed_limit(self):
        import threading

        limit = 4
        workers = 12
        for round_index in range(3):
            store = self._SlowStore()
            barrier = threading.Barrier(workers)
            results: list[str] = []
            lock = threading.Lock()

            def worker():
                barrier.wait()
                try:
                    api._check_rate(
                        store, "k", window=60.0, limit=limit, message="太频繁"
                    )
                    outcome = "ok"
                except Exception:
                    outcome = "limited"
                with lock:
                    results.append(outcome)

            threads = [threading.Thread(target=worker) for _ in range(workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            with self.subTest(round=round_index):
                self.assertEqual(
                    results.count("ok"), limit,
                    f"并发下放行的请求数必须正好是 {limit}（丢更新会让它偏离）",
                )
                self.assertEqual(len(store["k"]), limit)


if __name__ == "__main__":
    unittest.main(verbosity=2)
