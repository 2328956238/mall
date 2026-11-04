"""回归：秒杀主链路（改造后必须保持同一行为）。"""

import pytest


async def _seckill(client, headers, sid):
    return await client.post(f"/api/v1/seckill/{sid}", headers=headers)


async def test_seckill_success(
    client, make_user, auth_headers, make_product, make_seckill, db_session
):
    from sqlalchemy import select

    from app.models.order import Order
    from app.models.product import Product
    from app.models.seckill_product import SeckillProduct

    token = await make_user()
    p = await make_product(stock=10)
    sp = await make_seckill(p["id"], price=4999.0, stock=10)

    r = await _seckill(client, auth_headers(token), sp.id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["order_id"]

    # 两个库存都必须同步扣减（这是现有行为，改造后要改为统一库存域）
    product = (await db_session.execute(select(Product).where(Product.id == p["id"]).execution_options(populate_existing=True))).scalar_one()
    seckill = (
        await db_session.execute(select(SeckillProduct).where(SeckillProduct.id == sp.id).execution_options(populate_existing=True))
    ).scalar_one()
    assert product.stock == 9
    assert seckill.stock_count == 9

    order = (await db_session.execute(select(Order))).scalars().first()
    assert order is not None
    assert order.quantity == 1
    assert float(order.total_amount) == 4999.0
    assert order.status.value == "pending"


async def test_seckill_rate_limited(client, make_user, auth_headers, make_product, make_seckill):
    """同一用户同一活动：令牌桶容量 1，第二次应被限流。"""
    token = await make_user()
    p = await make_product(stock=10)
    sp = await make_seckill(p["id"], stock=10)

    first = await _seckill(client, auth_headers(token), sp.id)
    assert first.status_code == 200
    second = await _seckill(client, auth_headers(token), sp.id)
    assert second.status_code == 429


async def test_seckill_out_of_stock(
    client, make_user, auth_headers, make_product, make_seckill
):
    token = await make_user()
    p = await make_product(stock=0)
    sp = await make_seckill(p["id"], stock=0)

    r = await _seckill(client, auth_headers(token), sp.id)
    assert r.status_code == 410


async def test_seckill_not_started(
    client, make_user, auth_headers, make_product, make_seckill
):
    token = await make_user()
    p = await make_product(stock=10)
    sp = await make_seckill(p["id"], start_offset=3600, end_offset=7200)

    r = await _seckill(client, auth_headers(token), sp.id)
    assert r.status_code == 410


async def test_seckill_expired(client, make_user, auth_headers, make_product, make_seckill):
    token = await make_user()
    p = await make_product(stock=10)
    sp = await make_seckill(p["id"], start_offset=-7200, end_offset=-3600)

    r = await _seckill(client, auth_headers(token), sp.id)
    assert r.status_code == 410


async def test_seckill_not_found(client, make_user, auth_headers):
    token = await make_user()
    r = await _seckill(client, auth_headers(token), 999999)
    assert r.status_code == 410


async def test_seckill_requires_auth(client, make_product, make_seckill):
    p = await make_product(stock=10)
    sp = await make_seckill(p["id"], stock=10)
    r = await client.post(f"/api/v1/seckill/{sp.id}")
    assert r.status_code == 401


async def test_seckill_list(client, make_product, make_seckill):
    p = await make_product(stock=10)
    await make_seckill(p["id"], stock=10)
    r = await client.get("/api/v1/seckill/products/")
    assert r.status_code == 200
    assert len(r.json()) == 1
