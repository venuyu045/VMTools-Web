"""标点中继 API 接线单测 —— 直接调用路由函数（不启 FastAPI）。

离线可跑：FastAPI 用 ``_stub_env`` 的 stub，jwt 用 ``test_fashion_auth`` 的替身，
``hasJoined`` 的 HTTP 调用被假 fetcher 打掉，配置用假的 ``get_config``。

这层测的是**接线**：路由 → 中继核心 → fashion_auth 之间的参数名、返回结构、
异常翻译（``HTTPException.detail.code`` / ``Retry-After``）是否对得上。

跑法::

    cd backend && python3 -m unittest tests.test_ping_api -v
"""
from __future__ import annotations

import asyncio
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

from test_fashion_auth import _ensure_jwt, make_settings as make_fashion_settings  # noqa: E402

_ensure_jwt()

import vmtools_next.config as config_module  # noqa: E402
from vmtools_next.api.routers import ping as api  # noqa: E402
from vmtools_next.api.schemas import ping as schemas  # noqa: E402
from vmtools_next.core import fashion_auth as fa  # noqa: E402
from vmtools_next.core import ping_relay as relay_core  # noqa: E402

ROOM = "0123456789abcdef"
ROOM_B = "fedcba9876543210"
UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"
SECRET = "unit-test-secret-key-0123456789abcdef"
OVERWORLD = "minecraft:overworld"
NETHER = "minecraft:the_nether"


# ── 夹具 ────────────────────────────────────────────────────────────────

class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def wall(self) -> float:
        return 1_730_000_000.0 + (self.now - 1000.0)

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Request:
    client = types.SimpleNamespace(host="127.0.0.1")


class _Creds:
    def __init__(self, token: str):
        self.credentials = token


