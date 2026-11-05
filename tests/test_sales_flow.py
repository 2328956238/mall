"""第三阶段：销售与订单履约测试。

核心要验证的是"库存动作真的发生了"：
审核 → 预留（可用量减少、实际库存不变）
发货 → 核销（实际库存减少、锁定量归零）
取消 → 释放（可用量恢复）
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.inventory import (
    ReservationStatus,
    SKU,
    StockReservation,
    StockTransaction,
    Warehouse,
    WarehouseStock,
)
from app.models.product import Product
from app.models.sales import SalesOrderStatus, ShipmentStatus, TrackingStatus
from app.models.tenant import Tenant
from app.services.sales_service import (
    SalesError,
    SalesNotFound,
    SalesService,
    SalesTransitionError,
    ShipLine,
    ShipmentQuantityError,
)


# ---------------------------------------------------------------------------
# 数据准备
# ---------------------------------------------------------------------------
async def _seed(session, on_hand=20, default_tenant=False, code="t-sale"):
    if default_tenant:
        tenant_id = 1
    else:
        tenant = Tenant(code=code, name="销售租户", status="active")
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id

    product = Product(tenant_id=tenant_id, name="销售商品", description="t",
                      price=Decimal("500.0000"), stock=0, is_on_sale=True)
    session.add(product)
    await session.flush()

    sku1 = SKU(tenant_id=tenant_id, product_id=product.id, sku_code=f"S-{code}-1",
               spec={}, sale_price=Decimal("500.0000"))
    sku2 = SKU(tenant_id=tenant_id, product_id=product.id, sku_code=f"S-{code}-2",
               spec={}, sale_price=Decimal("200.0000"))
    wh = Warehouse(tenant_id=tenant_id, code=f"W-{code}", name="销售仓", country="CN")
    session.add_all([sku1, sku2, wh])
    await session.flush()

    session.add_all([
        WarehouseStock(tenant_id=tenant_id, sku_id=sku1.id, warehouse_id=wh.id,
                       on_hand=on_hand, reserved=0),
        WarehouseStock(tenant_id=tenant_id, sku_id=sku2.id, warehouse_id=wh.id,
                       on_hand=on_hand, reserved=0),
    ])
    await session.commit()
    return {
        "tenant_id": tenant_id, "product_id": product.id,
        "sku1": sku1.id, "sku2": sku2.id, "wh": wh.id,
    }


async def _customer(svc, tenant_id, code="CUS-1"):
    return await svc.create_customer(
        tenant_id=tenant_id, code=code, name="海外客户",
        email="c@example.com", country="US", address="1 Main St",
    )


async def _order(svc, ids, customer_id):
    return await svc.create_order(
        tenant_id=ids["tenant_id"], customer_id=customer_id,
        items=[
            {"sku_id": ids["sku1"], "warehouse_id": ids["wh"], "qty": 3, "unit_price": "499.00"},
            {"sku_id": ids["sku2"], "warehouse_id": ids["wh"], "qty": 2, "unit_price": "199.00"},
        ],
        user_id="user-1", created_by="seller@example.com",
    )


async def _stock(session, tenant_id, sku_id, warehouse_id):
    return (
        await session.execute(
            select(WarehouseStock).where(
                WarehouseStock.tenant_id == tenant_id,
                WarehouseStock.sku_id == sku_id,
                WarehouseStock.warehouse_id == warehouse_id,
            ).execution_options(populate_existing=True)
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def test_full_sales_flow(db_session):
    ids = await _seed(db_session)
    svc = SalesService(db_session)
    customer = await _customer(svc, ids["tenant_id"])
    order = await _order(svc, ids, customer.id)

    assert order.status == SalesOrderStatus.PENDING
    # 3*499 + 2*199 = 1497 + 398 = 1895
    assert order.total_amount == Decimal("1895.0000")

    await svc.approve(ids["tenant_id"], order.id, approved_by="boss@example.com")
    s1 = await _stock(db_session, ids["tenant_id"], ids["sku1"], ids["wh"])
    assert (s1.on_hand, s1.reserved, s1.available) == (20, 3, 17)   # 审核只预留

    shipment = await svc.create_shipment(ids["tenant_id"], order.id)
    assert shipment.status == ShipmentStatus.DRAFT

    result = await svc.confirm_shipment(
        ids["tenant_id"], shipment.id, carrier="DHL", tracking_no="DHL123"
    )
    assert result["applied"] is True
    assert result["shipped_qty"] == 5
    assert result["order_status"] == "shipped"

    s1 = await _stock(db_session, ids["tenant_id"], ids["sku1"], ids["wh"])
    assert (s1.on_hand, s1.reserved) == (17, 0)    # 出库后实际减少、锁定归零

    # 物流签收 → 订单完成
    await svc.add_tracking(
        tenant_id=ids["tenant_id"], shipment_id=shipment.id,
        carrier="DHL", tracking_no="DHL123", status=TrackingStatus.DELIVERED,
        description="已签收",
    )
    refreshed = await svc.get_order(ids["tenant_id"], order.id)
    assert refreshed.status == SalesOrderStatus.COMPLETED
    assert refreshed.completed_at is not None

    # 出库流水
    txns = (
        await db_session.execute(
            select(StockTransaction).where(StockTransaction.txn_type == "outbound")
            if False else
            select(StockTransaction).where(StockTransaction.ref_type == "sales_order")
        )
    ).scalars().all()
    assert len(txns) >= 2


async def test_approve_insufficient_stock(db_session):
    ids = await _seed(db_session, on_hand=1)
    svc = SalesService(db_session)
    customer = await _customer(svc, ids["tenant_id"], code="CUS-2")
    order = await _order(svc, ids, customer.id)
    # 必须在可能 rollback 之前取成普通值：失败路径 rollback 后 order 会过期，
    # 读 order.id 会触发隐式 IO（这是本仓库第 4 次踩同一个坑）。
    order_id = order.id

    from app.services.inventory_service import InsufficientStock

    with pytest.raises(InsufficientStock):
        await svc.approve(ids["tenant_id"], order_id, approved_by="boss")

    # 状态没变，库存也没被占
    # 注意：这里用裸 SQL 断言。失败路径里 InventoryService 做了一次 rollback，
    # 同 session 里的 ORM 对象全部过期，再走 ORM 会触发隐式 IO（MissingGreenlet）。
    from sqlalchemy import text

    status = (
        await db_session.execute(
            text("select status::text from sales_orders where id = :i"),
            {"i": order_id},
        )
    ).scalar_one()
    assert status == "pending"

    reserved = (
        await db_session.execute(
            text("select reserved from warehouse_stocks "
                 "where tenant_id = :t and sku_id = :s and warehouse_id = :w"),
            {"t": ids["tenant_id"], "s": ids["sku1"], "w": ids["wh"]},
        )
    ).scalar_one()
    assert reserved == 0


async def test_cancel_after_approve_releases(db_session):
    ids = await _seed(db_session)
    svc = SalesService(db_session)
    customer = await _customer(svc, ids["tenant_id"], code="CUS-3")
    order = await _order(svc, ids, customer.id)
    await svc.approve(ids["tenant_id"], order.id, approved_by="boss")

    s1 = await _stock(db_session, ids["tenant_id"], ids["sku1"], ids["wh"])
    assert s1.reserved == 3

    cancelled = await svc.cancel(ids["tenant_id"], order.id)
    assert cancelled.status == SalesOrderStatus.CANCELLED

    s1 = await _stock(db_session, ids["tenant_id"], ids["sku1"], ids["wh"])
    assert s1.reserved == 0
    assert s1.available == 20


async def test_partial_shipment(db_session):
    ids = await _seed(db_session)
    svc = SalesService(db_session)
    customer = await _customer(svc, ids["tenant_id"], code="CUS-4")
    order = await _order(svc, ids, customer.id)
    await svc.approve(ids["tenant_id"], order.id, approved_by="boss")

    shipment = await svc.create_shipment(ids["tenant_id"], order.id)
    items = (
        await db_session.execute(
            select(__import__("app.models.sales", fromlist=["SalesOrderItem"]).SalesOrderItem)
            .where(__import__("app.models.sales", fromlist=["SalesOrderItem"]).SalesOrderItem.sales_order_id == order.id)
        )
    ).scalars().all()
    first = items[0]

    r1 = await svc.confirm_shipment(
        ids["tenant_id"], shipment.id,
        lines=[ShipLine(sales_order_item_id=first.id, qty=1)],
    )
    assert r1["order_status"] == "picking"

    shipment2 = await svc.create_shipment(ids["tenant_id"], order.id)
    r2 = await svc.confirm_shipment(ids["tenant_id"], shipment2.id)
    assert r2["order_status"] == "shipped"

    refreshed = await svc.get_order(ids["tenant_id"], order.id)
    final_items = (
        await db_session.execute(
            select(__import__("app.models.sales", fromlist=["SalesOrderItem"]).SalesOrderItem)
            .where(__import__("app.models.sales", fromlist=["SalesOrderItem"]).SalesOrderItem.sales_order_id == order.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert all(i.shipped_qty == i.qty for i in final_items)


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------
async def test_cannot_ship_before_approve(db_session):
    ids = await _seed(db_session)
    svc = SalesService(db_session)
    customer = await _customer(svc, ids["tenant_id"], code="CUS-5")
    order = await _order(svc, ids, customer.id)

    with pytest.raises(SalesTransitionError):
        await svc.create_shipment(ids["tenant_id"], order.id)


async def test_cannot_approve_twice(db_session):
    ids = await _seed(db_session)
    svc = SalesService(db_session)
    customer = await _customer(svc, ids["tenant_id"], code="CUS-6")
    order = await _order(svc, ids, customer.id)
    await svc.approve(ids["tenant_id"], order.id, approved_by="boss")

    with pytest.raises(SalesTransitionError):
        await svc.approve(ids["tenant_id"], order.id, approved_by="boss")


async def test_cannot_cancel_shipped(db_session):
    ids = await _seed(db_session)
    svc = SalesService(db_session)
    customer = await _customer(svc, ids["tenant_id"], code="CUS-7")
    order = await _order(svc, ids, customer.id)
    await svc.approve(ids["tenant_id"], order.id, approved_by="boss")
    shipment = await svc.create_shipment(ids["tenant_id"], order.id)
    await svc.confirm_shipment(ids["tenant_id"], shipment.id)

    with pytest.raises(SalesTransitionError):
        await svc.cancel(ids["tenant_id"], order.id)


# ---------------------------------------------------------------------------
# 数量与幂等
# ---------------------------------------------------------------------------
async def test_over_ship_rejected(db_session):
    ids = await _seed(db_session)
    svc = SalesService(db_session)
    customer = await _customer(svc, ids["tenant_id"], code="CUS-8")
    order = await _order(svc, ids, customer.id)
    await svc.approve(ids["tenant_id"], order.id, approved_by="boss")
    shipment = await svc.create_shipment(ids["tenant_id"], order.id)

    from app.models.sales import SalesOrderItem

    items = (
        await db_session.execute(
            select(SalesOrderItem).where(SalesOrderItem.sales_order_id == order.id)
        )
    ).scalars().all()

    with pytest.raises(ShipmentQuantityError):
        await svc.confirm_shipment(
            ids["tenant_id"], shipment.id,
            lines=[ShipLine(sales_order_item_id=items[0].id, qty=items[0].qty + 1)],
        )


async def test_confirm_shipment_idempotent(db_session):
    ids = await _seed(db_session)
    svc = SalesService(db_session)
    customer = await _customer(svc, ids["tenant_id"], code="CUS-9")
    order = await _order(svc, ids, customer.id)
    await svc.approve(ids["tenant_id"], order.id, approved_by="boss")
    shipment = await svc.create_shipment(ids["tenant_id"], order.id)

    first = await svc.confirm_shipment(ids["tenant_id"], shipment.id)
    second = await svc.confirm_shipment(ids["tenant_id"], shipment.id)

    assert first["applied"] is True
    assert second["applied"] is False
    assert second["shipped_qty"] == 0

    s1 = await _stock(db_session, ids["tenant_id"], ids["sku1"], ids["wh"])
    assert s1.on_hand == 17      # 没有二次扣减


# ---------------------------------------------------------------------------
# 隔离与合并视图
# ---------------------------------------------------------------------------
async def test_cross_tenant_order_not_found(db_session):
    ids = await _seed(db_session)
    svc = SalesService(db_session)
    customer = await _customer(svc, ids["tenant_id"], code="CUS-10")
    order = await _order(svc, ids, customer.id)

    with pytest.raises(SalesNotFound):
        await svc.get_order(999, order.id)


async def test_duplicate_customer_code(db_session):
    ids = await _seed(db_session)
    svc = SalesService(db_session)
    await _customer(svc, ids["tenant_id"], code="CUS-11")
    with pytest.raises(SalesError):
        await _customer(svc, ids["tenant_id"], code="CUS-11")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
async def test_api_sales_flow(client, db_session, make_user, auth_headers):
    ids = await _seed(db_session, default_tenant=True, code="api")
    token = await make_user("seller@example.com")
    h = auth_headers(token)

    r = await client.post("/api/v1/sales/customers",
                          json={"code": "AC1", "name": "API客户"}, headers=h)
    assert r.status_code == 201, r.text
    customer_id = r.json()["data"]["id"]

    r = await client.post("/api/v1/sales/orders", json={
        "customer_id": customer_id,
        "items": [{"sku_id": ids["sku1"], "warehouse_id": ids["wh"],
                   "qty": 2, "unit_price": "480.00"}],
    }, headers=h)
    assert r.status_code == 201, r.text
    order_id = r.json()["data"]["id"]
    assert r.json()["data"]["total_amount"] == "960.0000"

    assert (await client.post(f"/api/v1/sales/orders/{order_id}/approve", headers=h)).status_code == 200

    r = await client.post(f"/api/v1/sales/orders/{order_id}/shipments", headers=h)
    assert r.status_code == 201, r.text
    shipment_id = r.json()["data"]["shipment_id"]

    r = await client.post(f"/api/v1/sales/shipments/{shipment_id}/confirm",
                          json={"carrier": "UPS", "tracking_no": "UPS9"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["order_status"] == "shipped"

    r = await client.post(f"/api/v1/sales/shipments/{shipment_id}/tracking", json={
        "carrier": "UPS", "tracking_no": "UPS9",
        "status": "delivered", "description": "已签收",
    }, headers=h)
    assert r.status_code == 201, r.text

    detail = await client.get(f"/api/v1/sales/orders/{order_id}", headers=h)
    assert detail.json()["data"]["status"] == "completed"


async def test_api_unified_orders(client, db_session, make_user, auth_headers):
    """`/orders/all` 要把秒杀订单和销售订单合并成一份。"""
    ids = await _seed(db_session, default_tenant=True, code="uni")
    token = await make_user("uni@example.com")
    h = auth_headers(token)

    r = await client.post("/api/v1/sales/customers",
                          json={"code": "UC1", "name": "合并客户"}, headers=h)
    customer_id = r.json()["data"]["id"]
    await client.post("/api/v1/sales/orders", json={
        "customer_id": customer_id,
        "items": [{"sku_id": ids["sku1"], "warehouse_id": ids["wh"],
                   "qty": 1, "unit_price": "100.00"}],
    }, headers=h)

    r = await client.get("/api/v1/orders/all", headers=h)
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    assert any(x["order_type"] == "sales" for x in rows)
    assert all("order_type" in x and "total_amount" in x for x in rows)


async def test_api_sales_requires_auth(client):
    assert (await client.get("/api/v1/sales/orders")).status_code == 401
