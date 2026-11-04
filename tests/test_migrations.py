"""迁移测试：在独立临时库上真跑 alembic。

为什么单独测迁移，而不是只测模型：
- 模型建表（create_all）能通过，不代表**迁移脚本**能通过 —— 两者是不同的代码路径；
- 第 1 步已经踩过「downgrade 不删 PG 枚举导致二次 upgrade 失败」的坑，
  只有真跑 `upgrade → downgrade → upgrade` 才能发现；
- 迁移里还包含数据操作（默认租户），模型测试完全覆盖不到。

代价是慢（每次起子进程跑 alembic，约 3~5 秒），所以只在迁移相关用例里做。
"""

import asyncio
import os
import subprocess
import sys

import asyncpg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def _create_db(base_url: str, name: str) -> None:
    conn = await asyncpg.connect(base_url + "/postgres")
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop_db(base_url: str, name: str) -> None:
    conn = await asyncpg.connect(base_url + "/postgres")
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


async def _alembic(db_url: str, *args: str) -> str:
    """在子进程里跑 alembic。

    不能在本进程里调 `alembic.command`：`alembic/env.py` 内部会 `asyncio.run()`，
    而测试本身已经在一个运行中的事件循环里，会直接报
    "asyncio.run() cannot be called from a running event loop"。
    """
    env = {**os.environ, "DATABASE_URL": db_url}
    env.pop("PYTHONASYNCIODEBUG", None)

    def _run():
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    proc = await asyncio.to_thread(_run)
    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, f"alembic {' '.join(args)} 失败：\n{output[-3000:]}"
    return output


def _async_url(base_url: str, db: str) -> str:
    return f"{base_url}/{db}".replace("postgresql://", "postgresql+asyncpg://", 1)


async def test_migrations_upgrade_downgrade_upgrade(pg_base_url):
    """完整走一遍：空库升级 → 回滚 → 再升级。

    第三段是关键：如果 downgrade 没清理干净（比如残留 ENUM 类型），
    第二次 upgrade 会报 DuplicateObject。
    """
    db = "mall_mig_test"
    url = _async_url(pg_base_url, db)
    await _create_db(pg_base_url, db)
    try:
        await _alembic(url, "upgrade", "head")
        await _alembic(url, "downgrade", "base")
        await _alembic(url, "upgrade", "head")
    finally:
        await _drop_db(pg_base_url, db)


async def test_migration_seeds_default_tenant(pg_base_url):
    """迁移必须建出 default 租户（决策 2：老数据统一挂 default）。"""
    db = "mall_mig_seed"
    url = _async_url(pg_base_url, db)
    await _create_db(pg_base_url, db)
    try:
        await _alembic(url, "upgrade", "head")
        conn = await asyncpg.connect(f"{pg_base_url}/{db}")
        try:
            row = await conn.fetchrow("select code, name, status from tenants where code='default'")
            assert row is not None, "迁移后没有 default 租户"
            assert row["status"] == "active"
            count = await conn.fetchval("select count(*) from tenants")
            assert count == 1
        finally:
            await conn.close()
    finally:
        await _drop_db(pg_base_url, db)


async def test_migration_schema_matches_models(pg_base_url):
    """迁移建出来的表集合必须和代码里的模型完全一致。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    import app.models  # noqa: F401
    from app.models.base import Base

    db = "mall_mig_schema"
    url = _async_url(pg_base_url, db)
    await _create_db(pg_base_url, db)
    try:
        await _alembic(url, "upgrade", "head")

        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                from sqlalchemy import text

                rows = await conn.execute(
                    text(
                        "select table_name from information_schema.tables "
                        "where table_schema='public'"
                    )
                )
                actual = {r[0] for r in rows} - {"alembic_version"}
        finally:
            await engine.dispose()

        expected = set(Base.metadata.tables)
        assert actual == expected, (
            f"迁移比模型多的表: {actual - expected}；"
            f"模型比迁移多的表: {expected - actual}"
        )
    finally:
        await _drop_db(pg_base_url, db)


async def test_steps_are_tracked(pg_base_url):
    """迁移可追踪：head 必须能查到版本号。"""
    db = "mall_mig_ver"
    url = _async_url(pg_base_url, db)
    await _create_db(pg_base_url, db)
    try:
        await _alembic(url, "upgrade", "head")
        out = await _alembic(url, "current")
        assert "head" in out
    finally:
        await _drop_db(pg_base_url, db)