def make_ping_settings(**overrides):
    base = dict(
        enabled=True,
        max_pings_per_room=200,
        ping_ttl_seconds=20.0,
        room_idle_seconds=300.0,
        max_rooms=1000,
        max_waiters_per_room=64,
        max_waiters_total=5000,
        rate_limit=100,  # 单测默认放宽，限流用例自己收紧
        rate_window_seconds=3.0,
        challenge_rate_limit=20,
        challenge_rate_window_seconds=60.0,
        wait_timeout_seconds=20.0,
        max_wait_timeout_seconds=25.0,
        token_ttl_seconds=3600,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def make_body(**overrides):
    params = {
        "dimension": OVERWORLD,
        "x": 1.5,
        "y": 64.0,
        "z": -2.5,
        "kind": "default",
        "target_uuid": None,
        "target_name": None,
        "client_time": None,
    }
    params.update(overrides)
    return schemas.PingSendRequest(**params)


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


class ApiBase(unittest.TestCase):
    def setUp(self):
        fa.reset_state()
        api.reset_state()
        self.clock = FakeClock()
        self.ping_settings = make_ping_settings()
        self.fake_config = types.SimpleNamespace(
            fashion=make_fashion_settings(),
            server=types.SimpleNamespace(secret_key=SECRET, jwt_algorithm="HS256"),
            ping=self.ping_settings,
        )
        original = config_module.get_config
        config_module.get_config = lambda: self.fake_config
        self.addCleanup(setattr, config_module, "get_config", original)

        self._orig_fetch = fa._fetch_json
        self.addCleanup(setattr, fa, "_fetch_json", self._orig_fetch)

        self.relay = self._build_relay()
        api.set_relay(self.relay)
        self.addCleanup(api.reset_state)

    def _build_relay(self) -> relay_core.PingRelay:
        config = relay_core.PingConfig.from_settings(self.ping_settings)
        return relay_core.PingRelay(
            config, clock=self.clock, wall_clock=self.clock.wall
        )

    def use_ping_settings(self, **overrides) -> relay_core.PingRelay:
        """换一套 ping 配置（会重建中继与 challenge 限流器）。"""
        self.ping_settings = make_ping_settings(**overrides)
        self.fake_config.ping = self.ping_settings
        api.reset_state()
        self.relay = self._build_relay()
        api.set_relay(self.relay)
        return self.relay

    # ── 鉴权夹具 ────────────────────────────────────────────────────────
    def token(self, mc_uuid: str = UUID_A, mc_name: str = "Steve", audience: str = api.TOKEN_AUDIENCE, ttl: int = 3600) -> str:
        token, _ = fa.issue_token(
            mc_uuid,
            mc_uuid=mc_uuid,
            mc_name=mc_name,
            audience=audience,
            ttl_seconds=ttl,
            secret=SECRET,
        )
        return token

    def principal(self, mc_uuid: str = UUID_A, mc_name: str = "Steve") -> api.PingPrincipal:
        return api.PingPrincipal(mc_uuid=mc_uuid, mc_name=mc_name)

    def send(self, room: str = ROOM, body=None, principal=None, version=None):
        """同步调一次发送端点（路由是 async def，测试里用 asyncio.run 包一层）。"""
        return asyncio.run(
            api.send_ping(
                room,
                make_body() if body is None else body,
                self.principal() if principal is None else principal,
                version,
            )
        )

    def install_fetcher(self, returned_uuid: str = UUID_A, name: str = "Steve"):
        calls = []

        async def _fetch(url, timeout):
            calls.append((url, timeout))
            return 200, {"id": returned_uuid, "name": name}

        fa._fetch_json = _fetch
        return calls

    def install_fetcher_status(self, status_code: int, payload=None):
        async def _fetch(url, timeout):
            return status_code, payload

        fa._fetch_json = _fetch

    def install_fetcher_error(self):
        async def _fetch(url, timeout):
            raise RuntimeError("boom")

        fa._fetch_json = _fetch


# ── 鉴权依赖 ────────────────────────────────────────────────────────────

class TestPrincipal(ApiBase):
    def test_missing_credentials_is_401_unauthorized(self):
        with self.assertRaises(Exception) as ctx:
            api.get_ping_principal(None)
        self.assertEqual(_http_code(ctx.exception), "unauthorized")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 401)

    def test_empty_token_is_401_unauthorized(self):
        with self.assertRaises(Exception) as ctx:
            api.get_ping_principal(_Creds(""))
        self.assertEqual(_http_code(ctx.exception), "unauthorized")

    def test_garbage_token_is_401_bad_token(self):
        with self.assertRaises(Exception) as ctx:
            api.get_ping_principal(_Creds("not-a-jwt"))
        self.assertEqual(_http_code(ctx.exception), "bad_token")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 401)

    def test_expired_token_is_401_bad_token(self):
        expired = self.token(ttl=-10)
        with self.assertRaises(Exception) as ctx:
            api.get_ping_principal(_Creds(expired))
        self.assertEqual(_http_code(ctx.exception), "bad_token")

    def test_fashion_token_cannot_call_ping(self):
        fashion_token = self.token(audience=fa.AUDIENCE_FASHION)
        with self.assertRaises(Exception) as ctx:
            api.get_ping_principal(_Creds(fashion_token))
        self.assertEqual(_http_code(ctx.exception), "bad_token")

    def test_ping_token_cannot_call_fashion(self):
        ping_token = self.token()
        with self.assertRaises(fa.FashionAuthError):
            fa.decode_token(ping_token, secret=SECRET)  # 默认 audience=fashion

    def test_valid_token_yields_principal(self):
        principal = api.get_ping_principal(_Creds(self.token()))
        self.assertEqual(principal.mc_uuid, UUID_A)
        self.assertEqual(principal.mc_name, "Steve")

    def test_compact_uuid_claim_is_normalized(self):
        compact = UUID_A.replace("-", "")
        principal = api.get_ping_principal(_Creds(self.token(mc_uuid=compact)))
        self.assertEqual(principal.mc_uuid, UUID_A)


# ── 认证：challenge / verify ───────────────────────────────────────────

