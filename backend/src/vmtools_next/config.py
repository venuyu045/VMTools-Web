"""Configuration loader for VMTools Next.

Loads YAML config with pydantic-settings, supports environment variable
override (VMT_* prefix) and multi-environment files (dev/prod overlay).
"""
from __future__ import annotations

import os
import pathlib
from functools import lru_cache
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


# ── Section models ──────────────────────────────────────────────────────

class GenericConfig(BaseModel):
    enabled: bool = True
    debug_logging: bool = False
    hud_refresh_interval: int = 20


class SafetyConfig(BaseModel):
    max_ops_per_tick: int = 1
    place_interval_ticks: int = 3
    container_interact_interval: int = 10
    teleport_cooldown_seconds: int = 5
    max_pathing_distance: int = 500
    max_failures_before_stop: int = 3
    pause_on_nearby_player: bool = True
    nearby_player_radius: int = 10
    auto_pause_on_disconnect: bool = True


class WarehouseConfig(BaseModel):
    default_scan_range_x: int = 16
    default_scan_range_y: int = 8
    default_scan_range_z: int = 16
    cache_validity_minutes: int = 5
    max_scan_containers: int = 0
    scan_shulker_contents: bool = True
    shulker_recursion_depth: int = 3
    servux_direct_scan: bool = True
    navigate_on_read_fail: bool = False
    servux_read_timeout_ms: int = 800
    servux_prefetch_window: int = 8
    scan_progress_interval: int = 25


class BuildConfig(BaseModel):
    default_layer_height: int = 6
    printer_range: int = 6
    printer_blocks_per_tick: int = 1
    layer_verify_retry_count: int = 3
    auto_continue_next_layer: bool = True


class TravelConfig(BaseModel):
    teleport_threshold: int = 200
    arrival_verify_radius: int = 3
    arrival_timeout_seconds: int = 10
    max_teleports_per_minute: int = 6
    fallback_to_baritone: bool = True


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    # 注意：socket.io ASGIApp 不支持多 worker（需 redis adapter），请保持 1；字段为兼容预留
    workers: int = 1
    debug: bool = False
    database_url: str = "sqlite:///vmtools-next.db"
    secret_key: str = "change-in-production"
    jwt_algorithm: str = "HS256"
    # 业务时区（定时任务 daily/weekly/once 的时间语义基准）。
    # ⚠️ 2026-10-20 审查修复（中等·容器部署下定时任务整体偏移 8 小时）：
    #   定时任务原用 `now.astimezone()` 取**进程时区**，而仓库里没有任何 TZ 设置：
    #   Dockerfile 用 python:3.12-slim（默认 UTC）、docker-compose.yml 只设 VMT_*，
    #   于是容器里"本地时间"= UTC → 用户选「每天 14:30」实际 22:30 才触发。
    #   改为显式配置，不再依赖部署环境的进程时区（裸机/Docker 行为一致）。
    timezone: str = "Asia/Shanghai"
    # CORS：显式白名单（生产部署按实际域名调整），禁止 "*" + allow_credentials 组合（M5）
    cors_origins: list[str] = Field(default_factory=lambda: [
        "http://localhost:5173",
        "http://localhost:8080",
    ])


class MccConfig(BaseModel):
    protocol: str = "mcp"  # mcp | ws
    mcp_endpoint: str = "http://127.0.0.1:33333/mcp"
    mcp_auth_token_env: str = "MCC_MCP_AUTH_TOKEN"
    ws_timeout: float = 10.0
    reconnect_base_delay: float = 1.0
    reconnect_max_delay: float = 60.0
    command_timeout: float = 30.0
    working_dir: str = "/opt/mcc"

    # Remote process management
    instance_root: str = "/opt/vmtools/mcc-instances"
    binary_path: str = "/opt/vmtools/mcc-runtime/MinecraftClient.exe"
    launch_command: list[str] = Field(default_factory=list)
    instance_start_port: int = 33333
    instance_end_port: int = 33352
    max_instances: int = 20
    terminal_buffer_lines: int = 2000
    log_retention_days: int = 14

    # Mineflayer backend ports (used when bot_engine == "mineflayer")
    mineflayer_ws_start_port: int = 44444
    mineflayer_ws_end_port: int = 44500

    # MF 3D viewer：端口分配范围 / 监听地址 / 反代前缀（OpenResty /viewer/ → 127.0.0.1:<port>）
    mineflayer_viewer_start_port: int = 3007
    mineflayer_viewer_end_port: int = 3060
    mineflayer_viewer_preferred_port: int = 3007
    mineflayer_viewer_host: str = "0.0.0.0"
    mineflayer_viewer_prefix: str = ""
    # viewer 渲染范围（chunk）：默认 4（81 chunk，几何计算/带宽较 6 省 ~33%，
    # 2 核 2G 服务器友好）；调大显著增加浏览器负载与首屏时间
    mineflayer_viewer_distance: int = 4


