"""数据库结构核对（Alembic baseline 的前置闸门）。

用途：在 `alembic stamp head` 之前，确认「线上库的真实结构」与「代码里的模型定义」一致。
不一致就退出码 1，不允许 stamp —— 否则等于用一个假 baseline 掩盖了结构漂移。

用法：
    DATABASE_URL=postgresql+asyncpg://... python scripts/verify_schema.py
    DATABASE_URL=... python scripts/verify_schema.py --allow-empty

退出码：
    0  结构一致（或库为空且显式允许）
    1  存在差异
    2  连接失败等环境问题
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

import app.models  # noqa: F401
from app.models import (  # noqa: F401
    base as _base,
    operation_log as _operation_log,
    order as _order,
    product as _product,
    seckill_product as _seckill_product,
    user as _user,
)
from app.models.base import Base  # noqa: E402


def _compare(connection):
    ctx = MigrationContext.configure(
        connection,
        opts={
            "compare_type": True,
            "compare_server_default": True,
            "include_object": lambda obj, name, type_, reflected, compare_to: not (
                type_ == "table" and name == "alembic_version"
            ),
        },
    )
    return compare_metadata(ctx, Base.metadata)


def _fmt(diff) -> str:
    """把 alembic 的差异元组转成可读文本。"""
    if isinstance(diff, list):
        return "; ".join(_fmt(d) for d in diff)
    if isinstance(diff, tuple) and diff:
        kind = diff[0]
        if kind == "add_table":
            return f"[缺失表] 库中没有：{diff[1].name}"
        if kind == "remove_table":
            return f"[多余表] 库中存在但模型没有：{diff[1].name}"
        if kind == "add_column":
            return f"[缺失列] {diff[2]}.{diff[3].name}"
        if kind == "remove_column":
            return f"[多余列] {diff[2]}.{diff[3].name}"
        if kind == "modify_type":
            return f"[类型不一致] {diff[2]}.{diff[3]}.{diff[4]}"
        if kind == "modify_nullable":
            return f"[可空性不一致] {diff[2]}.{diff[3]}.{diff[4]}"
        if kind in ("add_index", "remove_index"):
            return f"[索引差异] {kind} {getattr(diff[1], 'name', diff[1])}"
        if kind in ("add_fk", "remove_fk"):
            return f"[外键差异] {kind}"
        return f"[{kind}] {diff[1:]}"
    return str(diff)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-empty", action="store_true", help="允许空库（用于初始化前检查）")
    args = parser.parse_args()

    url = os.getenv("DATABASE_URL")
    if not url:
        print("ERROR: 未设置 DATABASE_URL", file=sys.stderr)
        return 2

    engine = create_async_engine(url, poolclass=None)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
            data_tables = [t for t in tables if t != "alembic_version"]

            if not data_tables:
                print("库为空（没有任何业务表）。")
                return 0 if args.allow_empty else 1

            diffs = await conn.run_sync(_compare)
    except Exception as exc:  # 连接/权限问题
        print(f"ERROR: 无法连接或读取结构：{exc}", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()

    if not diffs:
        print(f"结构一致 [OK]  现有 {len(data_tables)} 张表与模型定义完全匹配。")
        print("可以安全执行：alembic stamp head")
        return 0

    print(f"发现 {len(diffs)} 处结构差异 [FAIL]，禁止直接 stamp：\n")
    for d in diffs:
        print("  -", _fmt(d))
    print(
        "\n处理方式：\n"
        "  1) 若差异是历史遗留（例如手工加过索引），先补一条迁移把它对齐；\n"
        "  2) 若差异是模型写错了，改模型；\n"
        "  3) 确认无误后再 stamp，并保留这次核对输出作为证据。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
