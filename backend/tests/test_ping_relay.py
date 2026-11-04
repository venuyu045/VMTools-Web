"""标点中继核心（``core/ping_relay.py``）单测 —— 不依赖 fastapi / pydantic / 网络。

覆盖：房间键规范化、标点 TTL 惰性清理、每房间环形缓冲上限、限流滑动窗口、
长轮询超时 / 唤醒 / 维度过滤 / 等待者上限、房间空闲回收与全局房间数上限。

跑法::

    cd backend && python3 -m unittest tests.test_ping_relay -v
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
import threading
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, os.pardir, "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from vmtools_next.core import ping_relay as relay  # noqa: E402

ROOM_A = "0123456789abcdef"
ROOM_B = "fedcba9876543210"
ROOM_C = "aaaaaaaaaaaaaaaa"
UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"
WALL_BASE = 1_730_000_000.0
OVERWORLD = "minecraft:overworld"
NETHER = "minecraft:the_nether"


# ── 夹具 ────────────────────────────────────────────────────────────────

class FakeClock:
    """可控单调时钟；wall 由它派生，保证 ``serverTime`` / ``expiresAt`` 可断言。"""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def wall(self) -> float:
        return WALL_BASE + (self.now - 1000.0)

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RelayFixture:
    """一个小容量中继 + 可控时钟，测试里不用重复摆参数。"""

    def __init__(self, **overrides):
        self.clock = FakeClock()
        params = {"rate_limit": 1000}
        params.update(overrides)
        self.config = relay.PingConfig(**params)
        self.relay = relay.PingRelay(
            self.config, clock=self.clock, wall_clock=self.clock.wall
        )

    def publish(self, room: str = ROOM_A, uuid: str = UUID_A, **overrides):
        params = {
            "dimension": OVERWORLD,
            "x": 1.5,
            "y": 64.0,
            "z": -2.5,
            "kind": "default",
            "player_name": "Steve",
        }
        params.update(overrides)
        return self.relay.publish(room_id=room, player_uuid=uuid, **params)


def _code(exc: BaseException) -> str:
    return str(getattr(exc, "code", ""))


# ── 房间键 ──────────────────────────────────────────────────────────────

class TestRoomId(unittest.TestCase):
    def test_accepts_lowercase_hex16(self):
        self.assertEqual(relay.normalize_room_id(ROOM_A), ROOM_A)

    def test_normalizes_uppercase_and_spaces(self):
        self.assertEqual(relay.normalize_room_id("  0123456789ABCDEF "), ROOM_A)

    def test_rejects_bad_shapes(self):
        for value in ("", "  ", "0123456789abcde", "0123456789abcdef0", "0123456789abcdeg", None):
            with self.subTest(value=value):
                with self.assertRaises(relay.PingRelayError) as ctx:
                    relay.normalize_room_id(value)
                self.assertEqual(_code(ctx.exception), "bad_request")
                self.assertEqual(ctx.exception.status_code, 400)


# ── 发送 / 形状 ────────────────────────────────────────────────────────

class TestPublish(unittest.TestCase):
    def test_seq_is_monotonic_and_id_is_opaque(self):
        fixture = RelayFixture()
        first = fixture.publish()
        second = fixture.publish()
        self.assertEqual((first.seq, second.seq), (1, 2))
        self.assertEqual(len(first.id), 32)  # secrets.token_hex(16)
        self.assertNotEqual(first.id, second.id)

    def test_server_time_uses_wall_clock_and_ttl(self):
        fixture = RelayFixture()
        ping = fixture.publish()
        self.assertEqual(ping.server_time, int(WALL_BASE * 1000))
        self.assertEqual(ping.expires_at - ping.server_time, 20_000)

    def test_to_dict_matches_protocol_shape(self):
        fixture = RelayFixture()
        ping = fixture.publish(
            target_uuid="AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE", target_name=" Notch "
        )
        payload = ping.to_dict()
        self.assertEqual(
            set(payload),
            {
                "seq", "id", "player", "dimension", "x", "y", "z", "kind",
                "targetUuid", "targetName", "serverTime", "expiresAt",
            },
        )
        self.assertEqual(payload["player"], {"uuid": UUID_A, "name": "Steve"})
        self.assertEqual(payload["targetUuid"], "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertEqual(payload["targetName"], "Notch")
        self.assertEqual(payload["serverTime"], int(WALL_BASE * 1000))

    def test_client_time_is_ignored(self):
        fixture = RelayFixture()
        ping = fixture.publish(client_time=1)
        self.assertEqual(ping.server_time, int(WALL_BASE * 1000))


# ── TTL ────────────────────────────────────────────────────────────────

class TestTtl(unittest.TestCase):
    def test_expired_pings_are_pruned_lazily(self):
        fixture = RelayFixture()
        fixture.publish()
        self.assertEqual(len(fixture.relay.snapshot(ROOM_A).pings), 1)

        fixture.clock.advance(19.9)
        self.assertEqual(len(fixture.relay.snapshot(ROOM_A).pings), 1)

        fixture.clock.advance(0.2)  # 越过 20s
        batch = fixture.relay.snapshot(ROOM_A)
        self.assertEqual(batch.pings, [])
        self.assertEqual(fixture.relay.stats()["pings"], 0)

    def test_expired_pings_free_room_capacity(self):
        fixture = RelayFixture(max_pings_per_room=2)
        fixture.publish()
        fixture.publish()
        with self.assertRaises(relay.PingRelayError):
            fixture.publish()
        fixture.clock.advance(21)
        ping = fixture.publish()  # 过期条目已被清掉，容量恢复
        self.assertEqual(ping.seq, 3)


# ── 环形缓冲 / 房间满 ──────────────────────────────────────────────────

class TestRingBuffer(unittest.TestCase):
    def test_default_capacity_is_200(self):
        fixture = RelayFixture()
        self.assertEqual(fixture.publish().seq, 1)
        room = fixture.relay._rooms[ROOM_A]
        self.assertEqual(room.pings.maxlen, 200)

    def test_room_full_when_buffer_is_full(self):
        fixture = RelayFixture(max_pings_per_room=3)
        for _ in range(3):
            fixture.publish()
        with self.assertRaises(relay.PingRelayError) as ctx:
            fixture.publish()
        self.assertEqual(_code(ctx.exception), "room_full")
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.retry_after, 1)

    def test_buffer_never_grows_past_maxlen(self):
        fixture = RelayFixture(max_pings_per_room=3)
        for _ in range(3):
            fixture.publish()
        self.assertEqual(len(fixture.relay._rooms[ROOM_A].pings), 3)
        self.assertEqual(fixture.relay.stats()["pings"], 3)


# ── 限流 ────────────────────────────────────────────────────────────────

class TestRateLimit(unittest.TestCase):
    def test_default_is_5_per_3_seconds(self):
        fixture = RelayFixture(rate_limit=5, rate_window_seconds=3.0)
        for _ in range(5):
            fixture.publish()
        with self.assertRaises(relay.PingRelayError) as ctx:
            fixture.publish()
        self.assertEqual(_code(ctx.exception), "rate_limited")
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertGreaterEqual(ctx.exception.retry_after, 1)
        self.assertLessEqual(ctx.exception.retry_after, 3)

    def test_window_slides_after_expiry(self):
        fixture = RelayFixture(rate_limit=2, rate_window_seconds=3.0)
        fixture.publish()
        fixture.publish()
        fixture.clock.advance(3.1)
        ping = fixture.publish()  # 窗口滑过，重新可发
        self.assertEqual(ping.seq, 3)
        self.assertEqual(fixture.relay.stats()["pings"], 3)  # 标点 TTL 20s，还没过期

    def test_limits_are_per_player(self):
        fixture = RelayFixture(rate_limit=1, rate_window_seconds=3.0)
        fixture.publish(uuid=UUID_A)
        with self.assertRaises(relay.PingRelayError):
            fixture.publish(uuid=UUID_A)
        fixture.publish(uuid=UUID_B)  # 另一个玩家不受影响
        self.assertEqual(fixture.relay.snapshot(ROOM_A).seq, 2)

    def test_rate_limited_request_does_not_create_room(self):
        fixture = RelayFixture(rate_limit=1, rate_window_seconds=60.0)
        fixture.publish(room=ROOM_A)
        with self.assertRaises(relay.PingRelayError):
            fixture.publish(room=ROOM_B)  # 同一玩家，第二次就被限流
        self.assertEqual(fixture.relay.stats()["rooms"], 1)


class TestLimiter(unittest.TestCase):
    def test_check_records_and_raises_with_retry_after(self):
        limiter = relay.SlidingWindowLimiter(2, 10.0)
        limiter.check("k", now=100.0)
        limiter.check("k", now=101.0)
        with self.assertRaises(relay.PingRelayError) as ctx:
            limiter.check("k", now=104.0)
        self.assertEqual(ctx.exception.code, "rate_limited")
        self.assertEqual(ctx.exception.retry_after, 6)  # 最早那次 100 → 110 才滑出

    def test_reset_clears_hits(self):
        limiter = relay.SlidingWindowLimiter(1, 10.0)
        limiter.check("k", now=100.0)
        limiter.reset()
        limiter.check("k", now=100.5)  # 不抛
        self.assertEqual(len(limiter), 1)


# ── 房间生命周期 ────────────────────────────────────────────────────────

class TestRoomLifecycle(unittest.TestCase):
    def test_idle_room_is_reclaimed_and_seq_restarts(self):
        fixture = RelayFixture(room_idle_seconds=60.0)
        fixture.publish()
        fixture.clock.advance(61)
        removed = fixture.relay.prune()
        self.assertEqual(removed["rooms"], 1)
        self.assertEqual(fixture.relay.stats()["rooms"], 0)
        self.assertEqual(fixture.publish().seq, 1)  # 房间重建，seq 从头开始

    def test_room_with_waiter_is_not_reclaimed(self):
        fixture = RelayFixture(room_idle_seconds=60.0)

        async def scenario():
            task = asyncio.create_task(fixture.relay.wait_for_pings(ROOM_A, 0, 2.0))
            await asyncio.sleep(0.02)
            fixture.clock.advance(1000)  # 远超空闲阈值
            fixture.relay.prune()
            stats = fixture.relay.stats()
            fixture.publish()  # 唤醒等待者
            batch = await task
            return stats, batch

        stats, batch = asyncio.run(scenario())
        self.assertEqual(stats["rooms"], 1, "有等待者的房间不能被回收，否则唤醒丢失")
        self.assertEqual(batch.seq, 1)

    def test_reclaimed_room_seq_restarts_from_zero(self):
        """房间回收 → 重建后 ``seq`` 归零（**不是**全局单调），GET 先看到 seq=0。"""
        fixture = RelayFixture(room_idle_seconds=60.0)
        fixture.publish()
        fixture.publish()
        self.assertEqual(fixture.relay.snapshot(ROOM_A).seq, 2)

        fixture.clock.advance(61)
        fixture.relay.prune()
        batch = fixture.relay.snapshot(ROOM_A)  # 房间已回收：不存在 → seq 0
        self.assertEqual((batch.seq, batch.pings), (0, []))

        self.assertEqual(fixture.publish().seq, 1)  # 重建后从 1 开始，不复用旧 seq

    def test_global_room_cap(self):
        fixture = RelayFixture(max_rooms=2)
        fixture.publish(room=ROOM_A)
        fixture.publish(room=ROOM_B)
        with self.assertRaises(relay.PingRelayError) as ctx:
            fixture.publish(room=ROOM_C)
        self.assertEqual(_code(ctx.exception), "room_full")
        self.assertEqual(ctx.exception.status_code, 429)

    def test_global_room_cap_reclaims_idle_rooms_first(self):
        fixture = RelayFixture(max_rooms=2, room_idle_seconds=60.0)
        fixture.publish(room=ROOM_A)
        fixture.publish(room=ROOM_B)
        fixture.clock.advance(61)
        fixture.publish(room=ROOM_C)  # 先回收 A/B，再建 C
        self.assertEqual(fixture.relay.stats()["rooms"], 1)

    def test_reset_clears_everything(self):
        fixture = RelayFixture()
        fixture.publish()
        fixture.relay.reset()
        self.assertEqual(fixture.relay.stats(), {"rooms": 0, "pings": 0, "waiters": 0})

    def test_idle_room_is_reclaimed_on_publish_path(self):
        """生产没有周期任务：发送路径每 ``PRUNE_EVERY_PUBLISHES`` 次兜底回收空闲房间。

        回归（v2.0.1 第 7 轮 L1）：``prune()`` 只是运维/单测探针，从未被生产代码调用，
        于是 ``room_idle_seconds`` 只在「全局房间数打满、要建新房间」时才生效 ——
        被丢下的房间会带着已过期的标点长期驻留内存（上限 1000 房间 × 200 标点）。
        """
        fixture = RelayFixture(room_idle_seconds=60.0)
        fixture.publish(room=ROOM_B)  # 被丢下的房间
        fixture.clock.advance(61)  # 超过空闲阈值
        for _ in range(relay.PRUNE_EVERY_PUBLISHES):
            fixture.publish(room=ROOM_A)  # 另一个房间继续活动
        self.assertEqual(fixture.relay.stats()["rooms"], 1, "空闲房间应被兜底回收")
        self.assertEqual(fixture.relay.snapshot(ROOM_A).seq, relay.PRUNE_EVERY_PUBLISHES)

    def test_publish_path_prune_keeps_active_room_seq(self):
        """兜底回收不能误伤当前房间（误回收会让 seq 归零，客户端当成房间被回收）。"""
        fixture = RelayFixture(room_idle_seconds=60.0)
        fixture.publish(room=ROOM_A)
        fixture.clock.advance(61)
        last = None
        for _ in range(relay.PRUNE_EVERY_PUBLISHES):
            last = fixture.publish(room=ROOM_A)
        self.assertEqual(fixture.relay.stats()["rooms"], 1)
        self.assertEqual(last.seq, 1 + relay.PRUNE_EVERY_PUBLISHES)


# ── 长轮询 ──────────────────────────────────────────────────────────────

class TestWaitForPings(unittest.TestCase):
    def test_returns_immediately_when_data_exists(self):
        fixture = RelayFixture()
        fixture.publish()
        started = time.monotonic()
        batch = asyncio.run(fixture.relay.wait_for_pings(ROOM_A, 0, 5.0))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual((batch.seq, len(batch.pings)), (1, 1))

    def test_since_filters_already_seen(self):
        fixture = RelayFixture()
        first = fixture.publish()
        second = fixture.publish()
        batch = fixture.relay.snapshot(ROOM_A, since=first.seq)
        self.assertEqual([ping.seq for ping in batch.pings], [second.seq])
        self.assertEqual(batch.seq, second.seq)

    def test_timeout_returns_empty_batch(self):
        fixture = RelayFixture()
        started = time.monotonic()
        batch = asyncio.run(fixture.relay.wait_for_pings(ROOM_A, 0, 0.05))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.04)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(batch.pings, [])
        self.assertEqual(batch.seq, 0)

    def test_zero_timeout_returns_immediately(self):
        fixture = RelayFixture()
        started = time.monotonic()
        batch = asyncio.run(fixture.relay.wait_for_pings(ROOM_A, 0, 0))
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(batch.pings, [])

    def test_timeout_is_clamped_to_max(self):
        fixture = RelayFixture(max_wait_timeout_seconds=0.05, wait_timeout_seconds=0.05)
        started = time.monotonic()
        asyncio.run(fixture.relay.wait_for_pings(ROOM_A, 0, 100.0))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_publish_wakes_waiter(self):
        fixture = RelayFixture()

        async def scenario():
            task = asyncio.create_task(fixture.relay.wait_for_pings(ROOM_A, 0, 5.0))
            await asyncio.sleep(0.02)
            fixture.publish()
            return await task

        batch = asyncio.run(scenario())
        self.assertEqual(batch.seq, 1)
        self.assertEqual(len(batch.pings), 1)
        self.assertEqual(fixture.relay.stats()["waiters"], 0)

    def test_publish_from_another_thread_wakes_waiter(self):
        fixture = RelayFixture()

        async def scenario():
            task = asyncio.create_task(fixture.relay.wait_for_pings(ROOM_A, 0, 5.0))
            await asyncio.sleep(0.05)
            thread = threading.Thread(target=fixture.publish)
            thread.start()
            try:
                return await task
            finally:
                thread.join()

        batch = asyncio.run(scenario())
        self.assertEqual(batch.seq, 1)

    def test_dimension_filter_keeps_waiting_for_matching_dimension(self):
        fixture = RelayFixture()

        async def scenario():
            task = asyncio.create_task(
                fixture.relay.wait_for_pings(ROOM_A, 0, 5.0, NETHER)
            )
            await asyncio.sleep(0.02)
            fixture.publish(dimension=OVERWORLD)  # 别的维度：不返回
            await asyncio.sleep(0.02)
            still_waiting = not task.done()
            fixture.publish(dimension=NETHER)
            return still_waiting, await task

        still_waiting, batch = asyncio.run(scenario())
        self.assertTrue(still_waiting)
        self.assertEqual([ping.dimension for ping in batch.pings], [NETHER])

    def test_room_waiter_cap_returns_empty_batch_immediately(self):
        """协议 v1.1 §6：每房间等待者超限 → **立即返回空数组**（不是 429）。"""
        fixture = RelayFixture(max_waiters_per_room=1)

        async def scenario():
            first = asyncio.create_task(fixture.relay.wait_for_pings(ROOM_A, 0, 5.0))
            await asyncio.sleep(0.02)
            started = time.monotonic()
            try:
                batch = await fixture.relay.wait_for_pings(ROOM_A, 0, 5.0)
            finally:
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
            return batch, time.monotonic() - started

        batch, elapsed = asyncio.run(scenario())
        self.assertEqual((batch.seq, batch.pings), (0, []))
        self.assertLess(elapsed, 0.5, "超出等待者上限必须立刻返回，不能真等 5s")

    def test_publish_between_snapshot_and_registration_is_not_lost(self):
        """竞态回归：注册等待者之前到达的标点不能被「丢唤醒」拖到超时。"""
        fixture = RelayFixture()
        original_add = fixture.relay._try_add_waiter

        def racy_add_waiter(room_key, loop):
            fixture.publish()  # 模拟「snapshot 之后、注册之前」有发送方写入
            return original_add(room_key, loop)

        fixture.relay._try_add_waiter = racy_add_waiter
        started = time.monotonic()
        batch = asyncio.run(fixture.relay.wait_for_pings(ROOM_A, 0, 1.0))
        elapsed = time.monotonic() - started
        self.assertEqual(batch.seq, 1)
        self.assertLess(elapsed, 0.5, "注册后必须立刻看到新标点，而不是等到超时")

    def test_global_waiter_cap_returns_server_busy(self):
        """全局长轮询并发上限（跨房间）：超出 503 server_busy（不是 room_full）。"""
        fixture = RelayFixture(max_waiters_total=1)

        async def scenario():
            first = asyncio.create_task(fixture.relay.wait_for_pings(ROOM_A, 0, 5.0))
            await asyncio.sleep(0.02)
            try:
                with self.assertRaises(relay.PingRelayError) as ctx:
                    await fixture.relay.wait_for_pings(ROOM_B, 0, 5.0)  # 另一个房间也算全局
                return ctx.exception
            finally:
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)

        error = asyncio.run(scenario())
        self.assertEqual(error.code, "server_busy")
        self.assertEqual(error.status_code, 503)
        self.assertGreaterEqual(error.retry_after, 1)

    def test_waiter_can_be_cancelled(self):
        fixture = RelayFixture()

        async def scenario():
            task = asyncio.create_task(fixture.relay.wait_for_pings(ROOM_A, 0, 5.0))
            await asyncio.sleep(0.02)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return fixture.relay.stats()

        stats = asyncio.run(scenario())
        self.assertEqual(stats["waiters"], 0, "取消后等待者必须从房间摘掉")


# ── 参数校验 ────────────────────────────────────────────────────────────

class TestValidation(unittest.TestCase):
    def _assert_bad_request(self, fixture, **overrides):
        with self.assertRaises(relay.PingRelayError) as ctx:
            fixture.publish(**overrides)
        self.assertEqual(_code(ctx.exception), "bad_request")
        self.assertEqual(ctx.exception.status_code, 400)
        return ctx.exception

    def test_coordinates_must_be_finite(self):
        fixture = RelayFixture()
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self._assert_bad_request(fixture, x=value)
                self._assert_bad_request(fixture, y=value)
                self._assert_bad_request(fixture, z=value)

    def test_coordinate_ranges(self):
        fixture = RelayFixture()
        self._assert_bad_request(fixture, x=3e7 + 1)
        self._assert_bad_request(fixture, z=-3e7 - 1)
        self._assert_bad_request(fixture, y=2049)
        # 边界值合法
        fixture.publish(x=3e7, y=2048, z=-3e7)

    def test_kind_whitelist(self):
        fixture = RelayFixture()
        self._assert_bad_request(fixture, kind="explosion")
        self.assertEqual(fixture.publish(kind="danger").kind, "danger")
        self.assertEqual(fixture.publish(kind="item").kind, "item")

    def test_dimension_limits(self):
        fixture = RelayFixture()
        self._assert_bad_request(fixture, dimension="")
        self._assert_bad_request(fixture, dimension="x" * 129)
        self.assertEqual(fixture.publish(dimension="d" * 128).dimension, "d" * 128)

    def test_target_name_limit(self):
        fixture = RelayFixture()
        self._assert_bad_request(fixture, target_name="n" * 65)
        self.assertEqual(fixture.publish(target_name="n" * 64).target_name, "n" * 64)

    def test_target_uuid_must_be_valid(self):
        fixture = RelayFixture()
        self._assert_bad_request(fixture, target_uuid="not-a-uuid")
        self.assertIsNone(fixture.publish(target_uuid="").target_uuid)

    def test_player_uuid_must_be_valid(self):
        fixture = RelayFixture()
        with self.assertRaises(relay.PingRelayError):
            fixture.publish(uuid="nope")

    def test_invalid_params_do_not_consume_rate_budget(self):
        fixture = RelayFixture(rate_limit=1, rate_window_seconds=60.0)
        for _ in range(3):
            with self.assertRaises(relay.PingRelayError):
                fixture.publish(x=float("nan"))
        fixture.publish()  # 限额还没被消耗
        self.assertEqual(fixture.relay.stats()["pings"], 1)


# ── 配置 ────────────────────────────────────────────────────────────────

class TestPingConfig(unittest.TestCase):
    def test_defaults(self):
        """默认值：协议 §6 的容量 + Lead 决策收紧后的单进程安全值。"""
        config = relay.PingConfig()
        self.assertEqual(config.max_pings_per_room, 200)
        self.assertEqual(config.ping_ttl_seconds, 20.0)
        self.assertEqual(config.room_idle_seconds, 60.0)
        self.assertEqual(config.max_rooms, 1000)
        self.assertEqual(config.max_waiters_per_room, 64)
        self.assertEqual(config.max_waiters_total, 5000)
        self.assertEqual((config.rate_limit, config.rate_window_seconds), (5, 3.0))
        self.assertEqual(config.wait_timeout_seconds, 20.0)
        self.assertEqual(config.max_wait_timeout_seconds, 25.0)

    def test_from_settings_reads_pydantic_like_object(self):
        import types

        settings = types.SimpleNamespace(max_pings_per_room=7, rate_limit=2)
        config = relay.PingConfig.from_settings(settings)
        self.assertEqual(config.max_pings_per_room, 7)
        self.assertEqual(config.rate_limit, 2)
        self.assertEqual(config.ping_ttl_seconds, 20.0)  # 缺项用默认值

    def test_from_settings_tolerates_missing_settings(self):
        config = relay.PingConfig.from_settings(None)
        self.assertEqual(config.max_rooms, 1000)
        self.assertEqual(config.max_waiters_total, 5000)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
