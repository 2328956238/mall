"""本地一键启动：不需要 Docker，也不需要预装 PostgreSQL / Redis。

做四件事：
  1. 用 pgserver 起一个嵌入式 PostgreSQL 16（数据目录 .pgdata-local，端口随机）
  2. 幂等建库 flash_sales
  3. 跑 alembic upgrade head（走真实迁移链，含 default 租户等数据操作）
  4. 以 USE_FAKE_REDIS=true 起 uvicorn（Redis 用进程内 fakeredis）

用法（在项目根目录）：
    .venv\\Scripts\\python.exe dev_start.py             # 默认 8000 端口
    .venv\\Scripts\\python.exe dev_start.py --port 8100
    .venv\\Scripts\\python.exe dev_start.py --reload    # 改代码自动重启
    .venv\\Scripts\\python.exe dev_start.py --debug     # 打印每条 SQL
    .venv\\Scripts\\python.exe dev_start.py --reset     # 删掉旧库重来（数据会没）
    .venv\\Scripts\\python.exe dev_start.py --real-redis  # 用真 Redis（自己起在 6379）

也可以直接双击 start_dev.bat。

为什么这里要跑 alembic 而不是让 app 的 create_all 建表：
create_all 只建结构，不会执行迁移里的数据操作。而 `4e87f06d6490` 会写入
default 租户（id=1），少了它，注册第一个用户就会外键失败——tests/conftest.py
里手动补这一行就是这个原因。

边界（重要）：
  - 假 Redis 是进程内的：多 worker 不共享、重启即丢、限流不跨进程。
  - 嵌入式 PG 只适合本地跑功能与联调；压测、多机、迁移验证仍然要用真 PG + 真 Redis。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PG_DATA = ROOT / ".pgdata-local"
PG_TMP = ROOT / ".pgtmp"
DB_NAME = "flash_sales"


def _prepare_tmp() -> None:
    """pgserver 默认把锁文件放 %LOCALAPPDATA%\\Temp，部分环境下不可写，改到项目内。"""
    PG_TMP.mkdir(exist_ok=True)
    os.environ["TEMP"] = str(PG_TMP)
    os.environ["TMP"] = str(PG_TMP)
    tempfile.tempdir = str(PG_TMP)


def _start_pg() -> str:
    import fasteners
    import pgserver
    import pgserver.postgres_server as _pgs

    # pgserver 默认的锁文件目录在本机不可写（tests/conftest.py 里踩过同一个坑），改到项目内
    _pgs.PostgresServer._lock = fasteners.InterProcessLock(str(PG_TMP / ".pgserver.lock"))

    print(f"[1/4] 启动嵌入式 PostgreSQL（数据目录 {PG_DATA.name}）...", flush=True)
    pg = pgserver.get_server(PG_DATA)
    uri = pg.get_uri()  # postgresql://postgres:@127.0.0.1:PORT/postgres

    print(f"[2/4] 准备数据库 {DB_NAME} ...", flush=True)
    try:
        pg.psql(f"CREATE DATABASE {DB_NAME};")
    except Exception:
        pass  # 已存在就复用
    return uri.rsplit("/", 1)[0] + f"/{DB_NAME}"


def _run_migrations(database_url: str) -> None:
    print("[3/4] 执行 alembic upgrade head ...", flush=True)
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(ROOT),
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print("  迁移失败：")
        print(proc.stdout[-3000:])
        print(proc.stderr[-3000:])
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="mall 本地一键启动（嵌入式 PG + 假 Redis）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="改代码自动重启")
    parser.add_argument("--debug", action="store_true", help="打开 DEBUG（会打印每条 SQL）")
    parser.add_argument("--reset", action="store_true", help="删除 .pgdata-local 后重来")
    parser.add_argument("--real-redis", action="store_true", help="用真 Redis（需自己起在 REDIS_URL）")
    args = parser.parse_args()

    if args.reset and PG_DATA.exists():
        print(f"删除旧数据目录 {PG_DATA} ...", flush=True)
        shutil.rmtree(PG_DATA, ignore_errors=True)

    _prepare_tmp()
    sync_uri = _start_pg()

    database_url = "postgresql+asyncpg://" + sync_uri.split("://", 1)[1]
    os.environ["DATABASE_URL"] = database_url
    os.environ.setdefault("SECRET_KEY", "dev-only-secret-change-me")
    # DEBUG=true 会让 SQLAlchemy 把每条 SQL 都打到控制台（几十屏），本地默认关掉
    os.environ["DEBUG"] = "true" if args.debug else "false"

    os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
    os.environ["USE_FAKE_REDIS"] = "false" if args.real_redis else "true"

    _run_migrations(database_url)

    print("[4/4] 启动服务 ...", flush=True)
    print(f"      API 文档  http://{args.host}:{args.port}/docs", flush=True)
    print(f"      健康检查  http://{args.host}:{args.port}/health", flush=True)
    print(f"      前端首页  http://{args.host}:{args.port}/", flush=True)
    print(f"      Redis    {'真 Redis @ ' + os.environ['REDIS_URL'] if args.real_redis else '进程内假 Redis'}", flush=True)
    print("      Ctrl+C 退出\n", flush=True)

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
