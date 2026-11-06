"""SQLAlchemy engine, session factory, Base, and Socket.IO server.

Database URL comes from config (server.database_url). On startup,
init_db() creates all tables and ensures the site admin account exists.

Engine and Session are created lazily on first call to init_db() or
get_engine() to avoid importing config at module level.
"""
from __future__ import annotations

import os
import uuid
import pathlib
from typing import Optional

import socketio
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from vmtools_next.infra.logging import get_logger

logger = get_logger("db")

# ── Socket.IO Server ────────────────────────────────────────────────────

# ⚠️ 2026-08-16：放宽 ping 参数（默认 ping_interval=25s/ping_timeout=20s）——
# 地图画建造/补货/对账时后端事件循环繁忙（大量 WS/DB 操作），若 ping 响应
# 延迟超过 pingTimeout 前端 socket 被断开 → 推送（build_progress 等）丢失
# → 前端统计/速度/bot 位置全 0。放宽到 30s/60s（后端阻塞 <30s 不触发）。
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    ping_interval=30,
    ping_timeout=60,
)

# ── Lazy Engine & Session ───────────────────────────────────────────────

Base = declarative_base()

_engine = None
_SessionLocal = None
_DATABASE_URL: Optional[str] = None


def get_engine():
    """Get or create the SQLAlchemy engine (lazy init)."""
    global _engine, _SessionLocal, _DATABASE_URL
    if _engine is not None:
        return _engine

    from vmtools_next.config import get_config
    config = get_config()
    _DATABASE_URL = config.server.database_url

    # Resolve relative SQLite paths to absolute so the DB is found regardless of cwd.
    # Module: backend/src/vmtools_next/data/db.py → 5 levels up → project root.
    #
    # ⚠️ 2026-10-20 审查修复（严重·数据丢失）：
    #   原实现把解析出的 db_rel **丢弃**，无条件改写为
    #   `<project_root>/vmtools-next/vmtools-next.db`。后果：
    #     · Docker 部署（compose 设 VMT_SERVER__DATABASE_URL=sqlite:///data/vmtools-next.db，
    #       卷挂载在 /app/data）→ project_root 解析成 "/" → 实际落点
    #       /vmtools-next/vmtools-next.db，**不在挂载卷内**：容器重建即丢库；
    #     · 任何在 config.yaml 里显式配置的相对路径都被静默忽略。
    #   修复（保持向后兼容）：
    #     · 绝对路径：原样使用；
    #     · 相对路径且**含目录部分**（如 data/vmtools-next.db）：按 cwd 解析
    #       （与 config.yaml.example 注释「相对 cwd」及 Docker WORKDIR=/app 语义一致）；
    #     · 仅裸文件名（默认 sqlite:///vmtools-next.db）：沿用历史落点
    #       `<project_root>/vmtools-next/vmtools-next.db`，保证现网裸机部署不受影响。
    if _DATABASE_URL.startswith("sqlite:///"):
        db_rel = _DATABASE_URL.removeprefix("sqlite:///")
        db_path = pathlib.Path(db_rel)
        if not db_path.is_absolute():
            if db_path.parent != pathlib.Path("."):
                # 显式配置了目录（Docker: data/vmtools-next.db）→ 尊重配置，按 cwd 解析
                db_path = db_path.resolve()
            else:
                # 仅裸文件名 → 历史落点（裸机部署的既有数据库位置）
                project_root = pathlib.Path(__file__).resolve().parent.parent.parent.parent.parent
                db_path = (project_root / "vmtools-next" / "vmtools-next.db").resolve()
            _DATABASE_URL = f"sqlite:///{db_path}"

    connect_args = {}
    if _DATABASE_URL.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    _engine = create_engine(_DATABASE_URL, connect_args=connect_args, echo=False)

    # SQLite 并发优化：WAL 模式（读写不互斥）+ busy_timeout（写锁等待而非秒失败）
    if _DATABASE_URL.startswith("sqlite"):
        from sqlalchemy import event

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA synchronous=NORMAL")
                # 写缓存提至 64MB：大批量扫描落库时减少磁盘同步次数
                cursor.execute("PRAGMA cache_size=-65536")
                cursor.execute("PRAGMA temp_store=MEMORY")
            except Exception:
                pass
            finally:
                cursor.close()

    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    return _engine