class TestAuthEndpoints(ApiBase):
    def test_challenge_then_verify_issues_ping_token(self):
        challenge = api.auth_challenge(
            schemas.ChallengeRequest(mc_uuid=UUID_A, mc_name="Steve"), _Request()
        )
        server_id = _attr(challenge, "server_id", "serverId")
        self.assertTrue(server_id)
        # TTL 是 int(expires_at - now)，有小数截断，允许 119/120
        self.assertIn(_attr(challenge, "expires_in", "expiresIn"), (119, 120))
        self.assertEqual(_attr(challenge, "auth_type", "authType"), "mojang")

        calls = self.install_fetcher()
        before = int(time.time() * 1000)
        response = asyncio.run(
            api.auth_verify(
                schemas.VerifyRequest(
                    mc_uuid=UUID_A, mc_name="Steve", server_id=server_id, auth_server=""
                ),
                _Request(),
            )
        )
        after = int(time.time() * 1000)

        token = _attr(response, "token")
        self.assertTrue(token)
        self.assertEqual(_attr(response, "mc_uuid", "mcUuid"), UUID_A)
        self.assertEqual(_attr(response, "mc_name", "mcName"), "Steve")
        self.assertEqual(_attr(response, "auth_type", "authType"), "mojang")
        expires_at = _attr(response, "expires_at", "expiresAt")
        self.assertGreaterEqual(expires_at, before + 3600 * 1000)
        self.assertLessEqual(expires_at, after + 3600 * 1000 + 1000)

        claims = fa.decode_token(token, audience=fa.AUDIENCE_PING, secret=SECRET)
        self.assertEqual(claims["aud"], "ping")
        self.assertEqual(claims["sub"], UUID_A)
        self.assertEqual(claims["mcUuid"], UUID_A)
        self.assertEqual(len(calls), 1, "verify 应触发一次 hasJoined")
        self.assertIn("hasJoined", calls[0][0])
        self.assertEqual(calls[0][1], 5.0)

    def test_verify_rejects_replayed_server_id(self):
        challenge = api.auth_challenge(
            schemas.ChallengeRequest(mc_uuid=UUID_A, mc_name="Steve"), _Request()
        )
        server_id = _attr(challenge, "server_id", "serverId")
        self.install_fetcher()
        body = schemas.VerifyRequest(mc_uuid=UUID_A, mc_name="Steve", server_id=server_id)
        asyncio.run(api.auth_verify(body, _Request()))
        with self.assertRaises(Exception) as ctx:
            asyncio.run(api.auth_verify(body, _Request()))
        self.assertEqual(_http_code(ctx.exception), "challenge_not_found")

    def test_verify_rejects_uuid_mismatch(self):
        challenge = api.auth_challenge(
            schemas.ChallengeRequest(mc_uuid=UUID_A, mc_name="Steve"), _Request()
        )
        server_id = _attr(challenge, "server_id", "serverId")
        self.install_fetcher(returned_uuid=UUID_B)
        with self.assertRaises(Exception) as ctx:
            asyncio.run(
                api.auth_verify(
                    schemas.VerifyRequest(
                        mc_uuid=UUID_A, mc_name="Steve", server_id=server_id
                    ),
                    _Request(),
                )
            )
        self.assertEqual(_http_code(ctx.exception), "uuid_mismatch")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 403)

    def test_verify_not_joined(self):
        challenge = api.auth_challenge(
            schemas.ChallengeRequest(mc_uuid=UUID_A, mc_name="Steve"), _Request()
        )
        server_id = _attr(challenge, "server_id", "serverId")
        self.install_fetcher_status(204)
        with self.assertRaises(Exception) as ctx:
            asyncio.run(
                api.auth_verify(
                    schemas.VerifyRequest(
                        mc_uuid=UUID_A, mc_name="Steve", server_id=server_id
                    ),
                    _Request(),
                )
            )
        self.assertEqual(_http_code(ctx.exception), "not_joined")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 403)

    def test_verify_upstream_unavailable(self):
        challenge = api.auth_challenge(
            schemas.ChallengeRequest(mc_uuid=UUID_A, mc_name="Steve"), _Request()
        )
        server_id = _attr(challenge, "server_id", "serverId")
        self.install_fetcher_error()
        with self.assertRaises(Exception) as ctx:
            asyncio.run(
                api.auth_verify(
                    schemas.VerifyRequest(
                        mc_uuid=UUID_A, mc_name="Steve", server_id=server_id
                    ),
                    _Request(),
                )
            )
        self.assertEqual(_http_code(ctx.exception), "upstream_unavailable")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 503)

    def test_challenge_rejects_bad_uuid(self):
        with self.assertRaises(Exception) as ctx:
            api.auth_challenge(
                schemas.ChallengeRequest(mc_uuid="nope", mc_name="Steve"), _Request()
            )
        self.assertEqual(_http_code(ctx.exception), "invalid_uuid")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 400)

    def test_challenge_rate_limited_per_ip(self):
        self.use_ping_settings(challenge_rate_limit=2, challenge_rate_window_seconds=60.0)
        for _ in range(2):
            api.auth_challenge(
                schemas.ChallengeRequest(mc_uuid=UUID_A, mc_name="Steve"), _Request()
            )
        with self.assertRaises(Exception) as ctx:
            api.auth_challenge(
                schemas.ChallengeRequest(mc_uuid=UUID_A, mc_name="Steve"), _Request()
            )
        self.assertEqual(_http_code(ctx.exception), "rate_limited")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 429)
        self.assertIn("Retry-After", getattr(ctx.exception, "headers", {}) or {})

    def test_ping_challenge_cannot_be_used_for_fashion(self):
        challenge = api.auth_challenge(
            schemas.ChallengeRequest(mc_uuid=UUID_A, mc_name="Steve"), _Request()
        )
        server_id = _attr(challenge, "server_id", "serverId")
        with self.assertRaises(fa.FashionAuthError) as ctx:
            fa.consume_challenge(server_id, UUID_A, "Steve", audience=fa.AUDIENCE_FASHION)
        self.assertEqual(ctx.exception.code, "challenge_mismatch")
        # 用途不符不会消费掉挑战：ping 自己仍可用它
        consumed = fa.consume_challenge(server_id, UUID_A, "Steve", audience=fa.AUDIENCE_PING)
        self.assertEqual(consumed.server_id, server_id)

    def test_fashion_challenge_cannot_be_used_for_ping(self):
        challenge = fa.create_challenge(UUID_A, "Steve", audience=fa.AUDIENCE_FASHION)
        with self.assertRaises(fa.FashionAuthError) as ctx:
            fa.consume_challenge(
                challenge.server_id, UUID_A, "Steve", audience=fa.AUDIENCE_PING
            )
        self.assertEqual(ctx.exception.code, "challenge_mismatch")


