"""第二阶段：采购流程测试。

覆盖状态机、数量校验、幂等、以及"收货必须真的进库存"。
库存部分不重复实现，直接验证 `InventoryService.inbound` 的效果。
"""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.inventory import SKU, Warehouse, WarehouseStock
from app.models.product import Product
from app.models.purchase import (
    POStatus,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReceipt,
    ReceiptStatus,
)
from app.models.tenant import Tenant
from app.services.purchase_service import (
    InvalidTransition,
    PurchaseError,
    PurchaseNotFound,
    PurchaseService,
    ReceiptLine,
    ReceiptQuantityError,
)


# ---------------------------------------------------------------------------
# 数据准备
# ---------------------------------------------------------------------------
async def _seed(session, tenant_code="t-buy", default_tenant=False):
    """建一套采购测试数据。

    `default_tenant=True` 时复用默认租户（id=1）——API 用例必须这样做，
    因为接口里的租户取自登录用户（第一阶段固定默认租户）。
    """
    if default_tenant:
        tenant_id = 1
    else:
        tenant = Tenant(code=tenant_code, name="采购租户", status="active")
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id

    product = Product(tenant_id=tenant_id, name="采购商品", description="t",
                      price=Decimal("100.0000"), stock=0, is_on_sale=True)
    session.add(product)
    await session.flush()

    sku1 = SKU(tenant_id=tenant_id, product_id=product.id, sku_code="BUY-1",
               spec={}, sale_price=Decimal("100.0000"))
    sku2 = SKU(tenant_id=tenant_id, product_id=product.id, sku_code="BUY-2",
               spec={}, sale_price=Decimal("50.0000"))
    wh = Warehouse(tenant_id=tenant_id, code="W-BUY", name="采购仓", country="CN")
    session.add_all([sku1, sku2, wh])
    await session.flush()

    session.add_all([
        WarehouseStock(tenant_id=tenant_id, sku_id=sku1.id, warehouse_id=wh.id,
                       on_hand=0, reserved=0),
        WarehouseStock(tenant_id=tenant_id, sku_id=sku2.id, warehouse_id=wh.id,
                       on_hand=0, reserved=0),
    ])
    await session.commit()

    return {
        "tenant_id": tenant_id, "product_id": product.id,
        "sku1": sku1.id, "sku2": sku2.id, "wh": wh.id,
    }


async def _supplier(svc, tenant_id):
    return await svc.create_supplier(
        tenant_id=tenant_id, code="SUP-1", name="深圳供应商", email="s@example.com"
    )