class MineflayerConfig(BaseModel):
    """Mineflayer bot engine configuration."""
    enabled: bool = False  # Master switch to enable mineflayer mode
    default_engine: str = "mcc"  # "mcc" | "mineflayer" — default for new instances
    script_dir: str = "../mineflayer-bots"  # relative to backend/
    node_path: str = "node"  # Node.js executable path
    ws_port_start: int = 44444
    ws_port_end: int = 44500
    status_interval_ms: int = 5000
    yggdrasil_auth_server_url: str = ""  # 默认 Yggdrasil auth server URL

    # ── 地图画建造（map art） ──
    # EssentialsX 传送点名（补货回程用；换服务器需调整，P2-26 去硬编码）
    map_art_warehouse_home: str = "mdssj"   # 仓库传送点（需管理员 /sethome）
    map_art_temp_home: str = "gzqy"         # 临时回家点（建造位置）
    # 补货传送指令默认值（2026-08-14，任务级可覆盖；用户要求三个指令都可自定义）：
    #   go     前往补给点指令（默认传送到仓库传送点）
    #   save   暂存当前位置指令（bot 离开建造位置前保存位置）
    #   return 返回工作位置指令（补货完成后传送回来）
    map_art_supply_go_command: str = "/ehomes mdssj"
    map_art_supply_save_command: str = "/esethomes dth"
    map_art_supply_return_command: str = "/ehomes dth"
    # 固定返回指令（2026-08-14：兜底）——死亡/意外导致临时传送点（dth）不在地图画
    # 区域时，用此固定方式传回地图画（如 /ehomes mapart 或 /tp x y z），可自定义。
    map_art_supply_fixed_return_command: str = "/ehomes mapart"
    # 分区补给箱建议位置的 y 偏移（相对建造层 origin_y；默认 -1 = 地面层，P2-8 防悬空）
    map_art_supply_box_y_offset: int = -1
    # ── 食物补货（2026-08-14 模块A）──
    # 食物类型（默认熟猪排）；食物仓库传送指令；指令后等待时间（ms，指令自带 3s 延迟故等 5s）
    map_art_food_item: str = "minecraft:cooked_porkchop"
    map_art_food_go_command: str = "/res tp lixiangxiang.qwp"
    map_art_food_wait_after_go_ms: int = 5000
    # 进食阈值：饱食度/血量低于该值（满 20）时吃副手食物
    map_art_eat_threshold_food: int = 15
    map_art_eat_threshold_health: int = 15
    # ── 补货决策模型（2026-08-15 第一批重构，迁移 playercontrolpp target/have）──
    # 每种材料一次补货补到多少组：目标持有量 = min(区域剩余需求, N组×64)。
    # ⚠️ 2026-08-15 用户实测纠正：一种颜色一次拿【1 组】（64 个）——4 组会导致
    # 背包被建材塞满、后续颜色装不下。默认 1。
    # 2026-10-28：默认值唯一来源已收敛到 `core/map_art/settings.py`
    #   （`restock_stacks: int = 1`，旧的 getattr 兜底 4 属历史笔误，已随重构删除）。
    map_art_restock_stacks: int = 1  # 1-16
    # 单 bot 两次补货最小间隔（秒）——防"取 64 用 165 块又传"的短周期反复（配合组数配置）
    map_art_min_restock_interval_s: int = 90
    # ⚠️ 2026-08-16 语义统一（P1-1）：本字段 = bot 脚部 y 相对 origin_y 的偏移
    #   （即 bot 站立高度），与 map_art_coordinator._stand_y_for_row 的用法一致。
    #   默认 0 → bot 脚 y = origin_y（第一行站平台顶：平台 68 层顶面 = 脚 69 = origin_y，
    #   与 2026-08-15 实测「bot 脚 69.0 = 站 68 层顶」一致）；后续行站方块顶 = origin_y+1。
    #   旧注释（-1 = 站立方块层 origin_y-1）是另一套语义，已废弃，勿再混用。
    map_art_stand_y_offset: int = 0  # 0 默认 | 旧任务平台结构特殊可 -1/1（谨慎）
    # ── 地图画建造自动睡觉（2026-08-16，用户需求：夜晚受怪物攻击 + 幻翼）──
    # 建造区无领地保护，夜晚怪物会攻击 bot；长时间不睡觉幻翼也会生成。
    # 建造循环内独立协程检测服务器时间，进入可睡窗口（夜晚/雷暴）→ 暂停建造 →
    # 保存当前位置(/esethomes dth) → 寻路到任务配置的床（sleep_x/y/z）睡觉 →
    # 天亮醒来 → /ehomes dth 传回建造点 → 恢复建造。睡觉动作本身重置幻翼计时
    # （即使服务器 doDaylightCycle=false 时间不跳，睡觉仍有效防幻翼）。
    # 夜晚窗口（原版 tick：12542 起可睡，23458 后躺下立刻天亮；默认整窗）
    map_art_sleep_night_start_tick: int = 12542
    map_art_sleep_night_end_tick: int = 23458
    # 睡觉检查间隔（秒，建造循环内协程轮询）
    map_art_sleep_check_interval_s: int = 10
    # 睡下后最短睡眠时长（秒）——保证幻翼计时重置再起床
    map_art_sleep_min_sleep_s: int = 5
    # 等天亮最长等待（秒），超时手动唤醒（doDaylightCycle=false 时间不跳时兜底）
    map_art_sleep_max_wait_s: int = 300
    # 睡觉前传回初始点的传送指令（默认空 → 用 return_fixed，如 /ehomes mapart；
    # 床在初始点附近，站初始点上面可直接点到床，再寻路保险）
    map_art_sleep_go_command: str = ""
    # ── 2026-08-16 P2：寻路/放置/兜底参数收敛（原散落硬编码）──
    # 寻路失败重试次数（baritone+旧 pathfinder 各自重试，默认 2）
    map_art_move_retries: int = 2
    # 放置失败重试次数（默认 3）
    map_art_place_retries: int = 3
    # 移动到位高度校验 y 容差（P1-3 双条件：y 偏差 ≤ 容差 且 脚下实体；默认 1.0 格）
    map_art_verify_y_tol: float = 1.0
    # 传送兜底：每级（临时点/固定指令）内重试次数（默认 3）
    map_art_tp_retry_each: int = 3
    # 传送兜底：总尝试上限（防「传送未生效」死循环空转，默认 8）
    map_art_tp_retry_total: int = 8
    # ── 2026-08-15 环境检测整合（用户需求：维度+位置+附近领地结合）──
    # 地图画建造领地（Residence 名，可自定义；空=不校验）。bot 启动建造前检查
    # 当前所在领地是否匹配（用户主领地 venus，可按任务改成自己的领地）。
    map_art_residence: str = ""
    # 地图画建造维度（mineflayer dimension 值：overworld/world_nether/world_the_end）。
    # bot 在错误维度（如补货后被留在末地仓库）启动时先告警/传送回主世界再建。
    map_art_build_world: str = "overworld"
    # ── 任务调度（2026-08-12，实施计划阶段 2） ──
    # 地图画任务并发上限（同时运行的任务数；排队任务完成后自动启动下一个）
    task_max_concurrent_build: int = 1
    # 调度器轮询间隔（秒）
    task_poll_interval: float = 2.0
    # ── 多Bot列式协作建造（2026-08-19）──
    # 列式建造放置间隔（秒）：多Bot同时放置时服务端节流，默认 0.3s
    map_art_column_place_interval_s: float = 0.3
    # 卡滞判定超时（秒）：连续无位置/动作进展判定卡滞，默认 10
    map_art_stuck_timeout_s: int = 10
    # 死亡 /back 移动锁定超时（秒）：等待回到死亡点，默认 10
    map_art_death_back_timeout_s: int = 10
    # 减员容错离线超时（秒）：Bot离线超过此时间触发编队重组，默认 120
    map_art_bot_offline_timeout_s: int = 120
    # 坠落判定高度差（格）：低于建造层此格数判定坠落，默认 3
    map_art_fall_threshold: int = 3
    # 进度持久化间隔（秒），默认 30
    map_art_checkpoint_interval_s: int = 30
    # ── 缺块自动补放闭环（2026-10-28 重构，修 #51/B7 语义）──
    # 任务判 completed 前：对账 → 补放缺口 → 复对账；仍失败则 failed 并记录剩余清单。
    # 开关（默认开）：关掉则只对账不补放，行为回退到旧版。
    map_art_auto_fix_enabled: bool = True
    # 补放闭环最大轮数（默认 2）：每轮 = 一轮对账 + 一轮补放；
    # 2 轮后仍有缺口即判 failed（防「补 → 对账 → 再补」无限循环）。
    map_art_auto_fix_max_rounds: int = 2
    # 单轮补放批量（默认 256 格）：一轮内最多补多少格，防一次性重扫/重放过大拖死任务。
    map_art_auto_fix_batch: int = 256


