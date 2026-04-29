"""异步 SQLAlchemy 会话与 Base。"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
engine = create_async_engine(
    _settings.database_url,
    echo=False,
    pool_pre_ping=True,
    future=True,
)


# D4 修复：SQLite 默认 journal_mode=DELETE，写期间 reader 全部阻塞 ——
# 后端有多个并发流向 (FastAPI 请求 + BG _bg_compute_initial_score + SSE 持久化)，
# 在压力下 GET /interviews/{sid} 的 SELECT 等锁能被拖到 10–20 s（>> 3 s 接口预算）。
# WAL 让 reader 读 snapshot、writer 写 WAL，二者并发；busy_timeout 兜底偶发争抢。
if _settings.database_url.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_wal(dbapi_conn, _conn_record) -> None:  # noqa: ANN001
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=10000")
        finally:
            cur.close()


# H20 (SQLite lock contention) was REJECTED by tests/diag/probe_busy_timeout.py
# (reader during 7s held write = 3ms; busy_timeout=10000 fully honoured).
# Slow SQL execs observed in earlier runs were a SECONDARY symptom of
# main-thread blocking, not a primary cause. Instrumentation removed.


AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db() -> None:
    """一次性建表 + 轻量 column 同步（不使用 alembic 时的兜底）。"""
    from app.models import interview, job, user  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_new_columns)


def _ensure_new_columns(sync_conn) -> None:
    """SQLite-only：补齐后加的列，避免老 DB 不存在新字段时报错。"""
    from sqlalchemy import text

    expected = {
        "interview_session": [
            ("impression_breakdown", "JSON"),
            # P3 / lite-auth：老 DB 没有 user_id 列，加上后历史 NULL 行
            # 会自然落到 "legacy"（无主），新接口不会泄漏给任何具体用户。
            ("user_id", "INTEGER"),
        ],
        "report": [("score_explanation_md", "TEXT DEFAULT ''")],
        # P4 / a4：lite-auth 时期注册的老用户没有 password_hash 列。
        # 新增列允许 NULL；老用户 → 任何带密码的 login 都会 verify 失败 → 401，
        # 必须重新走 /register 设密码。
        # P6：邮箱主身份。新增 ``email`` / ``email_verified_at`` 列；老用户
        # ``email IS NULL`` → /login 路径会引导其重新注册（与 password_hash
        # 处理范式对齐）。SQLite 的 UNIQUE INDEX 加在 ALTER 之后，由
        # Base.metadata.create_all 没建到时这里也补一手（CREATE INDEX IF NOT EXISTS）。
        "users": [
            ("password_hash", "TEXT"),
            ("email", "VARCHAR(254)"),
            ("email_verified_at", "DATETIME"),
        ],
        # v0.4 / 火山语音重构：UserCredential 增 ``volc_voice_key`` 单业务字段。
        # 老 DB 不存在时新增空列；存量行的旧 ``dashscope_key`` 等字段保留。
        "user_credential": [("volc_voice_key", "TEXT DEFAULT ''")],
    }
    for table, cols in expected.items():
        try:
            existing = {
                row[1]
                for row in sync_conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            }
        except Exception:
            continue
        for col_name, col_type in cols:
            if col_name not in existing:
                try:
                    sync_conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"
                    )
                except Exception:
                    pass

    # P6：``users.email`` 走 ALTER 路径加进来后，``Base.metadata.create_all``
    # 不会再补 UNIQUE INDEX（SQLAlchemy 把表当已存在）。这里显式 CREATE
    # INDEX IF NOT EXISTS，把 ORM 层 ``unique=True`` 的语义补齐到老库。
    try:
        sync_conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users(email)"
        )
    except Exception:
        pass

    # P6：放宽 ``users.username`` NOT NULL 约束。
    # 老 DB（P3/P4 时期 ``Base.metadata.create_all`` 出来的）``username VARCHAR(80) NOT NULL``，
    # 与 P6 模型 ``username Optional[str]`` 不兼容；SQLite 不支持 ALTER COLUMN
    # 修改 NOT NULL，必须 rebuild。这里做一次性表重建：
    #   1) 探针：PRAGMA table_info(users) 找 username 行的 notnull == 1；
    #   2) 命中后 BEGIN → 新建 users_p6 → 拷贝行 → DROP 旧 users → RENAME；
    #   3) 全程在 ``BEGIN ... COMMIT`` 内，失败 ROLLBACK 不留半成品；
    #   4) 同时把外键依赖（``auth_session.user_id`` / ``user_credential.user_id``）
    #      的 FK 关系靠 SQLAlchemy 的 PRAGMA foreign_keys=OFF 在表重建期间临时
    #      关闭，避免重建中途触发 cascade。
    try:
        info = sync_conn.exec_driver_sql("PRAGMA table_info(users)").fetchall()
        username_row = next((row for row in info if row[1] == "username"), None)
        if username_row is not None and int(username_row[3]) == 1:
            # 必须重建。临时关闭 FK 检查；一次事务内完成。
            sync_conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            try:
                sync_conn.exec_driver_sql(
                    "CREATE TABLE users_p6_migration ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "username VARCHAR(80), "
                    "email VARCHAR(254), "
                    "email_verified_at DATETIME, "
                    "password_hash TEXT, "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL"
                    ")"
                )
                sync_conn.exec_driver_sql(
                    "INSERT INTO users_p6_migration "
                    "(id, username, email, email_verified_at, password_hash, created_at) "
                    "SELECT id, username, email, email_verified_at, password_hash, created_at FROM users"
                )
                sync_conn.exec_driver_sql("DROP TABLE users")
                sync_conn.exec_driver_sql(
                    "ALTER TABLE users_p6_migration RENAME TO users"
                )
                sync_conn.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users(username)"
                )
                sync_conn.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users(email)"
                )
            finally:
                sync_conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    except Exception:
        # 重建失败：让运维看到错误日志而不是静默 —— 但本函数当前签名是 None，
        # init_db 主流程不读取异常。最坏情况下保留旧 NOT NULL，业务侧靠
        # auth.py 的 IntegrityError → 409 兜底（用户感知是"该邮箱或昵称已被占用"）。
        pass
