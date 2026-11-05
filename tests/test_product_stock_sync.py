"""第 4 步：`products.stock` 降级为只读汇总。

两条不变量：
1. 库存变动后，`products.stock` 自动等于该商品所有 SKU 的 on_hand 合计；
2. 任何绕过库存服务、直接改 `products.stock` 的请求都被拒绝（409）。
"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.inventory import SKU, Warehouse, WarehouseStock
from app.models.product import Product
from app.models.tenant import Tenant
from app.services.inventory_service import InventoryService


async def _setup(session, product_stock=0, on_hand=10):
    tenant = Tenant(code="t-sync", name="同步租户", status="active")
    session.add(tenant)
    await session.flush()

    product = Product(name="同步测试商品", description="t", price=100.0,
                      stock=product_stock, is_on_sale=True)
    session.add(product)
    await session.flush()

    sku = SKU(tenant_id=tenant.id, product_id=product.id, sku_code="SYNC-1",
              spec={}, sale_price=Decimal("10.0000"))
    wh1 = Warehouse(tenant_id=tenant.id, code="W1", name="仓1", country="CN")
    wh2 = Warehouse(tenant_id=tenant.id, code="W2", name="仓2", country="CN")
    session.add_all([sku, wh1, wh2])
    await session.flush()

    session.add_all([
        WarehouseStock(tenant_id=tenant.id, sku_id=sku.id, warehouse_id=wh1.id,
                       on_hand=on_hand, reserved=0),
        WarehouseStock(tenant_id=tenant.id, sku_id=sku.id, warehouse_id=wh2.id,
                       on_hand=0, reserved=0),
    ])
    await session.commit()
    return {
        "tenant_id": tenant.id, "product_id": product.id, "sku_id": sku.id,
        "wh1": wh1.id, "wh2": wh2.id,
    }


async def _product_stock(session, product_id):
    return (
        await session.execute(select(Product.stock).where(Product.id == product_id))
    ).scalar_one()


async def test_stock_synced_after_inbound(db_session):
    ids = await _setup(db_session, product_stock=0, on_hand=10)
    svc = InventoryService(db_session)

    await svc.inbound(tenant_id=ids["tenant_id"], sku_id=ids["sku_id"],
                      warehouse_id=ids["wh1"], qty=5, idempotency_key="s-i1")

    assert await _product_stock(db_session, ids["product_id"]) == 15


async def test_stock_synced_after_outbound(db_session):
    ids = await _setup(db_session, product_stock=10, on_hand=10)
    svc = InventoryService(db_session)

    await svc.outbound(tenant_id=ids["tenant_id"], sku_id=ids["sku_id"],
                       warehouse_id=ids["wh1"], qty=4, idempotency_key="s-o1")

    assert await _product_stock(db_session, ids["product_id"]) == 6


async def test_stock_is_sum_across_warehouses(db_session):
    ids = await _setup(db_session, product_stock=0, on_hand=10)
    svc = InventoryService(db_session)

    await svc.inbound(tenant_id=ids["tenant_id"], sku_id=ids["sku_id"],
                      warehouse_id=ids["wh2"], qty=7, idempotency_key="s-w2")

    # 10（仓1）+ 7（仓2）= 17
    assert await _product_stock(db_session, ids["product_id"]) == 17


async def test_stock_synced_after_reserve_and_commit(db_session):
    """预留只动 locked，不改汇总；核销才真正减少。"""
    ids = await _setup(db_session, product_stock=10, on_hand=10)
    svc = InventoryService(db_session)

    r = await svc.reserve(tenant_id=ids["tenant_id"], sku_id=ids["sku_id"],
                          warehouse_id=ids["wh1"], qty=3, idempotency_key="s-r1",
                          ref_type="sales_order", ref_id="SO-1")
    assert await _product_stock(db_session, ids["product_id"]) == 10

    await svc.commit_reservation(tenant_id=ids["tenant_id"],
                                 reservation_id=r.reservation_id,
                                 idempotency_key="s-c1")
    assert await _product_stock(db_session, ids["product_id"]) == 7


async def test_adjust_syncs_stock(db_session):
    ids = await _setup(db_session, product_stock=10, on_hand=10)
    svc = InventoryService(db_session)

    await svc.adjust(tenant_id=ids["tenant_id"], sku_id=ids["sku_id"],
                     warehouse_id=ids["wh1"], counted_on_hand=8,
                     idempotency_key="s-a1")

    assert await _product_stock(db_session, ids["product_id"]) == 8


# ---------------------------------------------------------------------------
# API 层：禁止直接改 products.stock
# ---------------------------------------------------------------------------
async def test_api_rejects_direct_stock_update(client, make_product):
    p = await make_product(stock=10)

    r = await client.put(f"/api/v1/products/{p['id']}", json={"stock": 3})
    assert r.status_code == 409, r.text
    assert "库存" in r.json()["detail"]

    # 库存没被改掉
    r = await client.get(f"/api/v1/products/{p['id']}")
    assert r.json()["stock"] == 10


async def test_api_still_allows_other_fields(client, make_product):
    """拦的只是 stock，其它字段照常可改 —— 不能把整个更新接口废掉。"""
    p = await make_product(price=100.0, stock=10)

    r = await client.put(f"/api/v1/products/{p['id']}", json={"price": 88.0, "name": "改名"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert float(body["price"]) == 88.0
    assert body["name"] == "改名"
    assert body["stock"] == 10