class MonitorConfig(BaseModel):
    enabled: bool = True
    collect_interval_seconds: int = 10
    alert_check_interval_seconds: int = 30
    # 预留：webhook / SMTP 通知暂未接入（当前告警走 QQ 群广播 + Socket.IO）
    webhook_url: str = ""
    email_smtp_host: str = ""


class PluginBuiltinConfig(BaseModel):
    """内置插件配置——插件体系仅服务 mineflayer 引擎（MCC 不需要额外插件）。"""
    chat_responder: dict[str, Any] = Field(default_factory=lambda: {
        "enabled": True,
        "commands": {
            "!ping": "pong! ({username})",
            "!pos": "当前位置 {position}",
            "!help": "可用指令: !ping / !pos / !help",
        },
    })


class PluginsConfig(BaseModel):
    enabled: bool = True
    builtin: PluginBuiltinConfig = Field(default_factory=PluginBuiltinConfig)


class QqBotConfig(BaseModel):
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    sandbox: bool = False
    notify_groups: list[str] = Field(default_factory=list)
    mention_openids: list[str] = Field(default_factory=list)
    notify_on_start: bool = True
    notify_on_stop: bool = True
    notify_on_crash: bool = True
    notify_on_chat: bool = False


class QqConnectConfig(BaseModel):
    """QQ 互联（connect.qq.com）网站应用配置——用于「QQ 一键注册/登录」OAuth 认证。

    申请地址：https://connect.qq.com  → 创建「网站应用」→ 配置回调域名为本站域名。
    redirect_uri 必须与 QQ 互联后台配置的回调地址一致。
    """
    enabled: bool = False
    app_id: str = ""       # QQ 互联应用 App ID
    app_key: str = ""      # QQ 互联应用 App Key
    redirect_uri: str = "" # 例如 https://www.venusyu045.cn/api/auth/qq/callback
    state_ttl_seconds: int = 600
    ticket_ttl_seconds: int = 600