# ── 发送标点 ────────────────────────────────────────────────────────────

class TestSendPing(ApiBase):
    def test_send_ping_returns_201_shape(self):
        response = self.send()
        self.assertEqual(_attr(response, "seq"), 1)
        self.assertTrue(_attr(response, "id"))
        self.assertEqual(_attr(response, "server_time", "serverTime"), int(self.clock.wall() * 1000))
        self.assertEqual(
            _attr(response, "expires_at", "expiresAt") - _attr(response, "server_time", "serverTime"),
            20_000,
        )
        self.assertEqual(self.relay.stats()["pings"], 1)

    def test_send_ping_uses_token_identity(self):
        self.send(principal=self.principal(mc_uuid=UUID_B, mc_name="Alex"))
        ping = self.relay.snapshot(ROOM).pings[0]
        self.assertEqual(ping.player_uuid, UUID_B)
        self.assertEqual(ping.player_name, "Alex")

    def test_send_ping_normalizes_target_uuid(self):
        self.send(
            body=make_body(
                target_uuid=UUID_A.replace("-", "").upper(), target_name=" Notch "
            )
        )
        ping = self.relay.snapshot(ROOM).pings[0]
        self.assertEqual(ping.target_uuid, UUID_A)
        self.assertEqual(ping.target_name, "Notch")

    def test_send_ping_accepts_version_header(self):
        self.send(version="1.0.0")
        self.assertEqual(self.relay.stats()["pings"], 1)

    def test_send_ping_bad_room_id_is_400(self):
        with self.assertRaises(Exception) as ctx:
            self.send(room="not-a-room")
        self.assertEqual(_http_code(ctx.exception), "bad_request")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 400)

    def test_send_ping_bad_coordinates_is_400(self):
        for overrides in (
            {"x": float("nan")},
            {"y": float("inf")},
            {"z": 3e7 + 1},
            {"y": 2049},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(Exception) as ctx:
                    self.send(body=make_body(**overrides))
                self.assertEqual(_http_code(ctx.exception), "bad_request")

    def test_send_ping_bad_kind_is_400(self):
        with self.assertRaises(Exception) as ctx:
            self.send(body=make_body(kind="explosion"))
        self.assertEqual(_http_code(ctx.exception), "bad_request")

    def test_send_ping_bad_dimension_is_400(self):
        for dimension in ("", "d" * 129):
            with self.subTest(dimension=dimension[:8]):
                with self.assertRaises(Exception) as ctx:
                    self.send(body=make_body(dimension=dimension))
                self.assertEqual(_http_code(ctx.exception), "bad_request")

    def test_send_ping_bad_target_name_is_400(self):
        with self.assertRaises(Exception) as ctx:
            self.send(body=make_body(target_name="n" * 65))
        self.assertEqual(_http_code(ctx.exception), "bad_request")

    def test_send_ping_rate_limited_is_429_with_retry_after(self):
        self.use_ping_settings(rate_limit=5, rate_window_seconds=3.0)
        for _ in range(5):
            self.send()
        with self.assertRaises(Exception) as ctx:
            self.send()
        self.assertEqual(_http_code(ctx.exception), "rate_limited")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 429)
        headers = getattr(ctx.exception, "headers", {}) or {}
        self.assertEqual(headers.get("Retry-After"), "3")

    def test_send_ping_room_full_is_429(self):
        self.use_ping_settings(max_pings_per_room=2, rate_limit=100)
        self.send()
        self.send()
        with self.assertRaises(Exception) as ctx:
            self.send()
        self.assertEqual(_http_code(ctx.exception), "room_full")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 429)

    def test_send_ping_rate_limit_is_per_player(self):
        self.use_ping_settings(rate_limit=1, rate_window_seconds=60.0)
        self.send(principal=self.principal(mc_uuid=UUID_A))
        with self.assertRaises(Exception):
            self.send(principal=self.principal(mc_uuid=UUID_A))
        self.send(principal=self.principal(mc_uuid=UUID_B))
        self.assertEqual(self.relay.stats()["pings"], 2)


