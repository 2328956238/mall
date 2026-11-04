"""库存域表结构与约束测试（第一阶段 第 2 步）。

这些测试锁的是**数据库层面的不变量**，不是业务逻辑：
唯一键、检查约束、外键、Decimal 精度、枚举存值方式。
业务逻辑（入库/出库/预留…）在下一步的 InventoryService 里测。
"""

from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.inventory import (
    ReservationStatus,
    SKU,
    SkuStatus,
    StockReservation,
    StockTransaction,
    StockTxnType,
    Warehouse,
    WarehouseStock,
)
from app.models.tenant import Tenant


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
async def _tenant(session, code="t-schema", name="测试租户") -> Tenant:
    t = Tenant(code=code, name=name, status="active")
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return t


async def _product(session, name="iPhone 15") -> int:
    from app.models.product import Product

    p = Product(name=name, description="t", price=100.0, stock=0, is_on_sale=True)
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return p.id


async def _sku(session, tenant, product_id, code="SKU-001", price=Decimal("19.9900")) -> SKU:
    s = SKU(
        tenant_id=tenant.id,
        product_id=product_id,
        sku_code=code,
        spec={"color": "black", "storage": "128G"},
        sale_price=price,
        status=SkuStatus.ACTIVE,
    )
    session.add(s)
    await session.commit()
    await session.refresh(s)
    return s


async def _warehouse(session, tenant, code="SZ") -> Warehouse:
    w = Warehouse(tenant_id=tenant.id, code=code, name="深圳仓", country="CN")
    session.add(w)
    await session.commit()
    await session.refresh(w)
    return w


async def _stock(session, tenant, sku, wh, on_hand=10, reserved=0) -> WarehouseStock:
    st = WarehouseStock(
        tenant_id=tenant.id,
        sku_id=sku.id,
        warehouse_id=wh.id,
        on_hand=on_hand,
        reserved=reserved,
    )
    session.add(st)
    await session.commit()
    await session.refresh(st)
    return st


# ---------------------------------------------------------------------------
# 结构
# ---------------------------------------------------------------------------
async def test_inventory_tables_exist(db_session):
    rows = await db_session.execute(
        text(
            "select table_name from information_schema.tables "
            "where table_schema='public' order by table_name"
        )
    )
    names = {r[0] for r in rows}
    assert {
        "tenants",
        "skus",
        "warehouses",
        "warehouse_stocks",
        "stock_transactions",
        "stock_reservations",
    } <= names


async def test_tenant_uniqueness(db_session):
    await _tenant(db_session, code="acme")
    with pytest.raises(IntegrityError):
        await _tenant(db_session, code="acme")


async def test_sku_code_unique_per_tenant(db_session):
    t = await _tenant(db_session)
    pid = await _product(db_session)
    await _sku(db_session, t, pid, code="A-1")

    with pytest.raises(IntegrityError):
        await _sku(db_session, t, pid, code="A-1")


async def test_sku_code_reusable_across_tenants(db_session):
    """不同租户可以用同样的 SKU 编码 —— 这是多租户的基本要求。"""
    t1 = await _tenant(db_session, code="t1")
    t2 = await _tenant(db_session, code="t2")
    pid = await _product(db_session)

    await _sku(db_session, t1, pid, code="SAME")
    await _sku(db_session, t2, pid, code="SAME")


async def test_same_sku_in_multiple_warehouses(db_session):
    t = await _tenant(db_session)
    pid = await _product(db_session)
    sku = await _sku(db_session, t, pid)
    w1 = await _warehouse(db_session, t, code="SZ")
    w2 = await _warehouse(db_session, t, code="HK")

    await _stock(db_session, t, sku, w1, on_hand=5)
    await _stock(db_session, t, sku, w2, on_hand=7)

    total = (
        await db_session.execute(
            select(WarehouseStock).where(WarehouseStock.sku_id == sku.id)
        )
    ).scalars().all()
    assert len(total) == 2
    assert sum(s.on_hand for s in total) == 12


async def test_stock_unique_per_sku_warehouse(db_session):
    t = await _tenant(db_session)
    pid = await _product(db_session)
    sku = await _sku(db_session, t, pid)
    w = await _warehouse(db_session, t)

    await _stock(db_session, t, sku, w, on_hand=5)
    with pytest.raises(IntegrityError):
        await _stock(db_session, t, sku, w, on_hand=6)


# ---------------------------------------------------------------------------
# 约束
# ---------------------------------------------------------------------------
async def test_on_hand_cannot_be_negative(db_session):
    t = await _tenant(db_session)
    pid = await _product(db_session)
    sku = await _sku(db_session, t, pid)
    w = await _warehouse(db_session, t)

    with pytest.raises(IntegrityError):
        await _stock(db_session, t, sku, w, on_hand=-1)