class TrackedPlayer(BaseModel):
    name: str
    qq_openid: str = ""


class TrackOwner(BaseModel):
    name: str
    qq_openid: str = ""
    track_players: list[str] = Field(default_factory=list)


class PlayerTrackingConfig(BaseModel):
    enabled: bool = False
    sentinel_instance: str = ""
    owners: list[TrackOwner] = Field(default_factory=list)


class BlueMapConfig(BaseModel):
    """BlueMap website API configuration for player monitoring."""
    enabled: bool = True
    # 生产环境请在 config.yaml 显式配置 api_base_url（避免默认值泄露/耦合具体站点）
    api_base_url: str = ""
    poll_interval_seconds: int = 5
    worlds: list[str] = Field(default_factory=lambda: ["world", "world_nether", "world_the_end"])


class AccessControlConfig(BaseModel):
    """子路径应用的访问白名单（nginx auth_request 用）。

    背景：magpie / dsh 挂在平台子路径下，nginx 通过 auth_request 调后端校验身份。
    默认只有 site_admin 能过；这里可以额外放行指定的 game_id。

    ⚠️ 白名单只影响「能否进入该子路径应用」，不改变用户在平台内的角色权限。
    """
    # magpie Web UI 白名单（除 site_admin 外额外放行的 game_id）
    magpie_whitelist: list[str] = Field(default_factory=list)