# ── 长轮询拉取 ──────────────────────────────────────────────────────────

class TestFetchPings(ApiBase):
    def test_fetch_returns_all_alive_pings(self):
        self.send()
        self.send(body=make_body(x=2.0))
        response = asyncio.run(api.fetch_pings(ROOM, 0, 0, None, self.principal()))
        pings = _attr(response, "pings")
        self.assertEqual(len(pings), 2)
        self.assertEqual(_attr(response, "seq"), 2)
        first = pings[0]
        self.assertEqual(_attr(first, "seq"), 1)
        self.assertEqual(_attr(first, "x"), 1.5)
        self.assertEqual(_attr(first, "dimension"), OVERWORLD)
        player = _attr(first, "player")
        self.assertEqual(_attr(player, "uuid"), UUID_A)
        self.assertEqual(_attr(player, "name"), "Steve")
        self.assertEqual(_attr(first, "server_time", "serverTime"), int(self.clock.wall() * 1000))

    def test_fetch_since_filters(self):
        self.send()
        self.send()
        response = asyncio.run(api.fetch_pings(ROOM, 1, 0, None, self.principal()))
        pings = _attr(response, "pings")
        self.assertEqual([_attr(ping, "seq") for ping in pings], [2])

    def test_fetch_dimension_filter(self):
        self.send(body=make_body(dimension=OVERWORLD))
        self.send(body=make_body(dimension=NETHER))
        response = asyncio.run(api.fetch_pings(ROOM, 0, 0, NETHER, self.principal()))
        pings = _attr(response, "pings")
        self.assertEqual([_attr(ping, "dimension") for ping in pings], [NETHER])

    def test_fetch_unknown_room_is_empty(self):
        response = asyncio.run(api.fetch_pings(ROOM, 0, 0, None, self.principal()))
        self.assertEqual(_attr(response, "seq"), 0)
        self.assertEqual(_attr(response, "pings"), [])

    def test_fetch_wakes_on_publish(self):
        async def scenario():
            task = asyncio.create_task(api.fetch_pings(ROOM, 0, 5.0, None, self.principal()))
            await asyncio.sleep(0.02)
            await api.send_ping(ROOM, make_body(), self.principal())
            return await task

        response = asyncio.run(scenario())
        pings = _attr(response, "pings")
        self.assertEqual(len(pings), 1)
        self.assertEqual(_attr(response, "seq"), 1)

    def test_fetch_global_waiter_cap_is_503_server_busy(self):
        self.use_ping_settings(max_waiters_total=1, wait_timeout_seconds=5.0)

        async def scenario():
            first = asyncio.create_task(
                api.fetch_pings(ROOM, 0, 5.0, None, self.principal())
            )
            await asyncio.sleep(0.02)
            try:
                await api.fetch_pings(ROOM_B, 0, 5.0, None, self.principal())
            finally:
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)

        with self.assertRaises(Exception) as ctx:
            asyncio.run(scenario())
        self.assertEqual(_http_code(ctx.exception), "server_busy")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 503)
        headers = getattr(ctx.exception, "headers", {}) or {}
        self.assertEqual(headers.get("Retry-After"), "1")

    def test_fetch_bad_room_id_is_400(self):
        with self.assertRaises(Exception) as ctx:
            asyncio.run(api.fetch_pings("zzz", 0, 0, None, self.principal()))
        self.assertEqual(_http_code(ctx.exception), "bad_request")

    def test_fetch_dimension_too_long_is_400(self):
        with self.assertRaises(Exception) as ctx:
            asyncio.run(api.fetch_pings(ROOM, 0, 0, "d" * 129, self.principal()))
        self.assertEqual(_http_code(ctx.exception), "bad_request")

    def test_fetch_timeout_is_clamped(self):
        self.use_ping_settings(wait_timeout_seconds=0.05, max_wait_timeout_seconds=0.05)
        started = time.monotonic()
        asyncio.run(api.fetch_pings(ROOM, 0, 100.0, None, self.principal()))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_fetch_negative_since_is_treated_as_zero(self):
        self.send()
        response = asyncio.run(api.fetch_pings(ROOM, -5, 0, None, self.principal()))
        self.assertEqual(len(_attr(response, "pings")), 1)


# ── 中继单例 / 配置 ────────────────────────────────────────────────────

class TestRelayWiring(ApiBase):
    def test_relay_is_rebuilt_from_config_after_reset(self):
        first = api.get_relay()
        api.reset_state()
        second = api.get_relay()
        self.assertIsNot(first, second)
        self.assertEqual(second.config.rate_limit, self.ping_settings.rate_limit)

    def test_get_relay_falls_back_to_defaults_without_config(self):
        config_module.get_config = lambda: types.SimpleNamespace()  # 没有 ping 段
        api.reset_state()
        relay = api.get_relay()
        self.assertEqual(relay.config.max_pings_per_room, 200)

    def test_set_relay_overrides_singleton(self):
        replacement = relay_core.PingRelay(relay_core.PingConfig())
        api.set_relay(replacement)
        self.assertIs(api.get_relay(), replacement)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