async def _draft_po(svc, ids, supplier_id):
    return await svc.create_po(
        tenant_id=ids["tenant_id"], supplier_id=supplier_id,
        items=[
            {"sku_id": ids["sku1"], "warehouse_id": ids["wh"], "qty": 10, "unit_price": "99.50"},
            {"sku_id": ids["sku2"], "warehouse_id": ids["wh"], "qty": 4, "unit_price": "20.25"},
        ],
        created_by="buyer@example.com",
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
async def test_full_purchase_flow(db_session):
    ids = await _seed(db_session)
    svc = PurchaseService(db_session)
    supplier = await _supplier(svc, ids["tenant_id"])

    po = await _draft_po(svc, ids, supplier.id)
    assert po.status == POStatus.DRAFT
    # 10*99.5 + 4*20.25 = 995 + 81 = 1076
    assert po.total_amount == Decimal("1076.0000")

    await svc.submit(ids["tenant_id"], po.id)
    await svc.approve(ids["tenant_id"], po.id, approved_by="boss@example.com")

    receipt = await svc.create_receipt_task(ids["tenant_id"], po.id)
    assert receipt.status == ReceiptStatus.DRAFT

    result = await svc.confirm_receipt(ids["tenant_id"], receipt.id)
    assert result["applied"] is True
    assert result["inbound_qty"] == 14
    assert result["po_status"] == "completed"

    # 库存真的进了
    s1 = await _stock(db_session, ids["tenant_id"], ids["sku1"], ids["wh"])
    s2 = await _stock(db_session, ids["tenant_id"], ids["sku2"], ids["wh"])
    assert (s1.on_hand, s2.on_hand) == (10, 4)

    # 商品派生汇总也同步
    from app.models.product import Product as P

    product_stock = (
        await db_session.execute(
            select(P.stock).where(P.id == ids["product_id"]).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert product_stock == 14


async def test_partial_receipt_then_rest(db_session):
    ids = await _seed(db_session, tenant_code="t-partial")
    svc = PurchaseService(db_session)
    supplier = await _supplier(svc, ids["tenant_id"])
    po = await _draft_po(svc, ids, supplier.id)
    await svc.submit(ids["tenant_id"], po.id)
    await svc.approve(ids["tenant_id"], po.id, approved_by="boss")

    # 第一次只收 6 件 SKU1
    r1 = await svc.create_receipt_task(ids["tenant_id"], po.id)
    items = (
        await db_session.execute(
            select(PurchaseOrderItem).where(PurchaseOrderItem.po_id == po.id)
        )
    ).scalars().all()
    sku1_item = next(i for i in items if i.sku_id == ids["sku1"])

    first = await svc.confirm_receipt(
        ids["tenant_id"], r1.id, lines=[ReceiptLine(po_item_id=sku1_item.id, qty=6)]
    )
    assert first["po_status"] == "receiving"

    # 第二次收剩下的
    r2 = await svc.create_receipt_task(ids["tenant_id"], po.id)
    second = await svc.confirm_receipt(ids["tenant_id"], r2.id)
    assert second["po_status"] == "completed"

    s1 = await _stock(db_session, ids["tenant_id"], ids["sku1"], ids["wh"])
    assert s1.on_hand == 10


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------
async def test_cannot_approve_draft(db_session):
    ids = await _seed(db_session, tenant_code="t-sm")
    svc = PurchaseService(db_session)
    supplier = await _supplier(svc, ids["tenant_id"])
    po = await _draft_po(svc, ids, supplier.id)

    with pytest.raises(InvalidTransition):
        await svc.approve(ids["tenant_id"], po.id, approved_by="boss")


async def test_cannot_submit_twice(db_session):
    ids = await _seed(db_session, tenant_code="t-sm2")
    svc = PurchaseService(db_session)
    supplier = await _supplier(svc, ids["tenant_id"])
    po = await _draft_po(svc, ids, supplier.id)

    await svc.submit(ids["tenant_id"], po.id)
    with pytest.raises(InvalidTransition):
        await svc.submit(ids["tenant_id"], po.id)


async def test_cannot_cancel_after_completed(db_session):
    ids = await _seed(db_session, tenant_code="t-sm3")
    svc = PurchaseService(db_session)
    supplier = await _supplier(svc, ids["tenant_id"])
    po = await _draft_po(svc, ids, supplier.id)
    await svc.submit(ids["tenant_id"], po.id)
    await svc.approve(ids["tenant_id"], po.id, approved_by="boss")
    r = await svc.create_receipt_task(ids["tenant_id"], po.id)
    await svc.confirm_receipt(ids["tenant_id"], r.id)

    with pytest.raises(InvalidTransition):
        await svc.cancel(ids["tenant_id"], po.id)


async def test_receipt_requires_approved(db_session):
    ids = await _seed(db_session, tenant_code="t-sm4")
    svc = PurchaseService(db_session)
    supplier = await _supplier(svc, ids["tenant_id"])
    po = await _draft_po(svc, ids, supplier.id)

    with pytest.raises(InvalidTransition):
        await svc.create_receipt_task(ids["tenant_id"], po.id)


async def test_reject_flow(db_session):
    ids = await _seed(db_session, tenant_code="t-sm5")
    svc = PurchaseService(db_session)
    supplier = await _supplier(svc, ids["tenant_id"])
    po = await _draft_po(svc, ids, supplier.id)
    await svc.submit(ids["tenant_id"], po.id)

    rejected = await svc.reject(ids["tenant_id"], po.id, "价格过高")
    assert rejected.status == POStatus.REJECTED
    with pytest.raises(InvalidTransition):
        await svc.approve(ids["tenant_id"], po.id, approved_by="boss")


# ---------------------------------------------------------------------------
# 数量与幂等
# ---------------------------------------------------------------------------
async def test_over_receive_rejected(db_session):
    ids = await _seed(db_session, tenant_code="t-qty")
    svc = PurchaseService(db_session)
    supplier = await _supplier(svc, ids["tenant_id"])
    po = await _draft_po(svc, ids, supplier.id)
    await svc.submit(ids["tenant_id"], po.id)
    await svc.approve(ids["tenant_id"], po.id, approved_by="boss")
    r = await svc.create_receipt_task(ids["tenant_id"], po.id)

    items = (
        await db_session.execute(
            select(PurchaseOrderItem).where(PurchaseOrderItem.po_id == po.id)
        )
    ).scalars().all()
    target = items[0]

    with pytest.raises(ReceiptQuantityError):
        await svc.confirm_receipt(
            ids["tenant_id"], r.id,
            lines=[ReceiptLine(po_item_id=target.id, qty=target.qty + 1)],
        )


async def test_confirm_is_idempotent(db_session):
    """重复确认不能二次入库。"""
    ids = await _seed(db_session, tenant_code="t-idem")
    svc = PurchaseService(db_session)
    supplier = await _supplier(svc, ids["tenant_id"])
    po = await _draft_po(svc, ids, supplier.id)
    await svc.submit(ids["tenant_id"], po.id)
    await svc.approve(ids["tenant_id"], po.id, approved_by="boss")
    r = await svc.create_receipt_task(ids["tenant_id"], po.id)

    first = await svc.confirm_receipt(ids["tenant_id"], r.id)
    second = await svc.confirm_receipt(ids["tenant_id"], r.id)

    assert first["applied"] is True
    assert second["applied"] is False
    assert second["inbound_qty"] == 0

    s1 = await _stock(db_session, ids["tenant_id"], ids["sku1"], ids["wh"])
    assert s1.on_hand == 10          # 没有翻倍


async def test_receipt_writes_ledger(db_session):
    from app.models.inventory import StockTransaction

    ids = await _seed(db_session, tenant_code="t-ledger")
    svc = PurchaseService(db_session)
    supplier = await _supplier(svc, ids["tenant_id"])
    po = await _draft_po(svc, ids, supplier.id)
    await svc.submit(ids["tenant_id"], po.id)
    await svc.approve(ids["tenant_id"], po.id, approved_by="boss")
    r = await svc.create_receipt_task(ids["tenant_id"], po.id)
    await svc.confirm_receipt(ids["tenant_id"], r.id)

    txns = (
        await db_session.execute(
            select(StockTransaction).where(StockTransaction.ref_type == "purchase_receipt")
        )
    ).scalars().all()
    assert len(txns) == 2
    assert all(t.txn_type.value == "inbound" for t in txns)


# ---------------------------------------------------------------------------
# 校验与隔离
# ---------------------------------------------------------------------------
async def test_po_requires_items(db_session):
    ids = await _seed(db_session, tenant_code="t-empty")
    svc = PurchaseService(db_session)
    supplier = await _supplier(svc, ids["tenant_id"])
    with pytest.raises(PurchaseError):
        await svc.create_po(tenant_id=ids["tenant_id"], supplier_id=supplier.id, items=[])


async def test_duplicate_sku_warehouse_rejected(db_session):
    ids = await _seed(db_session, tenant_code="t-dup")
    svc = PurchaseService(db_session)
    supplier = await _supplier(svc, ids["tenant_id"])
    with pytest.raises(PurchaseError):
        await svc.create_po(
            tenant_id=ids["tenant_id"], supplier_id=supplier.id,
            items=[
                {"sku_id": ids["sku1"], "warehouse_id": ids["wh"], "qty": 1, "unit_price": "1"},
                {"sku_id": ids["sku1"], "warehouse_id": ids["wh"], "qty": 2, "unit_price": "1"},
            ],
        )


async def test_cross_tenant_po_not_visible(db_session):
    ids = await _seed(db_session, tenant_code="t-iso")
    svc = PurchaseService(db_session)
    supplier = await _supplier(svc, ids["tenant_id"])
    po = await _draft_po(svc, ids, supplier.id)

    with pytest.raises(PurchaseNotFound):
        await svc.get_po(999, po.id)


async def test_duplicate_supplier_code(db_session):
    ids = await _seed(db_session, tenant_code="t-sup")
    svc = PurchaseService(db_session)
    await _supplier(svc, ids["tenant_id"])
    with pytest.raises(PurchaseError):
        await _supplier(svc, ids["tenant_id"])


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------
async def test_api_purchase_flow(client, db_session, make_user, auth_headers):
    ids = await _seed(db_session, default_tenant=True)
    token = await make_user("buyer@example.com")
    h = auth_headers(token)

    r = await client.post("/api/v1/purchase/suppliers",
                          json={"code": "S1", "name": "API供应商"}, headers=h)
    assert r.status_code == 201, r.text
    supplier_id = r.json()["data"]["id"]

    r = await client.post("/api/v1/purchase/orders", json={
        "supplier_id": supplier_id,
        "items": [{"sku_id": ids["sku1"], "warehouse_id": ids["wh"],
                   "qty": 5, "unit_price": "10.00"}],
    }, headers=h)
    assert r.status_code == 201, r.text
    po_id = r.json()["data"]["id"]
    assert r.json()["data"]["total_amount"] == "50.0000"

    assert (await client.post(f"/api/v1/purchase/orders/{po_id}/submit", headers=h)).status_code == 200
    assert (await client.post(f"/api/v1/purchase/orders/{po_id}/approve", headers=h)).status_code == 200

    r = await client.post(f"/api/v1/purchase/orders/{po_id}/receipts", headers=h)
    assert r.status_code == 201, r.text
    receipt_id = r.json()["data"]["receipt_id"]

    r = await client.post(f"/api/v1/purchase/receipts/{receipt_id}/confirm", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["po_status"] == "completed"

    detail = await client.get(f"/api/v1/purchase/orders/{po_id}", headers=h)
    assert detail.status_code == 200
    assert detail.json()["data"]["items"][0]["received_qty"] == 5


async def test_api_invalid_transition_returns_409(client, db_session, make_user, auth_headers):
    ids = await _seed(db_session, default_tenant=True)
    token = await make_user("buyer2@example.com")
    h = auth_headers(token)

    r = await client.post("/api/v1/purchase/suppliers",
                          json={"code": "S2", "name": "供应商2"}, headers=h)
    supplier_id = r.json()["data"]["id"]
    r = await client.post("/api/v1/purchase/orders", json={
        "supplier_id": supplier_id,
        "items": [{"sku_id": ids["sku1"], "warehouse_id": ids["wh"],
                   "qty": 1, "unit_price": "1.00"}],
    }, headers=h)
    po_id = r.json()["data"]["id"]

    # 草稿直接审核 → 409
    r = await client.post(f"/api/v1/purchase/orders/{po_id}/approve", headers=h)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "INVALID_TRANSITION"


async def test_api_requires_auth(client):
    assert (await client.get("/api/v1/purchase/orders")).status_code == 401