class FashionSettings(BaseModel):
    """时装系统（VMFashion Fabric 客户端 mod）配置。

    环境变量：``VMT_FASHION__YGGDRASIL_BASES=https://a,https://b``（逗号/空白分隔）。

    认证只支持两条路径：Mojang 正版会话服务器 + 皮肤站（authlib-injector /
    Yggdrasil）。目标服务器无离线玩家，**没有**离线 UUID 兜底。
    """

    enabled: bool = True

    # ── 认证 ──
    # 皮肤站 API 基地址白名单（可多个）。客户端上报的 authServer 只有落在
    # 这个白名单内才会被使用 —— 白名单外的地址一律忽略（防 SSRF）。
    # NoDecode：pydantic-settings 默认会把 list 类型的 env 值当 JSON 解析，
    # 而运维写的是 `https://a,https://b`，不加 NoDecode 会在进校验器之前就报错。
    yggdrasil_bases: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["https://littleskin.cn/api/yggdrasil"]
    )
    # 正版会话服务器（Mojang / MSA）
    mojang_session_server: str = "https://sessionserver.mojang.com"
    # serverId 有效期（一次性，2 分钟）
    challenge_ttl_seconds: int = 120
    # 签发给 mod 的 fashion token 有效期（默认 24h）
    token_ttl_seconds: int = 86400
    # 所有对外 HTTP 调用的超时（秒），失败即降级
    http_timeout_seconds: float = 5.0
    # 设备码流程（网页绑定 MC 账号）：用户码有效期 + 建议轮询间隔
    device_code_ttl_seconds: int = 600
    device_poll_interval_seconds: int = 3

    # ── 资产 / 目录 ──
    # 资产根目录。相对路径按**仓库根目录**解析（vmtools-next/data/fashion-assets）
    asset_dir: str = "data/fashion-assets"
    # 目录缓存（catalog）TTL
    catalog_cache_ttl_seconds: int = 30
    # 批量 resolve 的单次 UUID 上限（热路径保护）
    resolve_max_uuids: int = 200
    # resolve 结果缓存 TTL（秒）；0 = 关闭
    resolve_cache_ttl_seconds: int = 5
    # 单个玩家的最大穿戴槽位数
    max_loadout_items: int = 8

    # ── 包级下发签名（可选，mod 契约 docs/fashion-api.md §6） ──
    # Ed25519 私钥（base64：裸 32 字节种子或 PKCS#8 DER）。留空 = 清单不带
    # signature 字段（mod 默认宽松模式放行 + 一次 warn；开了严格模式会拒绝）。
    # ⚠️ 私钥只能放服务端（环境变量 / KMS），绝不能进客户端仓库或 git。
    # 需要 `cryptography` 依赖；没装时自动降级为不签名（记一条 warn，不影响 /packs）。
    pack_signing_key_b64: str = ""
    # 与客户端 trusted_keys.json 里的 keys[].id 对应，便于轮换密钥
    pack_signing_key_id: str = "vmtools-official"

    @field_validator("yggdrasil_bases", mode="before")
    @classmethod
    def _split_bases(cls, value: Any) -> Any:
        """兼容 `VMT_FASHION__YGGDRASIL_BASES=a,b` 这种逗号分隔写法。

        pydantic-settings 对 ``list[str]`` 默认按 JSON 解析，运维不会去写
        ``'["https://..."]'``，所以在进校验前先按逗号/空白切分。
        """
        if isinstance(value, str):
            return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
        return value