async def test_reserved_cannot_exceed_on_hand(db_session):
    """锁定库存不能超过实际库存 —— 这是防超卖的数据库兜底。"""
    t = await _tenant(db_session)
    pid = await _product(db_session)
    sku = await _sku(db_session, t, pid)
    w = await _warehouse(db_session, t)

    with pytest.raises(IntegrityError):
        await _stock(db_session, t, sku, w, on_hand=5, reserved=6)


async def test_available_is_computed(db_session):
    t = await _tenant(db_session)
    pid = await _product(db_session)
    sku = await _sku(db_session, t, pid)
    w = await _warehouse(db_session, t)
    st = await _stock(db_session, t, sku, w, on_hand=10, reserved=3)

    assert st.available == 7


async def test_sku_fk_requires_real_product(db_session):
    t = await _tenant(db_session)
    with pytest.raises(IntegrityError):
        await _sku(db_session, t, 999999)


# ---------------------------------------------------------------------------
# 类型
# ---------------------------------------------------------------------------
async def test_price_is_decimal_not_float(db_session):
    """金额必须精确：Float 存 19.99 会出现 19.989999… 的问题。"""
    t = await _tenant(db_session)
    pid = await _product(db_session)
    sku = await _sku(db_session, t, pid, price=Decimal("19.99"))

    await db_session.refresh(sku)
    assert isinstance(sku.sale_price, Decimal)
    assert sku.sale_price == Decimal("19.9900")

    row = await db_session.execute(
        text("select data_type, numeric_precision, numeric_scale "
             "from information_schema.columns "
             "where table_name='skus' and column_name='sale_price'")
    )
    data_type, precision, scale = row.fetchone()
    assert data_type == "numeric"
    assert (precision, scale) == (18, 4)


async def test_txn_type_stored_as_value(db_session):
    """枚举存 value（inbound）而不是 name（INBOUND）—— 避免 PENDING/pending 混用。"""
    t = await _tenant(db_session)
    pid = await _product(db_session)
    sku = await _sku(db_session, t, pid)
    w = await _warehouse(db_session, t)

    txn = StockTransaction(
        tenant_id=t.id,
        sku_id=sku.id,
        warehouse_id=w.id,
        txn_type=StockTxnType.INBOUND,
        qty_delta=10,
        on_hand_after=10,
        reserved_after=0,
        idempotency_key="idem-1",
    )
    db_session.add(txn)
    await db_session.commit()

    raw = await db_session.execute(
        text("select txn_type::text from stock_transactions where idempotency_key='idem-1'")
    )
    assert raw.scalar_one() == "inbound"


async def test_idempotency_key_unique_per_tenant(db_session):
    t1 = await _tenant(db_session, code="t1")
    t2 = await _tenant(db_session, code="t2")
    pid = await _product(db_session)
    # 先把 id 取成普通 int：rollback 之后 ORM 对象会过期，
    # 再访问 t1.id 会触发隐式 IO -> MissingGreenlet。
    ids = {
        "t1": (t1.id, (await _sku(db_session, t1, pid, code="S1")).id,
               (await _warehouse(db_session, t1, code="W1")).id),
    }
    ids["t2"] = (t2.id, (await _sku(db_session, t2, pid, code="S2")).id,
                 (await _warehouse(db_session, t2, code="W2")).id)

    def _txn(tenant_id, sku_id, warehouse_id, key):
        return StockTransaction(
            tenant_id=tenant_id, sku_id=sku_id, warehouse_id=warehouse_id,
            txn_type=StockTxnType.ADJUST, qty_delta=0,
            on_hand_after=0, reserved_after=0, idempotency_key=key,
        )

    db_session.add(_txn(*ids["t1"], "same-key"))
    await db_session.commit()

    # 同租户重复键 -> 拒绝
    db_session.add(_txn(*ids["t1"], "same-key"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    # 不同租户同键 -> 允许
    db_session.add(_txn(*ids["t2"], "same-key"))
    await db_session.commit()


async def test_reservation_default_status(db_session):
    t = await _tenant(db_session)
    pid = await _product(db_session)
    sku = await _sku(db_session, t, pid)
    w = await _warehouse(db_session, t)

    r = StockReservation(
        tenant_id=t.id, sku_id=sku.id, warehouse_id=w.id, qty=2,
        ref_type="sales_order", ref_id="SO-1", idempotency_key="res-1",
    )
    db_session.add(r)
    await db_session.commit()
    await db_session.refresh(r)

    assert r.status == ReservationStatus.ACTIVE
    assert r.expires_at is None


async def test_reservation_qty_must_be_positive(db_session):
    t = await _tenant(db_session)
    pid = await _product(db_session)
    sku = await _sku(db_session, t, pid)
    w = await _warehouse(db_session, t)

    db_session.add(
        StockReservation(
            tenant_id=t.id, sku_id=sku.id, warehouse_id=w.id, qty=0,
            ref_type="sales_order", ref_id="SO-2", idempotency_key="res-2",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
