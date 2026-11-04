"""回归测试基础设施。

三个关键约束：
1. **必须跑在真实 PostgreSQL 上**（行锁语义与 SQLite 不同，SQLite 测不出超卖）。
   用 pgserver 拉起嵌入式 PostgreSQL 16，端口随机、数据目录在项目内。
2. **Redis 用 fakeredis（带 Lua）**，因为 `token_bucket` 用的是 EVAL 脚本。
3. **全测试共用一个事件循环**（loop_scope="session"）：
   asyncpg 连接不能跨 loop 复用，Windows 的 proactor 在 loop 关闭时还会抛
   `NoneType has no attribute send`，共用 loop 可以同时规避这两个问题。
"""

import asyncio
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Windows 默认的 ProactorEventLoop 在连接释放时会抛
# `AttributeError: NoneType has no attribute send`（proactor_events.py:402），
# 换 Selector 循环可稳定跑完整个测试集。asyncpg 在两种循环下都可用。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# pgserver 默认把锁文件放到 %LOCALAPPDATA%\Temp，该目录在本机对部分解释器不可写
_PGTMP = ROOT / ".pgtmp"
_PGTMP.mkdir(exist_ok=True)
os.environ["TEMP"] = str(_PGTMP)
os.environ["TMP"] = str(_PGTMP)
tempfile.tempdir = str(_PGTMP)

# ---------------------------------------------------------------------------
# 1) 拉起嵌入式 PostgreSQL（真实 PG，不是 SQLite）
# ---------------------------------------------------------------------------
import fasteners  # noqa: E402
import pgserver  # noqa: E402
import pgserver.postgres_server as _pgs  # noqa: E402

_pgs.PostgresServer._lock = fasteners.InterProcessLock(str(_PGTMP / ".pgserver.lock"))

_PG_DATA = ROOT / ".pgdata-test"
_pg = pgserver.get_server(_PG_DATA)
_pg.psql("DROP DATABASE IF EXISTS mall_test;")
_pg.psql("CREATE DATABASE mall_test;")

_base_uri = _pg.get_uri()                       # postgresql://postgres:@127.0.0.1:PORT/postgres
_sync_uri = _base_uri.rsplit("/", 1)[0] + "/mall_test"

_DB_URL = _sync_uri.replace("postgresql://", "postgresql+asyncpg://", 1)
os.environ["DATABASE_URL"] = _DB_URL
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "pytest-only-secret")
os.environ.setdefault("DEBUG", "false")

# ---------------------------------------------------------------------------
# 2) 导入应用
# ---------------------------------------------------------------------------
import fakeredis.aioredis  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

import app.db.redis_client as redis_module  # noqa: E402
import app.models  # noqa: E402,F401
import app.db.session as _dbs  # noqa: E402
from app.db.session import get_db  # noqa: E402

# 应用在 import 时用默认连接池建了 engine；连接池会把连接跨用例复用，
# 而 pytest-asyncio 每个用例一个新 loop，asyncpg 不允许跨 loop 复用连接。
# 这里把模块级 engine 换成 NullPool，任何遗漏的调用点也不会踩到跨 loop 问题。
_dbs.engine = create_async_engine(_DB_URL, poolclass=NullPool)
_dbs.AsyncSessionLocal.configure(bind=_dbs.engine)
from app.main import app  # noqa: E402
from app.models.base import Base  # noqa: E402



# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(_DB_URL, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        # 迁移会写入默认租户（id=1）；create_all 不会。
        # 测试用 create_all 建库，必须自己补这一行，否则 models 里
        # tenant_id=1 的默认值会撞外键。
        await conn.execute(
            text(
                "INSERT INTO tenants (id, code, name, status, created_at, updated_at) "
                "VALUES (1, 'default', '默认租户', 'active', now(), now())"
            )
        )
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def _clean(engine):
    """每个用例前清空数据与缓存。

    连 `tenants` 一起清（否则跨用例的租户编码会撞唯一键），
    清完立刻重建默认租户，保证 tenant_id=1 的外键始终成立。
    """
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await conn.execute(
            text(
                "INSERT INTO tenants (id, code, name, status, created_at, updated_at) "
                "VALUES (1, 'default', '默认租户', 'active', now(), now()) "
                "ON CONFLICT (id) DO NOTHING"
            )
        )
        # 自增序列推到默认租户之后，否则用例里新建的租户会再拿一次 id=1
        await conn.execute(
            text("SELECT setval(pg_get_serial_sequence('tenants','id'), (SELECT max(id) FROM tenants))")
        )
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    redis_module.redis_client = fake
    yield
    await fake.flushall()
    await fake.aclose()
    redis_module.redis_client = None


@pytest_asyncio.fixture
async def client(session_factory):
    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def db_session(session_factory):
    async with session_factory() as session:
        yield session


@pytest.fixture(scope="session")
def pg_base_url() -> str:
    """不含库名的连接前缀，形如 postgresql://postgres:@127.0.0.1:PORT。"""
    return _sync_uri.rsplit("/", 1)[0]


@pytest_asyncio.fixture
async def make_user(client):
    async def _make(email: str = "u1@example.com", password: str = "Password123"):
        r = await client.post(
            "/api/v1/auth/register",
            json={"email": email, "full_name": "Tester", "password": password},
        )
        assert r.status_code == 201, r.text
        r = await client.post(
            "/api/v1/auth/token",
            data={"username": email, "password": password},
        )
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    return _make


@pytest.fixture
def auth_headers():
    def _h(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    return _h


@pytest_asyncio.fixture
async def make_product(client):
    async def _make(name="iPhone 15", price=7999.0, stock=10):
        r = await client.post(
            "/api/v1/products/",
            json={"name": name, "description": "test", "price": price, "stock": stock},
        )
        assert r.status_code == 201, r.text
        return r.json()

    return _make


@pytest_asyncio.fixture
async def make_seckill(db_session):
    """直接建秒杀活动（秒杀活动没有对外的创建接口，只有 admin）。"""
    from datetime import datetime, timedelta

    from app.models.seckill_product import SeckillProduct

    async def _make(product_id: int, price=4999.0, stock=10, start_offset=-60, end_offset=3600):
        now = datetime.utcnow()
        sp = SeckillProduct(
            product_id=product_id,
            seckill_price=price,
            stock_count=stock,
            start_time=now + timedelta(seconds=start_offset),
            end_time=now + timedelta(seconds=end_offset),
        )
        db_session.add(sp)
        await db_session.commit()
        await db_session.refresh(sp)
        return sp

    return _make