class PingSettings(BaseModel):
    """标点中继（VMPing Fabric 客户端 mod）配置。

    环境变量：``VMT_PING__MAX_PINGS_PER_ROOM=200``、``VMT_PING__ENABLED=false`` ……

    默认值对齐 ``docs/协议-v1.md`` §6。**纯内存**服务：改配置需要重启进程，
    且多 worker 部署会各进程独立（必须单 worker，见 ``docs/后端实现.md``）。
    """

    enabled: bool = True

    # ── 房间 / 容量 ──
    # 每房间存活标点上限（满了返回 429 room_full）
    max_pings_per_room: int = 200
    # 标点 TTL（秒，惰性清理）
    ping_ttl_seconds: float = 20.0
    # 房间空闲回收（秒）：无活动且无等待者即回收（房间重建后 seq 从 0/1 重新开始）
    room_idle_seconds: float = 60.0
    # 全局房间数上限（超限先回收空闲房间，仍超限返回 429 room_full）
    max_rooms: int = 1000
    # 每房间长轮询等待者上限（超出**立即返回 200 空数组**，协议 v1.1 §6；不是 429）
    max_waiters_per_room: int = 64
    # 全局长轮询并发上限（跨房间；超出 503 server_busy + Retry-After）。
    # 一个等待者 = 一个挂起中的 HTTP 连接，单进程部署必须给个硬上限。
    max_waiters_total: int = 5000

    # ── 限流 ──
    # 每玩家（mcUuid）发送限流：N 条 / 窗口秒（滑动窗口）
    rate_limit: int = 5
    rate_window_seconds: float = 3.0
    # 认证 challenge 限流（每来源 IP）：防刷爆与 fashion 共用的挑战表
    challenge_rate_limit: int = 20
    challenge_rate_window_seconds: float = 60.0

    # ── 长轮询 / token ──
    # 长轮询默认等待与上限（秒）；协议 §5 规定默认 20、上限 25
    wait_timeout_seconds: float = 20.0
    max_wait_timeout_seconds: float = 25.0
    # 签发给 mod 的 ping token 有效期（默认 24h，协议 §2.3）
    token_ttl_seconds: int = 86400


