"""时装认证单测 —— Mojang / Yggdrasil serverId 流程、SSRF 白名单、设备码。

离线可跑：只用标准库 + ``_stub_env`` 注入的最小 stub，**不发任何真实网络请求**
（``hasJoined`` 的 HTTP 调用全部用假 fetcher 打掉）。

跑法（仓库根目录）::

    python3 -m unittest discover -s backend/tests -p 'test_fashion_*.py' -v
    # 或
    cd backend && python3 -m unittest tests.test_fashion_auth -v
    # 装了 pytest 的环境：
    cd backend && python -m pytest tests/test_fashion_auth.py -q
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _stub_env import install, quiet_stub_noise  # noqa: E402

install()
quiet_stub_noise()


# ── PyJWT 未安装时的最小替身（HS256 + exp + aud，行为与真 PyJWT 一致）──
def _ensure_jwt() -> None:
    try:  # 真依赖优先
        import jwt  # noqa: F401

        return
    except Exception:
        pass

    module = types.ModuleType("jwt")

    class InvalidTokenError(Exception):
        pass

    def _b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def _unb64(text: str) -> bytes:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

    def encode(payload, key, algorithm="HS256"):
        header = {"alg": algorithm, "typ": "JWT"}
        seg = (
            _b64(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64(json.dumps(payload, separators=(",", ":")).encode())
        )
        sig = _b64(hmac.new(str(key).encode(), seg.encode(), hashlib.sha256).digest())
        return f"{seg}.{sig}"

    def decode(token, key, algorithms=None, audience=None, options=None):
        parts = str(token).split(".")
        if len(parts) != 3:
            raise InvalidTokenError("malformed token")
        head, body, sig = parts
        expected = _b64(
            hmac.new(str(key).encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(expected, sig):
            raise InvalidTokenError("bad signature")
        payload = json.loads(_unb64(body))
        if audience is not None:
            aud = payload.get("aud")
            if isinstance(aud, list):
                if audience not in aud:
                    raise InvalidTokenError("bad audience")
            elif aud != audience:
                raise InvalidTokenError("bad audience")
        exp = payload.get("exp")
        if exp is not None and time.time() > float(exp):
            raise InvalidTokenError("expired")
        return payload

    module.encode = encode
    module.decode = decode
    module.InvalidTokenError = InvalidTokenError
    module.PyJWTError = InvalidTokenError
    sys.modules["jwt"] = module


_ensure_jwt()

from vmtools_next.core import fashion_auth as fa  # noqa: E402


# ── 测试夹具 ────────────────────────────────────────────────────────────

UUID_A = "12345678123456781234567812345678"
UUID_A_DASHED = "12345678-1234-5678-1234-567812345678"
UUID_B_DASHED = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SECRET = "unit-test-secret-key-0123456789abcdef"


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


class _Fetcher:
    """假 hasJoined：按 URL 返回预设结果，并记录调用。"""

    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error
        self.calls: list[tuple[str, float]] = []

    async def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        if self.error is not None:
            raise self.error
        for needle, response in self.responses.items():
            if needle in url:
                return response
        return 204, None


# ── 规范化 ──────────────────────────────────────────────────────────────

class TestNormalize(unittest.TestCase):
    def test_uuid_accepts_compact_and_dashed(self):
        self.assertEqual(fa.normalize_uuid(UUID_A), UUID_A_DASHED)
        self.assertEqual(fa.normalize_uuid(UUID_A_DASHED.upper()), UUID_A_DASHED)
        self.assertEqual(fa.normalize_uuid(f"  {UUID_A}  "), UUID_A_DASHED)

    def test_uuid_rejects_garbage(self):
        for bad in ("", None, "not-a-uuid", "1234", UUID_A + "ff"):
            with self.assertRaises(fa.FashionAuthError) as ctx:
                fa.normalize_uuid(bad)
            self.assertEqual(ctx.exception.code, "invalid_uuid")

    def test_mc_name_validation(self):
        self.assertEqual(fa.normalize_mc_name(" Steve_01 "), "Steve_01")
        for bad in ("ab", "x" * 17, "bad name", "bad-name"):
            with self.assertRaises(fa.FashionAuthError) as ctx:
                fa.normalize_mc_name(bad)
            self.assertEqual(ctx.exception.code, "invalid_mc_name")

    def test_auth_server_https_only(self):
        self.assertEqual(
            fa.normalize_auth_server("https://littleskin.cn/api/yggdrasil/"),
            "https://littleskin.cn/api/yggdrasil",
        )
        self.assertEqual(
            fa.normalize_auth_server("http://127.0.0.1:8080/api/yggdrasil"),
            "http://127.0.0.1:8080/api/yggdrasil",
        )
        with self.assertRaises(fa.FashionAuthError):
            fa.normalize_auth_server("http://evil.example/api/yggdrasil")
        with self.assertRaises(fa.FashionAuthError):
            fa.normalize_auth_server("https://user:pw@evil.example/api")
        with self.assertRaises(fa.FashionAuthError):
            fa.normalize_auth_server("ftp://evil.example/api")

    def test_user_code_normalization(self):
        self.assertEqual(fa.normalize_user_code(" ab-12 cd "), "AB12CD")


# ── challenge ───────────────────────────────────────────────────────────

class TestChallenge(unittest.TestCase):
    def setUp(self):
        fa.reset_state()
        self.settings = make_settings()

    def test_challenge_roundtrip(self):
        ch = fa.create_challenge(UUID_A, "Steve", settings=self.settings)
        self.assertEqual(ch.mc_uuid, UUID_A_DASHED)
        self.assertEqual(ch.auth_type, "mojang")
        self.assertGreater(ch.ttl_remaining, 0)
        consumed = fa.consume_challenge(
            ch.server_id, UUID_A, "Steve", settings=self.settings
        )
        self.assertEqual(consumed.mc_uuid, UUID_A_DASHED)

    def test_challenge_is_one_time(self):
        ch = fa.create_challenge(UUID_A, "Steve", settings=self.settings)
        fa.consume_challenge(ch.server_id, UUID_A, "Steve", settings=self.settings)
        with self.assertRaises(fa.FashionAuthError) as ctx:
            fa.consume_challenge(ch.server_id, UUID_A, "Steve", settings=self.settings)
        self.assertEqual(ctx.exception.code, "challenge_not_found")

    def test_challenge_expired(self):
        ch = fa.create_challenge(UUID_A, "Steve", settings=self.settings)
        # 直接把过期时间推到过去（不睡 120s）
        fa._CHALLENGES[ch.server_id].expires_at = time.time() - 1
        with self.assertRaises(fa.FashionAuthError) as ctx:
            fa.consume_challenge(ch.server_id, UUID_A, "Steve", settings=self.settings)
        self.assertEqual(ctx.exception.code, "challenge_expired")
        self.assertNotIn(ch.server_id, fa._CHALLENGES)

    def test_challenge_uuid_mismatch(self):
        ch = fa.create_challenge(UUID_A, "Steve", settings=self.settings)
        with self.assertRaises(fa.FashionAuthError) as ctx:
            fa.consume_challenge(ch.server_id, UUID_B_DASHED, "Steve", settings=self.settings)
        self.assertEqual(ctx.exception.code, "challenge_mismatch")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_challenge_name_mismatch(self):
        ch = fa.create_challenge(UUID_A, "Steve", settings=self.settings)
        with self.assertRaises(fa.FashionAuthError) as ctx:
            fa.consume_challenge(ch.server_id, UUID_A, "Alex", settings=self.settings)
        self.assertEqual(ctx.exception.code, "challenge_mismatch")

    def test_challenge_bound_to_source_ip(self):
        ch = fa.create_challenge(
            UUID_A, "Steve", source_ip="1.1.1.1", settings=self.settings
        )
        with self.assertRaises(fa.FashionAuthError) as ctx:
            fa.consume_challenge(
                ch.server_id, UUID_A, "Steve", source_ip="2.2.2.2", settings=self.settings
            )
        self.assertEqual(ctx.exception.code, "source_mismatch")

    def test_missing_server_id(self):
        with self.assertRaises(fa.FashionAuthError) as ctx:
            fa.consume_challenge("", UUID_A, "Steve", settings=self.settings)
        self.assertEqual(ctx.exception.code, "challenge_not_found")


# ── SSRF / 白名单 ───────────────────────────────────────────────────────

class TestAuthServerWhitelist(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings(
            yggdrasil_bases=[
                "https://littleskin.cn/api/yggdrasil",
                "https://skin.example.com/api/yggdrasil",
            ]
        )

    def test_untrusted_auth_server_is_ignored(self):
        auth_type, base = fa.pick_auth_server(
            "https://evil.example.com/api/yggdrasil", self.settings
        )
        self.assertEqual(auth_type, "yggdrasil")
        self.assertEqual(base, "", "白名单外的 authServer 绝不能被采用")

    def test_trusted_auth_server_is_used(self):
        auth_type, base = fa.pick_auth_server(
            "https://littleskin.cn/api/yggdrasil/", self.settings
        )
        self.assertEqual(auth_type, "yggdrasil")
        self.assertEqual(base, "https://littleskin.cn/api/yggdrasil")

    def test_no_auth_server_means_mojang(self):
        self.assertEqual(fa.pick_auth_server(None, self.settings), ("mojang", ""))
        self.assertEqual(fa.pick_auth_server("  ", self.settings), ("mojang", ""))

    def test_candidates_never_contain_untrusted_host(self):
        ch = fa.create_challenge(
            UUID_A,
            "Steve",
            "https://evil.example.com/api/yggdrasil",
            settings=self.settings,
        )
        urls = [url for url, _t, _b in fa.has_joined_candidates(ch, self.settings)]
        self.assertTrue(urls)
        for url in urls:
            self.assertNotIn("evil.example.com", url)
        # 只允许配置里的两个皮肤站 + mojang
        self.assertTrue(any("littleskin.cn" in u for u in urls))
        self.assertTrue(any("sessionserver.mojang.com" in u for u in urls))

    def test_has_joined_url_shape(self):
        self.assertEqual(
            fa.has_joined_url("https://littleskin.cn/api/yggdrasil", "yggdrasil"),
            "https://littleskin.cn/api/yggdrasil/sessionserver/session/minecraft/hasJoined",
        )
        self.assertEqual(
            fa.has_joined_url("https://sessionserver.mojang.com", "mojang"),
            "https://sessionserver.mojang.com/session/minecraft/hasJoined",
        )

    def test_whitelisted_client_base_tried_first(self):
        ch = fa.create_challenge(
            UUID_A,
            "Steve",
            "https://skin.example.com/api/yggdrasil",
            settings=self.settings,
        )
        candidates = fa.has_joined_candidates(ch, self.settings)
        self.assertIn("skin.example.com", candidates[0][0])


# ── hasJoined 校验 ──────────────────────────────────────────────────────

class TestHasJoined(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fa.reset_state()
        self.settings = make_settings()

    def _challenge(self):
        return fa.create_challenge(UUID_A, "Steve", settings=self.settings)

    async def test_success_returns_identity(self):
        fetcher = _Fetcher({"hasJoined": (200, {"id": UUID_A, "name": "Steve"})})
        info = await fa.verify_with_session_server(
            self._challenge(), settings=self.settings, fetch=fetcher
        )
        self.assertEqual(info["mc_uuid"], UUID_A_DASHED)
        self.assertEqual(info["mc_name"], "Steve")
        self.assertEqual(info["auth_type"], "mojang")
        self.assertEqual(info["auth_server"], "", "正版身份的 authServer 应为空串")

    async def test_uuid_mismatch_is_rejected(self):
        """只比用户名是不够的：返回的 UUID 与声明不一致必须拒绝。"""
        fetcher = _Fetcher({"hasJoined": (200, {"id": UUID_B_DASHED, "name": "Steve"})})
        with self.assertRaises(fa.FashionAuthError) as ctx:
            await fa.verify_with_session_server(
                self._challenge(), settings=self.settings, fetch=fetcher
            )
        self.assertEqual(ctx.exception.code, "uuid_mismatch")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(len(fetcher.calls), 1, "UUID 不匹配应立即拒绝，不再尝试其它候选")

    async def test_not_joined(self):
        fetcher = _Fetcher({})  # 所有候选都 204
        with self.assertRaises(fa.FashionAuthError) as ctx:
            await fa.verify_with_session_server(
                self._challenge(), settings=self.settings, fetch=fetcher
            )
        self.assertEqual(ctx.exception.code, "not_joined")
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_upstream_failure_degrades(self):
        fetcher = _Fetcher(error=TimeoutError("timed out"))
        with self.assertRaises(fa.FashionAuthError) as ctx:
            await fa.verify_with_session_server(
                self._challenge(), settings=self.settings, fetch=fetcher
            )
        self.assertEqual(ctx.exception.code, "upstream_unavailable")
        self.assertEqual(ctx.exception.status_code, 503)

    async def test_http_timeout_is_passed(self):
        fetcher = _Fetcher({"hasJoined": (200, {"id": UUID_A, "name": "Steve"})})
        await fa.verify_with_session_server(
            self._challenge(), settings=self.settings, fetch=fetcher
        )
        self.assertEqual(fetcher.calls[0][1], 5.0, "对外 HTTP 调用必须带 5s 超时")

    async def test_yggdrasil_fallback_when_mojang_misses(self):
        settings = make_settings()
        ch = fa.create_challenge(
            UUID_A,
            "Steve",
            "https://littleskin.cn/api/yggdrasil",
            settings=settings,
        )
        fetcher = _Fetcher({"littleskin.cn": (200, {"id": UUID_A, "name": "Steve"})})
        info = await fa.verify_with_session_server(ch, settings=settings, fetch=fetcher)
        self.assertEqual(info["auth_type"], "yggdrasil")
        self.assertEqual(info["auth_server"], "https://littleskin.cn/api/yggdrasil")


# ── token ───────────────────────────────────────────────────────────────

class TestToken(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings()

    def test_issue_and_decode(self):
        token, ttl = fa.issue_token(
            "identity-1", mc_uuid=UUID_A_DASHED, mc_name="Steve",
            settings=self.settings, secret=SECRET,
        )
        self.assertEqual(ttl, 3600)
        claims = fa.decode_token(token, settings=self.settings, secret=SECRET)
        self.assertEqual(claims["sub"], "identity-1")
        self.assertEqual(claims["aud"], "fashion")
        self.assertEqual(claims["mcUuid"], UUID_A_DASHED)

    def test_tampered_token_rejected(self):
        token, _ = fa.issue_token("identity-1", settings=self.settings, secret=SECRET)
        with self.assertRaises(fa.FashionAuthError) as ctx:
            fa.decode_token(token[:-2] + "xx", settings=self.settings, secret=SECRET)
        self.assertEqual(ctx.exception.code, "invalid_token")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_web_token_is_not_a_fashion_token(self):
        import jwt

        web_token = jwt.encode({"sub": "user-1"}, SECRET, algorithm="HS256")
        with self.assertRaises(fa.FashionAuthError):
            fa.decode_token(web_token, settings=self.settings, secret=SECRET)

    def test_expired_token_rejected(self):
        settings = make_settings(token_ttl_seconds=-10)
        token, _ = fa.issue_token("identity-1", settings=settings, secret=SECRET)
        with self.assertRaises(fa.FashionAuthError):
            fa.decode_token(token, settings=settings, secret=SECRET)


class TestDecodeTokenAny(unittest.TestCase):
    """多受众解码（v1.2）：``/api/ping`` 业务路由同时接受 ``ping`` 与 ``fashion``。"""

    def setUp(self):
        self.settings = make_settings()
        self.audiences = (fa.AUDIENCE_PING, fa.AUDIENCE_FASHION)

    def _token(self, audience: str, ttl: int = 3600) -> str:
        token, _ = fa.issue_token(
            "identity-1",
            mc_uuid=UUID_A_DASHED,
            mc_name="Steve",
            settings=make_settings(token_ttl_seconds=ttl),
            secret=SECRET,
            audience=audience,
        )
        return token

    def test_ping_token_accepted(self):
        claims = fa.decode_token_any(
            self._token(fa.AUDIENCE_PING),
            audiences=self.audiences,
            settings=self.settings,
            secret=SECRET,
        )
        self.assertEqual(claims["aud"], "ping")
        self.assertEqual(claims["mcUuid"], UUID_A_DASHED)

    def test_fashion_token_accepted(self):
        claims = fa.decode_token_any(
            self._token(fa.AUDIENCE_FASHION),
            audiences=self.audiences,
            settings=self.settings,
            secret=SECRET,
        )
        self.assertEqual(claims["aud"], "fashion")

    def test_order_and_duplicates_do_not_matter(self):
        token = self._token(fa.AUDIENCE_FASHION)
        claims = fa.decode_token_any(
            token,
            audiences=(fa.AUDIENCE_FASHION, fa.AUDIENCE_FASHION, fa.AUDIENCE_PING),
            settings=self.settings,
            secret=SECRET,
        )
        self.assertEqual(claims["aud"], "fashion")

    def test_unknown_audience_rejected(self):
        with self.assertRaises(fa.FashionAuthError) as ctx:
            fa.decode_token_any(
                self._token("viewer"),
                audiences=self.audiences,
                settings=self.settings,
                secret=SECRET,
            )
        self.assertEqual(ctx.exception.code, "invalid_token")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_tampered_token_rejected(self):
        token = self._token(fa.AUDIENCE_FASHION)
        with self.assertRaises(fa.FashionAuthError) as ctx:
            fa.decode_token_any(
                token[:-2] + "xx",
                audiences=self.audiences,
                settings=self.settings,
                secret=SECRET,
            )
        self.assertEqual(ctx.exception.code, "invalid_token")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_expired_token_rejected(self):
        with self.assertRaises(fa.FashionAuthError) as ctx:
            fa.decode_token_any(
                self._token(fa.AUDIENCE_FASHION, ttl=-10),
                audiences=self.audiences,
                settings=self.settings,
                secret=SECRET,
            )
        self.assertEqual(ctx.exception.code, "invalid_token")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_empty_audiences_rejected(self):
        with self.assertRaises(fa.FashionAuthError) as ctx:
            fa.decode_token_any(
                self._token(fa.AUDIENCE_PING),
                audiences=(),
                settings=self.settings,
                secret=SECRET,
            )
        self.assertEqual(ctx.exception.code, "invalid_token")

    def test_server_misconfigured_is_not_swallowed(self):
        """多受众尝试不得把「服务端没配密钥」吞成 invalid_token。"""
        import types
        from unittest.mock import patch

        import vmtools_next.config as config_module

        broken = types.SimpleNamespace()  # 没有 server 段 → _jwt_params 抛错
        token = self._token(fa.AUDIENCE_FASHION)
        with patch.object(config_module, "get_config", return_value=broken):
            with self.assertRaises(fa.FashionAuthError) as ctx:
                fa.decode_token_any(token, audiences=self.audiences)
        self.assertEqual(ctx.exception.code, "server_misconfigured")
        self.assertEqual(ctx.exception.status_code, 500)


# ── 设备码 ──────────────────────────────────────────────────────────────

class TestDeviceFlow(unittest.TestCase):
    def setUp(self):
        fa.reset_state()
        self.settings = make_settings()

    def test_start_approve_status(self):
        record = fa.start_device_flow("identity-1", settings=self.settings)
        self.assertEqual(len(record.user_code), 6)
        self.assertTrue(record.user_code.isalnum())
        self.assertEqual(fa.device_flow_status(record.device_code, "identity-1")["status"], "pending")

        approved = fa.approve_device_flow(record.user_code.lower(), "user-9", settings=self.settings)
        self.assertEqual(approved.approved_user_id, "user-9")

        status = fa.device_flow_status(record.device_code, "identity-1")
        self.assertEqual(status["status"], "approved")
        self.assertEqual(status["boundUserId"], "user-9")

    def test_unknown_code(self):
        with self.assertRaises(fa.FashionAuthError) as ctx:
            fa.approve_device_flow("ZZZZZZ", "user-9", settings=self.settings)
        self.assertEqual(ctx.exception.code, "device_not_found")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_expired_code(self):
        record = fa.start_device_flow("identity-1", settings=self.settings)
        fa._DEVICE_CODES[record.device_code].expires_at = time.time() - 1
        with self.assertRaises(fa.FashionAuthError) as ctx:
            fa.approve_device_flow(record.user_code, "user-9", settings=self.settings)
        self.assertEqual(ctx.exception.code, "device_expired")

    def test_bound_to_other_user(self):
        record = fa.start_device_flow("identity-1", settings=self.settings)
        fa.approve_device_flow(record.user_code, "user-1", settings=self.settings)
        with self.assertRaises(fa.FashionAuthError) as ctx:
            fa.approve_device_flow(record.user_code, "user-2", settings=self.settings)
        self.assertEqual(ctx.exception.code, "device_bound_to_other")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_status_hides_other_identity(self):
        record = fa.start_device_flow("identity-1", settings=self.settings)
        with self.assertRaises(fa.FashionAuthError) as ctx:
            fa.device_flow_status(record.device_code, "identity-2")
        self.assertEqual(ctx.exception.code, "device_not_found")

    # ── 内存表清理（已批准的设备码不能永久驻留，否则会被 start 挤掉别人的码） ──

    def test_approved_code_survives_expiry_until_grace(self):
        """刚过期的已批准码要能再查一会儿：玩家最后一秒批准、mod 下一轮才轮询。"""
        record = fa.start_device_flow("identity-1", settings=self.settings)
        fa.approve_device_flow(record.user_code, "user-9", settings=self.settings)
        fa._DEVICE_CODES[record.device_code].expires_at = time.time() - 1
        fa.start_device_flow("identity-2", settings=self.settings)  # 触发清理
        self.assertIn(record.device_code, fa._DEVICE_CODES)
        self.assertEqual(
            fa.device_flow_status(record.device_code, "identity-1")["status"], "approved"
        )

    def test_approved_code_is_pruned_after_grace(self):
        record = fa.start_device_flow("identity-1", settings=self.settings)
        fa.approve_device_flow(record.user_code, "user-9", settings=self.settings)
        fa._DEVICE_CODES[record.device_code].expires_at = (
            time.time() - fa._DEVICE_APPROVED_GRACE_SECONDS - 1
        )
        fa.start_device_flow("identity-2", settings=self.settings)  # 触发清理
        self.assertNotIn(record.device_code, fa._DEVICE_CODES)

    def test_unapproved_expired_code_is_pruned(self):
        record = fa.start_device_flow("identity-1", settings=self.settings)
        fa._DEVICE_CODES[record.device_code].expires_at = time.time() - 1
        fa.start_device_flow("identity-2", settings=self.settings)
        self.assertNotIn(record.device_code, fa._DEVICE_CODES)


# ── 网页 JWT 与 token 混用防护（2026-10-28 审查修复） ───────────────────

class TestWebTokenAudience(unittest.TestCase):
    def _fake_config(self):
        import types

        return types.SimpleNamespace(
            server=types.SimpleNamespace(secret_key=SECRET, jwt_algorithm="HS256")
        )

    def test_web_token_without_aud_is_accepted(self):
        import jwt
        from unittest.mock import patch

        import vmtools_next.config as config_module

        token = jwt.encode({"sub": "user-1"}, SECRET, algorithm="HS256")
        with patch.object(config_module, "get_config", return_value=self._fake_config()):
            self.assertEqual(fa.decode_web_token(token), "user-1")

    def test_viewer_token_is_not_a_web_token(self):
        """aud=viewer 的 iframe token 不能被当成网页登录 JWT。"""
        import jwt
        from unittest.mock import patch

        import vmtools_next.config as config_module

        token = jwt.encode(
            {"sub": "user-1", "aud": "viewer"}, SECRET, algorithm="HS256"
        )
        with patch.object(config_module, "get_config", return_value=self._fake_config()):
            self.assertIsNone(fa.decode_web_token(token))

    def test_fashion_token_is_not_a_web_token(self):
        from unittest.mock import patch

        import vmtools_next.config as config_module

        token, _ = fa.issue_token("identity-1", settings=make_settings(), secret=SECRET)
        with patch.object(config_module, "get_config", return_value=self._fake_config()):
            self.assertIsNone(fa.decode_web_token(token))


if __name__ == "__main__":
    unittest.main(verbosity=2)
