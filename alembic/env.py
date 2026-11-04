"""Alembic 环境配置（异步）。

约定：
- 连接串只从环境变量 `DATABASE_URL` 读取，绝不写进 alembic.ini，
  避免把生产库密码提交进仓库。
- `target_metadata` 指向 `app.models.base.Base.metadata`，
  因此 autogenerate 的比对基准就是代码里的模型定义。
"""

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# app/models/__init__.py 已统一导出所有模型（见 docs/PROBLEMS.md P8 的根治方案）。
# 新增模型只要在那里补一行，这里不需要改。
import app.models  # noqa: F401,E402
from app.models.base import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "未设置 DATABASE_URL。迁移必须显式指定目标库，例如：\n"
            "  DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db alembic upgrade head"
        )
    return url


def include_object(obj, name, type_, reflected, compare_to):
    """跳过 alembic 自己的版本表。"""
    if type_ == "table" and name == "alembic_version":
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