def get_session_factory():
    """Get the SessionLocal factory (lazy init)."""
    if _SessionLocal is None:
        get_engine()
    return _SessionLocal


def get_db():
    """FastAPI dependency yielding a SQLAlchemy session."""
    Session = get_session_factory()
    db = Session()
    try:
        yield db
    finally:
        db.close()


# ── Initialization ──────────────────────────────────────────────────────

def init_db() -> None:
    """Create all tables and ensure site admin exists.

    Called from FastAPI lifespan on startup. For schema migrations
    beyond create_all, use Alembic (`alembic upgrade head`).
    """
    engine = get_engine()
    Session = get_session_factory()

    # Ensure SQLite parent directory exists (for Docker volume mounts)
    if _DATABASE_URL and _DATABASE_URL.startswith("sqlite:///"):
        db_path = _DATABASE_URL.removeprefix("sqlite:///")
        pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    # Import all model modules so Base.metadata knows about every table
    from vmtools_next.data.models import (  # noqa: F401
        warehouse, auth, logistics, build, build_map_art, mcc_session, mcc_remote, plugin, monitor, player_tracking,
        lixiangxiang, scheduled_task, fashion,
    )

    Base.metadata.create_all(bind=engine)
    _run_lightweight_migrations(engine)
    _create_indexes(engine)
    _ensure_site_admin(Session)
    _sync_player_tracking(Session)
    _seed_lixiangxiang(Session)
    logger.info("Database initialized: {}", _DATABASE_URL)


def _seed_lixiangxiang(Session) -> None:
    """幂等写入「理想乡」官网种子数据（组织介绍/22 位成员/基地/相册占位）。

    仅当对应表为空时写入；占位渐变 PNG 由标准库生成到 backend/static/lixiangxiang/seed/。
    """
    try:
        from vmtools_next.core.lixiangxiang_seed import seed_lixiangxiang

        static_dir = pathlib.Path(__file__).resolve().parent.parent.parent.parent / "static"
        seed_lixiangxiang(Session, static_dir)
    except Exception as e:
        logger.warning("Lixiangxiang seed failed: {}", e)


def _sync_player_tracking(Session) -> None:
    """Sync player_tracking_owners DB table into the runtime config."""
    try:
        from vmtools_next.config import get_config as _cfg_get, TrackOwner
        import json as _json
        db = Session()
        try:
            from vmtools_next.data.models.player_tracking import PlayerTrackingOwnerModel
            owners_db = db.query(PlayerTrackingOwnerModel).all()
            if not owners_db:
                return
            owners = [
                TrackOwner(
                    name=o.name,
                    qq_openid=o.qq_openid,
                    track_players=_json.loads(o.track_players_json),
                )
                for o in owners_db
            ]
            # Mutate cached config in-place (don't invalidate cache)
            _cfg_get().player_tracking.owners = owners
            logger.info("Player tracking: synced {} owners from DB to config", len(owners_db))
        finally:
            db.close()
    except Exception:
        pass


