"""金额精度：Float → Decimal 之后，API 与数据库都不能丢精度。

要点：
- pydantic 把 `Decimal` 序列化成**字符串**（如 "19.9900"），这是刻意的：
  变成 JSON number 会经过 IEEE754 双精度，19.99 就不再是 19.99。
- 前端已改成 `Number(x).toFixed(2)` 只用于展示，不参与计算。
"""

from decimal import Decimal

import pytest
from sqlalchemy import text


async def test_product_price_kept_as_string(client):
    r = await client.post(
        "/api/v1/products/",
        json={"name": "精度商品", "price": "19.9999", "stock": 0},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # 序列化成字符串，且精度完整保留
    assert body["price"] == "19.9999"
    assert isinstance(body["price"], str)

    got = await client.get(f"/api/v1/products/{body['id']}")
    assert got.json()["price"] == "19.9999"


async def test_price_not_degraded_by_float(client, db_session):
    """0.1 + 0.2 在 float 下是 0.30000000000000004，Decimal 下必须精确。"""
    r = await client.post(
        "/api/v1/products/", json={"name": "小数商品", "price": "0.3", "stock": 0}
    )
    pid = r.json()["id"]

    row = await db_session.execute(
        text("select price::text from products where id = :pid"), {"pid": pid}
    )
    assert row.scalar_one() == "0.3000"


async def test_order_amount_is_decimal(client, db_session, make_user, auth_headers):
    """订单金额同样走 Decimal，接口返回字符串。"""
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from app.models.inventory import SKU, Warehouse, WarehouseStock
    from app.models.product import Product
    from app.models.seckill_product import SeckillProduct
    from app.models.tenant import Tenant

    tenant = Tenant(code="t-money", name="金额租户", status="active")
    db_session.add(tenant)
    await db_session.flush()

    product = Product(tenant_id=tenant.id, name="订单金额商品", description="t",
                      price=Decimal("99.0000"), stock=1, is_on_sale=True)
    db_session.add(product)
    await db_session.flush()

    sku = SKU(tenant_id=tenant.id, product_id=product.id, sku_code="M-1",
              spec={}, sale_price=Decimal("99.0000"))
    wh = Warehouse(tenant_id=tenant.id, code="W-M", name="金额仓", country="CN")
    db_session.add_all([sku, wh])
    await db_session.flush()
    db_session.add(WarehouseStock(tenant_id=tenant.id, sku_id=sku.id,
                                  warehouse_id=wh.id, on_hand=1, reserved=0))

    now = datetime.utcnow()
    sec = SeckillProduct(
        tenant_id=tenant.id, product_id=product.id, sku_id=sku.id, warehouse_id=wh.id,
        seckill_price=Decimal("0.0700"), stock_count=1,
        start_time=now - timedelta(minutes=1), end_time=now + timedelta(hours=1),
    )
    db_session.add(sec)
    await db_session.commit()

    token = await make_user("money@example.com")
    r = await client.post(f"/api/v1/seckill/{sec.id}", headers=auth_headers(token))
    assert r.status_code == 200, r.text

    order = (await db_session.execute(select(__import__("app.models.order", fromlist=["Order"]).Order))).scalars().first()
    assert order.total_amount == Decimal("0.0700")

    listing = await client.get("/api/v1/orders/", headers=auth_headers(token))
    assert listing.status_code == 200
    assert listing.json()["data"][0]["total_amount"] == "0.0700"
