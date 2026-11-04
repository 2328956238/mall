"""第 5 步：秒杀走库存域。

关键性质：
1. 秒杀成功 = 库存域预留 + 核销，`warehouse_stocks.on_hand` 是唯一真相；
2. `products.stock` 与 `seckill_products.stock_count` 都跟着变（一个派生、一个配额）；
3. 并发下不超卖；
4. 失败路径要补偿：释放预留 + 回补 Redis；
5. 对账能把 Redis 与数据库的差值算出来。
"""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.inventory import (
    ReservationStatus,
    SKU,
    StockReservation,
    StockTransaction,
    StockTxnType,
    Warehouse,
    WarehouseStock,
)
from app.models.order import Order
from app.models.product import Product
from app.models.seckill_product import SeckillProduct
from app.models.tenant import Tenant
from app.services.seckill_service import SeckillService


async def _setup(session, on_hand=5, stock_count=5, bind=True):
    tenant = Tenant(code="t-sk", name="秒杀租户", status="active")
    session.add(tenant)
    await session.flush()

    product = Product(name="秒杀商品", description="t", price=8999.0, stock=0, is_on_sale=True)
    session.add(product)
    await session.flush()

    sku = SKU(tenant_id=tenant.id, product_id=product.id, sku_code="SKU-SK1",
              spec={}, sale_price=Decimal("4999.0000"))
    wh = Warehouse(tenant_id=tenant.id, code="SZ", name="深圳仓", country="CN")
    session.add_all([sku, wh])
    await session.flush()

    stock = WarehouseStock(tenant_id=tenant.id, sku_id=sku.id, warehouse_id=wh.id,
                           on_hand=on_hand, reserved=0)
    session.add(stock)

    now = datetime.utcnow()
    sec = SeckillProduct(
        product_id=product.id,
        sku_id=sku.id if bind else None,
        warehouse_id=wh.id if bind else None,
        seckill_price=4999.0,
        stock_count=stock_count,
        start_time=now - timedelta(minutes=5),
        end_time=now + timedelta(hours=1),
    )
    session.add(sec)
    await session.commit()

    return {
        "tenant_id": tenant.id, "product_id": product.id, "sku_id": sku.id,
        "warehouse_id": wh.id, "seckill_id": sec.id,
    }