def _run_lightweight_migrations(engine) -> None:
    """Apply tiny SQLite-compatible additive migrations for pre-Alembic tables."""
    try:
        with engine.connect() as conn:
            # ── 权限组重构：org_member→user、org_admin→admin（幂等 UPDATE）──
            # 合并"用户"与"组织成员"为用户权限组；"组织管理员"更名为"管理员"。
            try:
                conn.execute(text("UPDATE users SET role='user' WHERE role='org_member'"))
                conn.execute(text("UPDATE users SET role='admin' WHERE role='org_admin'"))
            except Exception as _e:
                logger.warning("Role migration check: {}", _e)
            if _DATABASE_URL and _DATABASE_URL.startswith("sqlite"):
                # ── 时装：芒果块定价列（v1.1）放在本块**最前面** ──────────
                # 正式迁移是 alembic 20261105_fashion_mango；这里是「开发机直接跑
                # init_db() 的 create_all、没跑 alembic」的兜底。create_all(checkfirst)
                # **只建缺失的表，不给已存在的表加列**（2026-11-01 事故 §六.106 同款
                # 教训），缺列会让所有时装目录查询直接 OperationalError。
                # ⚠️ 必须放在本块最前：下面任何一条 ALTER 若因「表不存在」抛异常，
                # 会被最外层 except 接住并跳过后续所有迁移；定价列是时装页的硬性
                # 前置，不能排在其它表的迁移后面。
                # ⚠️ 也必须留在这个 sqlite 判断块内：PRAGMA 是 SQLite 专用语法，
                # 放到块外会在 PostgreSQL 上报语法错误，把整个事务拖进 aborted 状态。
                try:
                    f_columns = {
                        row[1]
                        for row in conn.execute(
                            text("PRAGMA table_info(fashion_catalog)")
                        ).fetchall()
                    }
                    if f_columns:  # 空集合 = 表还不存在，交给 create_all 建（自带新列）
                        if "price_mango" not in f_columns:
                            conn.execute(text(
                                "ALTER TABLE fashion_catalog "
                                "ADD COLUMN price_mango INTEGER NOT NULL DEFAULT 0"
                            ))
                        if "purchasable" not in f_columns:
                            conn.execute(text(
                                "ALTER TABLE fashion_catalog "
                                "ADD COLUMN purchasable BOOLEAN NOT NULL DEFAULT 1"
                            ))
                except Exception as _e:
                    logger.warning("fashion mango columns migration check: {}", _e)

                columns = {row[1] for row in conn.execute(text("PRAGMA table_info(mcc_instances)")).fetchall()}
                if "account_profile_id" not in columns:
                    conn.execute(text("ALTER TABLE mcc_instances ADD COLUMN account_profile_id VARCHAR"))
                if "auto_reconnect" not in columns:
                    conn.execute(text("ALTER TABLE mcc_instances ADD COLUMN auto_reconnect BOOLEAN DEFAULT 0"))
                if "bot_engine" not in columns:
                    conn.execute(text("ALTER TABLE mcc_instances ADD COLUMN bot_engine VARCHAR DEFAULT 'mcc'"))
                if "mcp_host" not in columns:
                    conn.execute(text("ALTER TABLE mcc_instances ADD COLUMN mcp_host VARCHAR DEFAULT '127.0.0.1'"))
                if "viewer_port" not in columns:
                    conn.execute(text("ALTER TABLE mcc_instances ADD COLUMN viewer_port INTEGER"))
                if "ws_port" not in columns:
                    conn.execute(text("ALTER TABLE mcc_instances ADD COLUMN ws_port INTEGER"))
                # QQ 互联绑定列（users.qq_openid 唯一——一个 QQ 只能注册一个账号）
                try:
                    u_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()}
                    if "qq_openid" not in u_columns:
                        conn.execute(text("ALTER TABLE users ADD COLUMN qq_openid VARCHAR"))
                    if "qq_nickname" not in u_columns:
                        conn.execute(text("ALTER TABLE users ADD COLUMN qq_nickname VARCHAR"))
                    # 上次上线时间（成员管理「上次上线」列；登录 / 访问网页时更新）
                    if "last_seen_at" not in u_columns:
                        conn.execute(text("ALTER TABLE users ADD COLUMN last_seen_at DATETIME"))
                    conn.execute(text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_qq_openid "
                        "ON users (qq_openid) WHERE qq_openid IS NOT NULL"
                    ))
                except Exception as _e:
                    logger.warning("users qq migration check: {}", _e)
                # scan_status 实时进度扩展列（扫描队列）
                try:
                    s_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(scan_status)")).fetchall()}
                    if "items_scanned" not in s_columns:
                        conn.execute(text("ALTER TABLE scan_status ADD COLUMN items_scanned BIGINT DEFAULT 0"))
                    if "started_at" not in s_columns:
                        conn.execute(text("ALTER TABLE scan_status ADD COLUMN started_at DATETIME"))
                    if "finished_at" not in s_columns:
                        conn.execute(text("ALTER TABLE scan_status ADD COLUMN finished_at DATETIME"))
                except Exception as _e:
                    logger.warning("scan_status migration check: {}", _e)
                # mcc_bots 战斗配置持久化（combat_config JSON，2026-08-11）：
                # {mode, enabled, radius, target_types, interval_ms, stop_durability, move_to_target}
                # bot 重启/面板重开时恢复上次配置（不自动启用）。
                try:
                    b_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(mcc_bots)")).fetchall()}
                    if "combat_config" not in b_columns:
                        conn.execute(text("ALTER TABLE mcc_bots ADD COLUMN combat_config TEXT"))
                    # 手动标记「工作中」（2026-08-13 任务管理）：跨上下线保持
                    if "manual_working" not in b_columns:
                        conn.execute(text("ALTER TABLE mcc_bots ADD COLUMN manual_working BOOLEAN DEFAULT 0"))
                    if "manual_task_name" not in b_columns:
                        conn.execute(text("ALTER TABLE mcc_bots ADD COLUMN manual_task_name VARCHAR DEFAULT ''"))
                    # 永昼睡觉点（2026-08-16）：per-bot 固定睡觉点（床的方块坐标），
                    # 前端「功能」tab 配置；auto_sleep 插件据此每晚前往睡觉。
                    if "sleep_x" not in b_columns:
                        conn.execute(text("ALTER TABLE mcc_bots ADD COLUMN sleep_x INTEGER"))
                    if "sleep_y" not in b_columns:
                        conn.execute(text("ALTER TABLE mcc_bots ADD COLUMN sleep_y INTEGER"))
                    if "sleep_z" not in b_columns:
                        conn.execute(text("ALTER TABLE mcc_bots ADD COLUMN sleep_z INTEGER"))
                except Exception as _e:
                    logger.warning("mcc_bots manual_work migration check: {}", _e)
                # map_art_tasks 睡觉点（2026-08-16，用户需求：夜晚自动睡觉防怪物/幻翼）：
                # 床的方块坐标（世界坐标），建造循环夜晚自动前往睡觉。未配置→跳过。
                try:
                    t_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(map_art_tasks)")).fetchall()}
                    if "sleep_x" not in t_columns:
                        conn.execute(text("ALTER TABLE map_art_tasks ADD COLUMN sleep_x INTEGER"))
                    if "sleep_y" not in t_columns:
                        conn.execute(text("ALTER TABLE map_art_tasks ADD COLUMN sleep_y INTEGER"))
                    if "sleep_z" not in t_columns:
                        conn.execute(text("ALTER TABLE map_art_tasks ADD COLUMN sleep_z INTEGER"))
                except Exception as _e:
                    logger.warning("map_art_tasks sleep migration check: {}", _e)

            # 定时任务：自动上线/下线开关（2026-08-13）
            try:
                s_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(scheduled_tasks)")).fetchall()}
                if "auto_manage" not in s_columns:
                    conn.execute(text("ALTER TABLE scheduled_tasks ADD COLUMN auto_manage BOOLEAN DEFAULT 1"))
            except Exception as _e:
                logger.warning("scheduled_tasks auto_manage migration check: {}", _e)
            # 地图画任务：补货传送指令配置（supply_config JSON，2026-08-14）
            # 形如 {"go":"/ehomes mdssj","save":"/esethomes dth","return":"/ehomes dth"}
            try:
                mt_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(map_art_tasks)")).fetchall()}
                if "supply_config" not in mt_columns:
                    conn.execute(text("ALTER TABLE map_art_tasks ADD COLUMN supply_config TEXT DEFAULT ''"))
                # 地图画任务：投影朝向（rotation，2026-08-14）——0/1/2/3 = 0°/90°/180°/270°
                if "rotation" not in mt_columns:
                    conn.execute(text("ALTER TABLE map_art_tasks ADD COLUMN rotation INTEGER DEFAULT 0"))
                # 地图画任务：食物配置（food_config JSON，2026-08-14 模块A）
                if "food_config" not in mt_columns:
                    conn.execute(text("ALTER TABLE map_art_tasks ADD COLUMN food_config TEXT DEFAULT ''"))
                # 2026-08-19 列式协作模式：建造模式
                # ⚠️ 2026-10-28 决策（B12）：这里的 DEFAULT 'strip' **故意保持 strip，勿"统一"成 column**。
                #   本迁移只影响「旧库首次加 build_mode 列」的那批**历史行**，而历史任务确实是
                #   strip 时代建的（进度、map_art_bot_assignments.region_*、column_index 全是
                #   strip 语义）；标成 column 会让续建时按列池重排、把已建区域当未建 → 数据破坏。
                #   B12 要统一的是「**新建**任务的默认值」，那条链已统一：create_task 显式写
                #   column + resolve_build_mode() 兜底 column + 模型列默认 column。
                #   即：本行是「历史数据语义」，不是「默认值」。
                # 历史行语义保持 strip（strip 时代的进度/分区数据），新建任务默认值由 create_task + resolve_build_mode 统一为 column
                if "build_mode" not in mt_columns:
                    conn.execute(text("ALTER TABLE map_art_tasks ADD COLUMN build_mode VARCHAR DEFAULT 'strip'"))
            except Exception as _e:
                logger.warning("map_art_tasks supply_config migration check: {}", _e)
            # 2026-08-19 列式协作模式：bot分配扩展
            try:
                ba_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(map_art_bot_assignments)")).fetchall()}
                if "column_index" not in ba_columns:
                    conn.execute(text("ALTER TABLE map_art_bot_assignments ADD COLUMN column_index INTEGER"))
                if "row_direction" not in ba_columns:
                    conn.execute(text("ALTER TABLE map_art_bot_assignments ADD COLUMN row_direction INTEGER DEFAULT 1"))
            except Exception as _e:
                logger.warning("map_art_bot_assignments column migration check: {}", _e)
            # 地图画方块坐标语义统一（2026-08-16 P0-5/B4）：
            # map_art_block_states 历史混存两套坐标（create 时投影相对坐标 0..size-1 /
            # 运行 flush 时世界坐标 origin+相对）。统一为世界坐标。
            # 幂等保护：用 SQLite PRAGMA user_version（0→1 只执行一次），
            # 且按「相对坐标特征行」过滤（x/z ∈ [0,size) 且 placed=False，绝大多数
            # 非零 origin 地图画 origin 远离 0，区间不重叠可安全区分）。
            try:
                cur_v = conn.execute(text("PRAGMA user_version")).fetchone()
                cur_ver = int(cur_v[0]) if cur_v and cur_v[0] is not None else 0
                if cur_ver < 1:
                    mt_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(map_art_tasks)")).fetchall()}
                    bs_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(map_art_block_states)")).fetchall()}
                    if ("origin_x" in mt_columns and "origin_z" in mt_columns
                            and "task_id" in bs_columns and "placed" in bs_columns):
                        rows = conn.execute(text(
                            "SELECT task_id, origin_x, origin_y, origin_z, "
                            "projection_size_x, projection_size_z FROM map_art_tasks"
                        )).fetchall()
                        migrated = 0
                        for task_id, ox, oy, oz, sx, sz in rows:
                            if not ox and not oy and not oz:
                                continue  # origin 全 0：相对=世界，无需转换
                            sx = sx or 0
                            sz = sz or 0
                            if sx <= 0 or sz <= 0:
                                continue  # 尺寸未知无法判定相对特征，跳过（对账兜底）
                            n = conn.execute(text(
                                "UPDATE map_art_block_states "
                                "SET x = x + :ox, y = y + :oy, z = z + :oz "
                                "WHERE task_id = :tid AND placed = 0 "
                                "AND x >= 0 AND x < :sx AND z >= 0 AND z < :sz"
                            ), {"ox": ox or 0, "oy": oy or 0, "oz": oz or 0,
                                "sx": sx, "sz": sz, "tid": task_id}).rowcount
                            migrated += n
                        if migrated:
                            logger.info("MapArt block coords migrated to world coords: {} rows", migrated)
                    conn.execute(text("PRAGMA user_version = 1"))
            except Exception as _e:
                logger.warning("map_art_block_states coord migration check: {}", _e)
            conn.commit()
    except Exception as e:
        logger.warning("Lightweight migration check: {}", e)