class AppConfig(BaseSettings):
    """Top-level application configuration."""

    model_config = SettingsConfigDict(
        env_prefix="VMT_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    generic: GenericConfig = Field(default_factory=GenericConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    warehouse: WarehouseConfig = Field(default_factory=WarehouseConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)
    travel: TravelConfig = Field(default_factory=TravelConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    mcc: MccConfig = Field(default_factory=MccConfig)
    mineflayer: MineflayerConfig = Field(default_factory=MineflayerConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    qqbot: QqBotConfig = Field(default_factory=QqBotConfig)
    qq_connect: QqConnectConfig = Field(default_factory=QqConnectConfig)
    player_tracking: PlayerTrackingConfig = Field(default_factory=PlayerTrackingConfig)
    bluemap: BlueMapConfig = Field(default_factory=BlueMapConfig)
    access_control: AccessControlConfig = Field(default_factory=AccessControlConfig)
    fashion: FashionSettings = Field(default_factory=FashionSettings)
    ping: PingSettings = Field(default_factory=PingSettings)


# ── Loader ──────────────────────────────────────────────────────────────

def _find_config_dir() -> pathlib.Path:
    """Locate the config directory.

    Priority:
    1. VMT_CONFIG_PATH env var (file or dir)
    2. ../config/ relative to this module (src/vmtools_next/config.py → vmtools-next/config/)
    3. ./config/ cwd fallback
    """
    env_path = os.getenv("VMT_CONFIG_PATH")
    if env_path:
        p = pathlib.Path(env_path)
        if p.is_file():
            return p.parent
        return p
    # src/vmtools_next/config.py → ../../config/
    here = pathlib.Path(__file__).resolve().parent
    candidate = here.parent.parent / "config"
    if candidate.is_dir():
        return candidate
    return pathlib.Path("config")


def get_config_dir() -> pathlib.Path:
    """Public accessor for the resolved config directory (used by hot-reload watcher)."""
    return _find_config_dir()


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base (overlay wins)."""
    result = dict(base)
    for key, val in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _load_yaml(path: pathlib.Path) -> dict:
    if not path.is_file():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def _parse_env_value(value: str) -> Any:
    try:
        parsed = yaml.safe_load(value)
        return value if parsed is None else parsed
    except yaml.YAMLError:
        return value


def _apply_env_overrides(config: dict) -> dict:
    """Apply VMT_* env vars after YAML merge so env wins over files."""
    result = dict(config)
    prefix = "VMT_"
    for raw_key, raw_value in os.environ.items():
        if not raw_key.startswith(prefix) or raw_key in {"VMT_ENV", "VMT_CONFIG_PATH"}:
            continue
        path = raw_key.removeprefix(prefix).lower().split("__")
        if not path or len(path) < 2:
            continue
        cursor = result
        for part in path[:-1]:
            node = cursor.get(part)
            if not isinstance(node, dict):
                node = {}
                cursor[part] = node
            cursor = node
        cursor[path[-1]] = _parse_env_value(raw_value)
    return result


def load_config() -> AppConfig:
    """Load merged configuration from YAML files + environment variables.

    Loading order (later overrides earlier):
    1. config/config.yaml (base)
    2. config/config.{env}.yaml (overlay, env from VMT_ENV or 'dev')
    3. Environment variables (VMT_* prefix, via pydantic-settings)
    """
    config_dir = _find_config_dir()
    base = _load_yaml(config_dir / "config.yaml")

    env = os.getenv("VMT_ENV", "dev")
    overlay = _load_yaml(config_dir / f"config.{env}.yaml")
    merged = _deep_merge(base, overlay)
    merged = _apply_env_overrides(merged)

    cfg = AppConfig(**merged)
    # 安全：JWT 签名密钥严禁使用默认/占位值，防止公开源码默认密钥伪造令牌
    #
    # ⚠️ 2026-10-20 审查修复（严重·占位密钥绕过启动校验）：
    #   原实现是**精确字符串黑名单** `in ("change-in-production", "change-me")`，
    #   但仓库里实际流通的占位值有两个变体，都不在名单内：
    #     · .env.example:  "change-me-to-a-random-string-at-least-32-chars"
    #     · deploy/docker-compose.yml: "change-me-in-production"
    #   而 README 允许直接 `docker compose up -d --build`（不经过 start.sh 的自动
    #   替换兜底）→ 服务会以**仓库里公开可见的字符串**作为 HS256 签名密钥启动，
    #   任何人 `jwt.encode({"sub": <site_admin id>}, "<该公开串>")` 即可伪造最高权限令牌。
    #   改为「长度 + 占位前缀」双重判定，不再依赖枚举具体值。
    _sk = (cfg.server.secret_key or "").strip()
    # 只枚举「明确是占位模板」的前缀，避免误伤合法密钥（如 "secret..." 开头的自定密钥）
    _placeholder_prefixes = (
        "change-", "change_", "changeme", "your-", "your_",
        "example", "placeholder", "todo",
    )
    if len(_sk) < 32 or _sk.lower().startswith(_placeholder_prefixes):
        raise RuntimeError(
            "VMT_SERVER__SECRET_KEY 未设置、长度不足 32 位或仍是占位值，禁止启动。"
            "请用 `openssl rand -hex 32` 生成随机密钥后重试。"
        )
    return cfg


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Cached singleton config accessor."""
    return load_config()


def _replace_toplevel_section(text: str, section: str, new_block: str) -> str:
    """Replace a top-level YAML section block in ``text`` with ``new_block``.

    Other sections (including their comments and formatting) are preserved
    verbatim — only the target section's block is swapped.
    """
    import re

    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(section)}:", line):
            start = i
            break
    if start is None:
        # Section not present — append at the end.
        base = text if text.endswith("\n") else text + "\n"
        return base + new_block.rstrip("\n") + "\n"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].strip() and not lines[j][0].isspace():
            end = j
            break
    return "".join(lines[:start]) + new_block.rstrip("\n") + "\n" + "".join(lines[end:])


def save_mcc_config(instance_root: str | None = None, binary_path: str | None = None,
                    launch_command: list[str] | None = None, instance_start_port: int | None = None,
                    instance_end_port: int | None = None, max_instances: int | None = None,
                    log_retention_days: int | None = None) -> AppConfig:
    """Update MCC section in config.yaml and reload.

    Only the top-level ``mcc`` section is rewritten — comments and other
    sections (qqbot / qq_connect / ...) are preserved verbatim.
    """
    config_dir = _find_config_dir()
    config_path = config_dir / "config.yaml"
    text = config_path.read_text(encoding="utf-8")

    data = yaml.safe_load(text) or {}
    mcc = data.setdefault("mcc", {})
    updates = {
        "instance_root": instance_root,
        "binary_path": binary_path,
        "launch_command": launch_command,
        "instance_start_port": instance_start_port,
        "instance_end_port": instance_end_port,
        "max_instances": max_instances,
        "log_retention_days": log_retention_days,
    }
    for key, value in updates.items():
        if value is not None:
            mcc[key] = value

    new_block = yaml.safe_dump(
        {"mcc": mcc}, allow_unicode=True, default_flow_style=False, sort_keys=False,
    )
    config_path.write_text(_replace_toplevel_section(text, "mcc", new_block), encoding="utf-8")

    get_config.cache_clear()
    return get_config()


def reload_config() -> AppConfig:
    """Force reload config (clears cache). Call after hot-reload."""
    get_config.cache_clear()
    return get_config()