async def _snapshot(session, ids):
    """取一份纯值快照，避免 ORM 对象过期问题。"""
    stock = (
        await session.execute(
            select(WarehouseStock).where(
                WarehouseStock.sku_id == ids["sku_id"],
                WarehouseStock.warehouse_id == ids["warehouse_id"],
            ).execution_options(populate_existing=True)
        )
    ).scalar_one()
    product_stock = (
        await session.execute(
            select(Product.stock).where(Product.id == ids["product_id"])
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    sec_stock = (
        await session.execute(
            select(SeckillProduct.stock_count).where(SeckillProduct.id == ids["seckill_id"])
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return {"on_hand": stock.on_hand, "reserved": stock.reserved,
            "product_stock": product_stock, "stock_count": sec_stock}


# ---------------------------------------------------------------------------
# 正常成交
# ---------------------------------------------------------------------------
async def test_seckill_uses_inventory(client, db_session, make_user, auth_headers):
    ids = await _setup(db_session)
    token = await make_user("sk1@example.com")

    r = await client.post(f"/api/v1/seckill/{ids['seckill_id']}", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True

    snap = await _snapshot(db_session, ids)
    assert snap["on_hand"] == 4          # 库存域真的减了
    assert snap["reserved"] == 0         # 预留已核销，不是挂着的
    assert snap["product_stock"] == 4    # 派生汇总同步
    assert snap["stock_count"] == 4      # 活动配额

    # 订单
    order = (await db_session.execute(select(Order))).scalars().first()
    assert order is not None and float(order.total_amount) == 4999.0

    # 预留状态
    res = (await db_session.execute(select(StockReservation))).scalars().first()
    assert res.status == ReservationStatus.COMMITTED

    # 流水：一次成交会留下 reserve + outbound 两条
    txns = (await db_session.execute(select(StockTransaction))).scalars().all()
    types = [x.txn_type for x in txns]
    assert StockTxnType.RESERVE in types
    outbound = [x for x in txns if x.txn_type == StockTxnType.OUTBOUND]
    assert len(outbound) == 1
    assert outbound[0].on_hand_after == 4
    assert outbound[0].qty_delta == -1


async def test_seckill_without_explicit_binding_still_uses_inventory(
    client, db_session, make_user, auth_headers
):
    """活动没显式绑 SKU，但商品有 SKU+库存 —— 自动解析后仍走库存域。"""
    ids = await _setup(db_session, bind=False)
    token = await make_user("sk2@example.com")

    r = await client.post(f"/api/v1/seckill/{ids['seckill_id']}", headers=auth_headers(token))
    assert r.status_code == 200, r.text

    snap = await _snapshot(db_session, ids)
    assert snap["on_hand"] == 4
    txns = (await db_session.execute(select(StockTransaction))).scalars().all()
    assert any(x.txn_type == StockTxnType.OUTBOUND for x in txns)


# ---------------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------------
async def test_concurrent_seckill_no_oversell(client, db_session, make_user, auth_headers):
    ids = await _setup(db_session, on_hand=5, stock_count=5)
    tokens = [await make_user(f"buyer{i}@qq.com") for i in range(10)]

    async def buy(t):
        return await client.post(f"/api/v1/seckill/{ids['seckill_id']}", headers=auth_headers(t))

    results = await asyncio.wait_for(
        asyncio.gather(*[buy(t) for t in tokens]), timeout=60
    )
    codes = [r.status_code for r in results]
    assert codes.count(200) == 5, f"应恰好 5 人成功，实际分布 {codes}"

    snap = await _snapshot(db_session, ids)
    assert snap["on_hand"] == 0
    assert snap["stock_count"] == 0
    assert snap["reserved"] == 0
    assert snap["product_stock"] == 0

    orders = (await db_session.execute(select(func.count()).select_from(Order))).scalar()
    assert orders == 5


# ---------------------------------------------------------------------------
# 失败与补偿
# ---------------------------------------------------------------------------
async def test_insufficient_stock_compensates(client, db_session, make_user, auth_headers):
    """库存为 0 但活动配额还有 —— 必须拒绝，且不能留下预留和错乱的 Redis。"""
    ids = await _setup(db_session, on_hand=0, stock_count=5)
    token = await make_user("sk3@example.com")

    r = await client.post(f"/api/v1/seckill/{ids['seckill_id']}", headers=auth_headers(token))
    assert r.status_code == 410

    snap = await _snapshot(db_session, ids)
    assert snap["on_hand"] == 0
    assert snap["stock_count"] == 5      # 没卖掉，配额没扣
    assert snap["reserved"] == 0

    active = (
        await db_session.execute(
            select(func.count()).select_from(StockReservation).where(
                StockReservation.status == ReservationStatus.ACTIVE
            )
        )
    ).scalar()
    assert active == 0

    # Redis 回补：计数器应回到初始 5
    from app.config import settings
    raw = await db_session.execute(select(func.now()))  # 占位，避免未使用告警
    from app.db.redis_client import get_redis

    redis = await get_redis()
    assert await redis.get(f"{settings.SECKILL_STOCK_PREFIX}{ids['seckill_id']}") == "5"


async def test_rate_limit_still_applies(client, db_session, make_user, auth_headers):
    ids = await _setup(db_session)
    token = await make_user("sk4@example.com")

    first = await client.post(f"/api/v1/seckill/{ids['seckill_id']}", headers=auth_headers(token))
    assert first.status_code == 200
    second = await client.post(f"/api/v1/seckill/{ids['seckill_id']}", headers=auth_headers(token))
    assert second.status_code == 429


# ---------------------------------------------------------------------------
# 对账
# ---------------------------------------------------------------------------
async def test_reconcile_matches_after_sales(client, db_session, make_user, auth_headers):
    ids = await _setup(db_session, on_hand=5, stock_count=5)
    token = await make_user("sk5@example.com")
    await client.post(f"/api/v1/seckill/{ids['seckill_id']}", headers=auth_headers(token))

    from app.db.redis_client import get_redis

    svc = SeckillService(await get_redis(), db_session)
    report = await svc.reconcile(ids["seckill_id"])

    assert report["ok"] is True
    assert report["drift"] == 0
    assert report["redis_counter"] == 4
    assert report["db_available"] == 4


async def test_reconcile_detects_drift(client, db_session, make_user, auth_headers):
    """人为把 Redis 计数器改小 —— 对账必须报出负漂移（超卖风险）。"""
    ids = await _setup(db_session, on_hand=5, stock_count=5)

    from app.config import settings
    from app.db.redis_client import get_redis

    redis = await get_redis()
    await redis.set(f"{settings.SECKILL_STOCK_PREFIX}{ids['seckill_id']}", 2)

    svc = SeckillService(redis, db_session)
    report = await svc.reconcile(ids["seckill_id"])

    assert report["ok"] is False
    assert report["drift"] == -3        # 2 - 5：Redis 认为只剩 2，数据库还有 5