def _create_indexes(engine) -> None:
    """Create additional indexes and utility tables that aren't in ORM definitions."""
    try:
        with engine.connect() as conn:
            # bluemap_cache table (key-value store for BlueMap data persistence)
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS bluemap_cache ("
                "  cache_key TEXT PRIMARY KEY,"
                "  cache_data TEXT,"
                "  updated_at REAL"
                ")"
            ))
            for sql in (
                "CREATE INDEX IF NOT EXISTS idx_build_tasks_status ON build_tasks (status)",
                "CREATE INDEX IF NOT EXISTS idx_build_tasks_bot ON build_tasks (bot_id)",
                "CREATE INDEX IF NOT EXISTS idx_metrics_snapshot_ts ON metrics_snapshot (timestamp, metric_name)",
                "CREATE INDEX IF NOT EXISTS idx_mcc_instances_status ON mcc_instances (status)",
                "CREATE INDEX IF NOT EXISTS idx_mcc_instances_account_profile ON mcc_instances (account_profile_id)",
                "CREATE INDEX IF NOT EXISTS idx_mcc_terminal_logs_instance_seq ON mcc_terminal_logs (instance_id, seq)",
            ):
                conn.execute(text(sql))
            conn.commit()
    except Exception as e:
        logger.warning("Index creation check: {}", e)


