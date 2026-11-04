"""第 6 步：老数据回填与金额迁移测试。

在**独立临时库**上真跑迁移，验证：
1. 老库（Float 金额 + 无 tenant_id）能平滑升级；
2. `tenant_id` 回填到默认租户；
3. 每个商品生成 SKU + 默认仓库存 + 初始化流水；
4. 秒杀活动被绑定到库存域；
5. 金额从 `double precision` 变成 `numeric(18,4)`；
6. `orders.seckill_product_id` 变成可空。
"""

import asyncio
import os
import subprocess
import sys

import asyncpg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRE_BACKFILL = "8ad9bed21515"


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
    env = {**os.environ, "DATABASE_URL": db_url}

    def _run():
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )

    proc = await asyncio.to_thread(_run)
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, f"alembic {' '.join(args)} 失败：\n{out[-3000:]}"
    return out


def _url(base_url: str, db: str) -> str:
    return f"{base_url}/{db}".replace("postgresql://", "postgresql+asyncpg://", 1)


async def test_backfill_upgrades_legacy_database(pg_base_url):
    db = "mall_backfill"
    url = _url(pg_base_url, db)
    await _create_db(pg_base_url, db)
    try:
        # ① 升到"回填前"的版本，塞入老数据
        await _alembic(url, "upgrade", PRE_BACKFILL)
        conn = await asyncpg.connect(f"{pg_base_url}/{db}")
        try:
            tenant_id = await conn.fetchval("select id from tenants where code='default'")
            # 回填前的状态：这些老表**还没有** tenant_id 列
            await conn.execute(
                """
                insert into products (name, description, price, stock, is_on_sale)
                values ('老商品', 'legacy', 7999.99, 7, true)
                """
            )
            product_id = await conn.fetchval("select id from products where name='老商品'")
            await conn.execute(
                """
                insert into users (id, email, hashed_password, full_name, is_active, is_admin)
                values (gen_random_uuid(), 'old@example.com', 'x', '老用户', true, false)
                """
            )
            user_id = await conn.fetchval("select id from users where email='old@example.com'")
            await conn.execute(
                """
                insert into seckill_products
                    (product_id, seckill_price, stock_count, start_time, end_time)
                values ($1, 4999.5, 3, now() - interval '1 hour', now() + interval '1 hour')
                """,
                product_id,
            )
            seckill_id = await conn.fetchval("select max(id) from seckill_products")
            await conn.execute(
                """
                insert into orders (id, user_id, seckill_product_id, quantity, total_amount, status)
                values (gen_random_uuid(), $1, $2, 1, 4999.5, 'PENDING')
                """,
                user_id, seckill_id,
            )
        finally:
            await conn.close()

        # ② 跑回填迁移
        await _alembic(url, "upgrade", "head")

        # ③ 断言
        conn = await asyncpg.connect(f"{pg_base_url}/{db}")
        try:
            # 所有老表都挂上默认租户
            for table in ("products", "users", "seckill_products", "orders"):
                nulls = await conn.fetchval(f"select count(*) from {table} where tenant_id is null")
                assert nulls == 0, f"{table}.tenant_id 仍有空值"

            # 金额类型变成 numeric(18,4)，且值没丢
            row = await conn.fetchrow(
                "select data_type, numeric_precision, numeric_scale, price "
                "from information_schema.columns c "
                "join products p on true "
                "where c.table_name='products' and c.column_name='price' limit 1"
            )
            assert row["data_type"] == "numeric"
            assert (row["numeric_precision"], row["numeric_scale"]) == (18, 4)
            assert str(row["price"]) == "7999.9900"

            # 每商品一个 SKU
            sku_count = await conn.fetchval(
                "select count(*) from skus where product_id = $1", product_id
            )
            assert sku_count == 1

            # 库存从 products.stock 迁到 warehouse_stocks
            on_hand = await conn.fetchval(
                "select ws.on_hand from warehouse_stocks ws "
                "join skus s on s.id = ws.sku_id where s.product_id = $1",
                product_id,
            )
            assert on_hand == 7

            # 派生汇总与明细一致
            product_stock = await conn.fetchval(
                "select stock from products where id = $1", product_id
            )
            assert product_stock == 7

            # 初始化流水
            txn = await conn.fetchval(
                "select count(*) from stock_transactions where ref_type='migration'"
            )
            assert txn >= 1

            # 秒杀绑定到库存域
            bound = await conn.fetchrow(
                "select sku_id, warehouse_id from seckill_products where id = $1", seckill_id
            )
            assert bound["sku_id"] is not None
            assert bound["warehouse_id"] is not None

            # 订单金额也变成 numeric
            order_amount = await conn.fetchval("select total_amount from orders limit 1")
            assert str(order_amount) == "4999.5000"

            # seckill_product_id 可空
            nullable = await conn.fetchval(
                "select is_nullable from information_schema.columns "
                "where table_name='orders' and column_name='seckill_product_id'"
            )
            assert nullable == "YES"

            # 可以插入一张没有秒杀活动的普通订单
            await conn.execute(
                """
                insert into orders (id, tenant_id, user_id, seckill_product_id, quantity, total_amount, status)
                values (gen_random_uuid(), $1, $2, null, 1, 100.0000, 'PENDING')
                """,
                tenant_id, user_id,
            )
        finally:
            await conn.close()
    finally:
        await _drop_db(pg_base_url, db)


async def test_backfill_downgrade_cleans_up(pg_base_url):
    """回滚要把回填出来的库存数据清掉，避免 products.stock 与库存域重复计数。"""
    db = "mall_backfill_rollback"
    url = _url(pg_base_url, db)
    await _create_db(pg_base_url, db)
    try:
        await _alembic(url, "upgrade", PRE_BACKFILL)
        conn = await asyncpg.connect(f"{pg_base_url}/{db}")
        try:
            tenant_id = await conn.fetchval("select id from tenants where code='default'")
            await conn.execute(
                "insert into products (name, price, stock, is_on_sale) "
                "values ('回滚商品', 10.5, 4, true)"
            )
        finally:
            await conn.close()

        await _alembic(url, "upgrade", "head")
        await _alembic(url, "downgrade", PRE_BACKFILL)

        conn = await asyncpg.connect(f"{pg_base_url}/{db}")
        try:
            assert await conn.fetchval("select count(*) from warehouse_stocks") == 0
            assert await conn.fetchval("select count(*) from skus") == 0
            # 商品本身还在，金额回到 double precision
            assert await conn.fetchval("select count(*) from products") == 1
            dtype = await conn.fetchval(
                "select data_type from information_schema.columns "
                "where table_name='products' and column_name='price'"
            )
            assert dtype == "double precision"
        finally:
            await conn.close()
    finally:
        await _drop_db(pg_base_url, db)
