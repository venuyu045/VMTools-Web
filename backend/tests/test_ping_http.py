"""标点中继的 **HTTP 级**集成测试（TestClient）+ 路由表冒烟。

为什么要单开一个文件
--------------------
``test_ping_api.py`` 是「直接调用路由函数」，中间件、依赖注入、真实的 201/429
响应头、``HTTPException.headers`` → ``Retry-After`` 的落地**都没覆盖**。本文件用
``fastapi.testclient.TestClient`` 跑真实 HTTP 语义（进程内，不联网）。

⚠️ 本机（离线环境）**没有安装 fastapi / starlette / httpx**，``TestClient`` 不可用：
``tests/_stub_env.py`` 里的 fastapi 是假模块（``FastAPI=object``）。所以真实 HTTP
用例用 ``@unittest.skipUnless(_HAS_HTTP_STACK, ...)`` 自动跳过，在装了依赖的机器 /
CI 上会真正执行；**路由表用例不跳过**（stub 也能跑），保证「4 个端点确实挂在
/api/ping 下」这条最低契约在任何环境都有覆盖。

跑法::

    cd backend && python3 -m unittest tests.test_ping_http -v
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _stub_env import install, quiet_stub_noise  # noqa: E402

install()
quiet_stub_noise()

from vmtools_next.api.routers import ping as api  # noqa: E402
from vmtools_next.core import fashion_auth  # noqa: E402
from vmtools_next.core import ping_relay as relay_core  # noqa: E402

try:  # 离线 stub 环境里 fastapi 是假的：FastAPI=object，没有 include_router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _HAS_HTTP_STACK = hasattr(FastAPI, "include_router") and hasattr(TestClient, "get")
except Exception:  # pragma: no cover - 取决于环境
    _HAS_HTTP_STACK = False

# 配置里 secret_key 有长度/占位值校验，给一个明显是测试用的 64 位 hex
TEST_SECRET_KEY = "0123456789abcdef" * 4
MC_UUID = "11111111-1111-1111-1111-111111111111"
MC_NAME = "Steve"
ROOM = "0123456789abcdef"
OVERWORLD = "minecraft:overworld"


# ── 路由表（任何环境都跑） ──────────────────────────────────────────────

def _iter_routes(router_obj):
    """返回 ``(prefix, [(METHOD, 完整 path), ...])``，兼容 stub 与真 APIRouter。"""
    prefix = str(getattr(router_obj, "prefix", "") or "")
    entries: list[tuple[str, str]] = []
    for route in getattr(router_obj, "routes", []) or []:
        if isinstance(route, dict):  # tests/_stub_env.py 的假 _Router
            method = str(route.get("method") or "").upper()
            path = str(route.get("path") or "")
            entries.append((method, path))
            continue
        path = str(getattr(route, "path", "") or "")
        for method in getattr(route, "methods", None) or set():
            entries.append((str(method).upper(), path))
    full = [
        (method, path if path.startswith(prefix) else prefix + path)
        for method, path in entries
    ]
    return prefix, full


class TestPingRouteTable(unittest.TestCase):
    """最低契约：4 个端点都注册在 /api/ping 下、方法正确。"""

    def test_router_prefix(self):
        prefix, _ = _iter_routes(api.router)
        self.assertEqual(prefix, "/api/ping")

    def test_endpoints_registered(self):
        _, entries = _iter_routes(api.router)
        expected = {
            ("POST", "/api/ping/auth/challenge"),
            ("POST", "/api/ping/auth/verify"),
            ("POST", "/api/ping/rooms/{room_id}/pings"),
            ("GET", "/api/ping/rooms/{room_id}/pings"),
        }
        actual = set(entries)
        for method, path in expected:
            self.assertIn((method, path), actual)
        # 路径集合必须完全一致（多挂/少挂端点都算契约漂移；GET 会被 Starlette 补 HEAD）
        self.assertEqual({path for _, path in actual}, {path for _, path in expected})

    def test_no_unexpected_methods(self):
        _, entries = _iter_routes(api.router)
        for method, path in entries:
            self.assertIn(method, {"GET", "POST", "HEAD"}, f"{method} {path}")


# ── 真实 HTTP（装了 fastapi + httpx 才跑） ────────────────────────────

@unittest.skipUnless(_HAS_HTTP_STACK, "需要真实 FastAPI + httpx（离线 stub 环境跳过）")
class TestPingHttp(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(
            os.environ,
            {
                "VMT_SERVER__SECRET_KEY": TEST_SECRET_KEY,
                # 默认容量对齐协议 §6；限流用例自己收紧
                "VMT_PING__RATE_LIMIT": "5",
                "VMT_PING__RATE_WINDOW_SECONDS": "3.0",
                "VMT_PING__MAX_PINGS_PER_ROOM": "200",
                "VMT_PING__WAIT_TIMEOUT_SECONDS": "20",
                "VMT_PING__MAX_WAIT_TIMEOUT_SECONDS": "25",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

        from vmtools_next.config import get_config

        get_config.cache_clear()
        self.addCleanup(get_config.cache_clear)
        api.reset_state()
        self.addCleanup(api.reset_state)

        self.app = FastAPI()
        self.app.include_router(api.router)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    # ── 夹具 ────────────────────────────────────────────────────────────
    def issue_token(self, audience: str = api.TOKEN_AUDIENCE) -> str:
        token, _ttl = fashion_auth.issue_token(
            MC_UUID, mc_uuid=MC_UUID, mc_name=MC_NAME, audience=audience
        )
        return token

    def auth_headers(self, token: str = "") -> dict:
        return {"Authorization": f"Bearer {token or self.issue_token()}"}

    def ping_payload(self, **overrides) -> dict:
        payload = {
            "dimension": OVERWORLD,
            "x": 1.5,
            "y": 64.0,
            "z": -2.5,
            "kind": "default",
            "clientTime": 1,
        }
        payload.update(overrides)
        return payload

    def send_ping(self, headers: dict = None, **overrides):
        return self.client.post(
            f"/api/ping/rooms/{ROOM}/pings",
            json=self.ping_payload(**overrides),
            headers=headers if headers is not None else self.auth_headers(),
        )

    # ── 鉴权 ────────────────────────────────────────────────────────────
    def test_missing_token_is_401_unauthorized(self):
        response = self.client.post(
            f"/api/ping/rooms/{ROOM}/pings", json=self.ping_payload()
        )
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["detail"]["code"], "unauthorized")

    def test_garbage_token_is_401_bad_token(self):
        response = self.client.get(
            f"/api/ping/rooms/{ROOM}/pings",
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["detail"]["code"], "bad_token")

    def test_fashion_token_can_send_and_fetch(self):
        """v1.2：时装 token（aud=fashion）可发标点并长轮询（VMFashion 2.0 合订版）。"""
        headers = {"Authorization": f"Bearer {self.issue_token('fashion')}"}
        sent = self.client.post(
            f"/api/ping/rooms/{ROOM}/pings", json=self.ping_payload(), headers=headers
        )
        self.assertEqual(sent.status_code, 201, sent.text)
        fetched = self.client.get(
            f"/api/ping/rooms/{ROOM}/pings?since=0&timeout=0", headers=headers
        )
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(
            fetched.json()["pings"][0]["player"], {"uuid": MC_UUID, "name": MC_NAME}
        )

    def test_ping_token_still_send_and_fetch(self):
        """回归：独立 VMPing 0.1.0 的 aud=ping token 行为不变。"""
        headers = {"Authorization": f"Bearer {self.issue_token('ping')}"}
        sent = self.client.post(
            f"/api/ping/rooms/{ROOM}/pings", json=self.ping_payload(), headers=headers
        )
        self.assertEqual(sent.status_code, 201, sent.text)
        fetched = self.client.get(
            f"/api/ping/rooms/{ROOM}/pings?since=0&timeout=0", headers=headers
        )
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(len(fetched.json()["pings"]), 1)

    def test_unknown_audience_token_is_401_bad_token(self):
        """aud 不在 {ping, fashion} 内（如 viewer）仍必须 401 bad_token。"""
        response = self.client.get(
            f"/api/ping/rooms/{ROOM}/pings",
            headers={"Authorization": f"Bearer {self.issue_token('viewer')}"},
        )
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["detail"]["code"], "bad_token")

    # ── 认证接口 ────────────────────────────────────────────────────────
    def test_auth_challenge_returns_server_id(self):
        response = self.client.post(
            "/api/ping/auth/challenge",
            json={"mcUuid": MC_UUID, "mcName": MC_NAME, "authServer": ""},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["serverId"])
        self.assertIn(body["authType"], ("mojang", "yggdrasil"))
        self.assertLessEqual(body["expiresIn"], 120)

    def test_auth_challenge_bad_uuid_is_400(self):
        response = self.client.post(
            "/api/ping/auth/challenge", json={"mcUuid": "nope", "mcName": MC_NAME}
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"]["code"], "invalid_uuid")

    # ── 发送标点 ────────────────────────────────────────────────────────
    def test_send_ping_is_201_with_protocol_shape(self):
        response = self.send_ping()
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(
            set(body), {"id", "seq", "serverTime", "expiresAt"}
        )
        self.assertEqual(body["seq"], 1)
        self.assertTrue(body["id"])
        self.assertEqual(body["expiresAt"] - body["serverTime"], 20_000)

    def test_send_ping_bad_coordinates_is_400(self):
        response = self.send_ping(x=3e8)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"]["code"], "bad_request")

    def test_send_ping_bad_kind_is_400(self):
        response = self.send_ping(kind="explosion")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"]["code"], "bad_request")

    def test_send_ping_bad_room_id_is_400(self):
        response = self.client.post(
            "/api/ping/rooms/zzz/pings",
            json=self.ping_payload(),
            headers=self.auth_headers(),
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"]["code"], "bad_request")

    def test_send_ping_rate_limited_is_429_with_retry_after(self):
        for index in range(5):
            response = self.send_ping()
            self.assertEqual(response.status_code, 201, f"#{index} {response.text}")
        limited = self.send_ping()
        self.assertEqual(limited.status_code, 429, limited.text)
        self.assertEqual(limited.json()["detail"]["code"], "rate_limited")
        self.assertTrue(limited.headers.get("Retry-After"))
        self.assertGreaterEqual(int(limited.headers["Retry-After"]), 1)

    def test_room_full_is_429(self):
        # 房间容量换成 2，直接替换单例中继（比改环境变量更快，且不用重启配置缓存）
        api.set_relay(
            relay_core.PingRelay(
                relay_core.PingConfig(max_pings_per_room=2, rate_limit=1000)
            )
        )
        self.addCleanup(api.reset_state)
        self.assertEqual(self.send_ping().status_code, 201)
        self.assertEqual(self.send_ping().status_code, 201)
        full = self.send_ping()
        self.assertEqual(full.status_code, 429, full.text)
        self.assertEqual(full.json()["detail"]["code"], "room_full")

    # ── 长轮询 ──────────────────────────────────────────────────────────
    def test_fetch_pings_is_200_with_pings(self):
        self.send_ping()
        response = self.client.get(
            f"/api/ping/rooms/{ROOM}/pings?since=0&timeout=0",
            headers=self.auth_headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), {"seq", "serverTime", "pings"})
        self.assertEqual(body["seq"], 1)
        self.assertEqual(len(body["pings"]), 1)
        ping = body["pings"][0]
        self.assertEqual(
            set(ping),
            {
                "seq", "id", "player", "dimension", "x", "y", "z", "kind",
                "targetUuid", "targetName", "serverTime", "expiresAt",
            },
        )
        self.assertEqual(ping["player"], {"uuid": MC_UUID, "name": MC_NAME})
        self.assertEqual(ping["dimension"], OVERWORLD)
        self.assertIsNone(ping["targetUuid"])
        self.assertIsNone(ping["targetName"])

    def test_fetch_pings_timeout_zero_returns_immediately(self):
        started = time.monotonic()
        response = self.client.get(
            f"/api/ping/rooms/{ROOM}/pings?since=0&timeout=0",
            headers=self.auth_headers(),
        )
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["seq"], 0)
        self.assertEqual(body["pings"], [])
        self.assertIn("serverTime", body)

    def test_fetch_pings_since_filters(self):
        self.send_ping()
        self.send_ping()
        response = self.client.get(
            f"/api/ping/rooms/{ROOM}/pings?since=1&timeout=0",
            headers=self.auth_headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([ping["seq"] for ping in response.json()["pings"]], [2])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