def _ensure_site_admin(Session) -> None:
    """Create or update the site admin account from env vars."""
    import bcrypt
    from vmtools_next.data.models.auth import UserModel

    admin_game_id = os.getenv("SITE_ADMIN_GAME_ID", "VenusYu")
    # 安全：不再提供默认密码（未设置时走下方随机生成分支）；已存在的
    # site_admin 密码绝不随重启被重置，避免「改密失效 + 默认密码后门」。
    admin_password = os.getenv("SITE_ADMIN_PASSWORD", "")

    db = Session()
    try:
        existing = db.query(UserModel).filter(UserModel.role == "site_admin").first()
        if existing:
            changed = False
            if existing.game_id != admin_game_id:
                existing.game_id = admin_game_id
                existing.display_name = admin_game_id
                changed = True
            if changed:
                db.commit()
                logger.info("Site admin updated: {}", admin_game_id)
            return

        if not admin_password:
            admin_password = uuid.uuid4().hex[:12]
            # ⚠️ 2026-10-20 审查修复（中等·初始管理员口令写入日志）：
            #   原实现把口令明文打进日志。日志会流向 journalctl（systemd）、
            #   docker logs、logs/*.log 落盘，通常比 .env 权限更宽（运维/监控/
            #   日志采集系统都能读）且长期留存 —— 等于把「首次口令」的暴露面
            #   从受保护文件挪到了日志。
            #   修复：写入 0600 文件，日志只提示路径；debug 模式才打印明文。
            _pw_file = None
            try:
                import pathlib as _pathlib
                from vmtools_next.config import get_config_dir
                _cfg_dir = _pathlib.Path(get_config_dir())
                _pw_file = _cfg_dir / ".site_admin_password"
                _pw_file.write_text(
                    f"{admin_game_id}\n{admin_password}\n", encoding="utf-8"
                )
                try:
                    _pw_file.chmod(0o600)
                except OSError:
                    pass
            except Exception as _e:  # 写文件失败不应阻断启动
                logger.warning("Failed to persist initial admin password file: {}", _e)
                _pw_file = None

            if _pw_file is not None:
                logger.warning(
                    "Generated site admin password for '{}' — see {} (chmod 600)",
                    admin_game_id, _pw_file,
                )
                from vmtools_next.config import get_config as _gc
                if getattr(_gc().server, "debug", False):
                    logger.warning("DEBUG: site admin password = {}", admin_password)
            else:
                # 兜底：无文件可写时只能打印（比丢密码导致无法登录更可接受）
                logger.warning("Generated site admin password for '{}': {}",
                               admin_game_id, admin_password)

        password_hash = bcrypt.hashpw(
            admin_password.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")
        admin = UserModel(
            id=str(uuid.uuid4()),
            game_id=admin_game_id,
            password_hash=password_hash,
            display_name=admin_game_id,
            role="site_admin",
            status="approved",
            organization_id=None,
        )
        db.add(admin)
        db.commit()
        logger.info("Site admin account created: {}", admin_game_id)
    except Exception as e:
        db.rollback()
        logger.error("Failed to create site admin: {}", e)
    finally:
        db.close()
