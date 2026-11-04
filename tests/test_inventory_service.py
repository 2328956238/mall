"""InventoryService 测试（第一阶段 第 3 步）。

重点覆盖三类性质：
1. **正确性**：六种操作各自的结果与流水类型；
2. **不变量**：可用量不足时必须失败且**不留任何痕迹**（库存不变、不写流水）；
3. **并发与幂等**：并发扣减不超卖、重复幂等键不重复生效。
"""

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.models.inventory import (
    ReservationStatus,
    StockReservation,
    StockTransaction,
    StockTxnType,
)
from app.services.inventory_service import (
    InsufficientStock,
    InventoryError,
    InventoryService,
    ReservationStateError,
    StockRowNotFound,
)


# ---------------------------------------------------------------------------
# 数据准备
# ---------------------------------------------------------------------------
async def _seed(session, on_hand=10, reserved=0):
    """建一个租户 + 一个 SKU + 两个仓库 + 两条库存。返回纯 int 字典。"""
    from app.models.inventory import SKU, Warehouse, WarehouseStock
    from app.models.product import Product
    from app.models.tenant import Tenant

    tenant = Tenant(code="t1", name="租户1", status="active")
    session.add(tenant)
    await session.flush()

    product = Product(name="iPhone 15", description="t", price=100.0, stock=0, is_on_sale=True)
    session.add(product)
    await session.flush()

    sku = SKU(
        tenant_id=tenant.id, product_id=product.id, sku_code="SKU-1",
        spec={"storage": "128G"}, sale_price=Decimal("4999.0000"),
    )
    wh_a = Warehouse(tenant_id=tenant.id, code="SZ", name="深圳仓", country="CN")
    wh_b = Warehouse(tenant_id=tenant.id, code="HK", name="香港仓", country="HK")
    session.add_all([sku, wh_a, wh_b])
    await session.flush()

    session.add_all([
        WarehouseStock(tenant_id=tenant.id, sku_id=sku.id, warehouse_id=wh_a.id,
                       on_hand=on_hand, reserved=reserved),
        WarehouseStock(tenant_id=tenant.id, sku_id=sku.id, warehouse_id=wh_b.id,
                       on_hand=5, reserved=0),
    ])
    await session.commit()

    return {
        "tenant_id": tenant.id, "product_id": product.id, "sku_id": sku.id,
        "wh_a": wh_a.id, "wh_b": wh_b.id,
    }


@pytest.fixture
def ids_factory():
    async def _make(session, **kw):
        return await _seed(session, **kw)

    return _make


async def _txn_count(session, tenant_id):
    return (
        await session.execute(
            select(func.count()).select_from(StockTransaction).where(
                StockTransaction.tenant_id == tenant_id
            )
        )
    ).scalar()


# ---------------------------------------------------------------------------
# ① 入库
# ---------------------------------------------------------------------------
async def test_inbound_increases_stock_and_writes_ledger(db_session, ids_factory):
    ids = await ids_factory(db_session)
    svc = InventoryService(db_session)

    r = await svc.inbound(
        tenant_id=ids["tenant_id"], sku_id=ids["sku_id"], warehouse_id=ids["wh_a"],
        qty=5, idempotency_key="in-1", ref_type="purchase_order", ref_id="PO-1",
    )

    assert r.applied is True
    assert (r.on_hand, r.reserved, r.available) == (15, 0, 15)

    txn = (await db_session.execute(
        select(StockTransaction).where(StockTransaction.idempotency_key == "in-1")
    )).scalar_one()
    assert txn.txn_type == StockTxnType.INBOUND
    assert txn.qty_delta == 5
    assert txn.on_hand_after == 15


async def test_inbound_idempotent(db_session, ids_factory):
    ids = await ids_factory(db_session)
    svc = InventoryService(db_session)

    first = await svc.inbound(
        tenant_id=ids["tenant_id"], sku_id=ids["sku_id"], warehouse_id=ids["wh_a"],
        qty=5, idempotency_key="in-dup",
    )
    second = await svc.inbound(
        tenant_id=ids["tenant_id"], sku_id=ids["sku_id"], warehouse_id=ids["wh_a"],
        qty=5, idempotency_key="in-dup",
    )

    assert first.applied is True
    assert second.applied is False          # 重放，不重复加
    assert second.on_hand == first.on_hand == 15
    assert await _txn_count(db_session, ids["tenant_id"]) == 1


# ---------------------------------------------------------------------------
# ② 出库
# ---------------------------------------------------------------------------
async def test_outbound_decreases(db_session, ids_factory):
    ids = await ids_factory(db_session)
    svc = InventoryService(db_session)

    r = await svc.outbound(
        tenant_id=ids["tenant_id"], sku_id=ids["sku_id"], warehouse_id=ids["wh_a"],
        qty=4, idempotency_key="out-1",
    )
    assert (r.on_hand, r.available) == (6, 6)
    txn = (await db_session.execute(
        select(StockTransaction).where(StockTransaction.idempotency_key == "out-1")
    )).scalar_one()
    assert txn.txn_type == StockTxnType.OUTBOUND and txn.qty_delta == -4


async def test_outbound_insufficient_leaves_no_trace(db_session, ids_factory):
    ids = await ids_factory(db_session, on_hand=3)
    svc = InventoryService(db_session)

    with pytest.raises(InsufficientStock):
        await svc.outbound(
            tenant_id=ids["tenant_id"], sku_id=ids["sku_id"], warehouse_id=ids["wh_a"],
            qty=10, idempotency_key="out-fail",
        )

    stock = await svc.get_stock(ids["tenant_id"], ids["sku_id"], ids["wh_a"])
    assert stock.on_hand == 3                       # 库存没变
    assert await _txn_count(db_session, ids["tenant_id"]) == 0   # 没留流水


async def test_outbound_cannot_consume_reserved(db_session, ids_factory):
    """有 10 件、其中 8 件被锁定，只能出库 2 件。"""
    ids = await ids_factory(db_session, on_hand=10, reserved=8)
    svc = InventoryService(db_session)

    with pytest.raises(InsufficientStock):
        await svc.outbound(
            tenant_id=ids["tenant_id"], sku_id=ids["sku_id"], warehouse_id=ids["wh_a"],
            qty=3, idempotency_key="out-over",
        )

    ok = await svc.outbound(
        tenant_id=ids["tenant_id"], sku_id=ids["sku_id"], warehouse_id=ids["wh_a"],
        qty=2, idempotency_key="out-ok",
    )
    assert ok.available == 0


# ---------------------------------------------------------------------------
# ③ 预留 / ④ 释放 / ④' 核销
# ---------------------------------------------------------------------------
async def test_reserve_and_release(db_session, ids_factory):
    ids = await ids_factory(db_session)
    svc = InventoryService(db_session)

    r = await svc.reserve(
        tenant_id=ids["tenant_id"], sku_id=ids["sku_id"], warehouse_id=ids["wh_a"],
        qty=4, idempotency_key="res-1", ref_type="sales_order", ref_id="SO-1",
    )
    assert r.applied and r.reservation_id
    assert (r.on_hand, r.reserved, r.available) == (10, 4, 6)

    rel = await svc.release(
        tenant_id=ids["tenant_id"], reservation_id=r.reservation_id,
        idempotency_key="rel-1",
    )
    assert rel.applied
    assert (rel.on_hand, rel.reserved, rel.available) == (10, 0, 10)

    res = (await db_session.execute(
        select(StockReservation).where(StockReservation.id == r.reservation_id)
    )).scalar_one()
    assert res.status == ReservationStatus.RELEASED


async def test_reserve_insufficient(db_session, ids_factory):
    ids = await ids_factory(db_session, on_hand=2)
    svc = InventoryService(db_session)
    with pytest.raises(InsufficientStock):
        await svc.reserve(
            tenant_id=ids["tenant_id"], sku_id=ids["sku_id"], warehouse_id=ids["wh_a"],
            qty=5, idempotency_key="res-fail", ref_type="sales_order", ref_id="SO-x",
        )


async def test_release_twice_is_safe(db_session, ids_factory):
    """重复释放不应把锁定量减成负数。"""
    ids = await ids_factory(db_session)
    svc = InventoryService(db_session)

    r = await svc.reserve(
        tenant_id=ids["tenant_id"], sku_id=ids["sku_id"], warehouse_id=ids["wh_a"],
        qty=3, idempotency_key="res-2", ref_type="sales_order", ref_id="SO-2",
    )
    await svc.release(tenant_id=ids["tenant_id"], reservation_id=r.reservation_id,
                      idempotency_key="rel-2a")
    again = await svc.release(tenant_id=ids["tenant_id"], reservation_id=r.reservation_id,
                              idempotency_key="rel-2b")

    assert again.applied is False
    stock = await svc.get_stock(ids["tenant_id"], ids["sku_id"], ids["wh_a"])
    assert stock.reserved == 0


async def test_commit_reservation_decrements_both(db_session, ids_factory):
    """核销：实际与锁定同时减少（真正把货发出去）。"""
    ids = await ids_factory(db_session)
    svc = InventoryService(db_session)

    r = await svc.reserve(
        tenant_id=ids["tenant_id"], sku_id=ids["sku_id"], warehouse_id=ids["wh_a"],
        qty=4, idempotency_key="res-3", ref_type="sales_order", ref_id="SO-3",
    )
    c = await svc.commit_reservation(
        tenant_id=ids["tenant_id"], reservation_id=r.reservation_id,
        idempotency_key="commit-3",
    )
    assert c.applied
    assert (c.on_hand, c.reserved, c.available) == (6, 0, 6)

    res = (await db_session.execute(
        select(StockReservation).where(StockReservation.id == r.reservation_id)
    )).scalar_one()
    assert res.status == ReservationStatus.COMMITTED


async def test_cannot_commit_released_reservation(db_session, ids_factory):
    ids = await ids_factory(db_session)
    svc = InventoryService(db_session)
    r = await svc.reserve(
        tenant_id=ids["tenant_id"], sku_id=ids["sku_id"], warehouse_id=ids["wh_a"],
        qty=2, idempotency_key="res-4", ref_type="sales_order", ref_id="SO-4",
    )
    await svc.release(tenant_id=ids["tenant_id"], reservation_id=r.reservation_id,
                      idempotency_key="rel-4")

    with pytest.raises(ReservationStateError):
        await svc.commit_reservation(
            tenant_id=ids["tenant_id"], reservation_id=r.reservation_id,
            idempotency_key="commit-4",
        )


# ---------------------------------------------------------------------------
# ⑤ 调拨
# ---------------------------------------------------------------------------
async def test_transfer_moves_between_warehouses(db_session, ids_factory):
    ids = await ids_factory(db_session)
    svc = InventoryService(db_session)

    out, inn = await svc.transfer(
        tenant_id=ids["tenant_id"], sku_id=ids["sku_id"],
        from_warehouse_id=ids["wh_a"], to_warehouse_id=ids["wh_b"],
        qty=4, idempotency_key="tr-1",
    )
    assert (out.on_hand, inn.on_hand) == (6, 9)

    txns = (await db_session.execute(
        select(StockTransaction).where(StockTransaction.ref_id == "tr-1")
    )).scalars().all()
    types = sorted(t.txn_type.value for t in txns)
    assert types == ["transfer_in", "transfer_out"]


async def test_transfer_insufficient_rolls_back_everything(db_session, ids_factory):
    """调出仓不够时，调入仓也不能被加上。"""
    ids = await ids_factory(db_session, on_hand=2)
    svc = InventoryService(db_session)

    with pytest.raises(InsufficientStock):
        await svc.transfer(
            tenant_id=ids["tenant_id"], sku_id=ids["sku_id"],
            from_warehouse_id=ids["wh_a"], to_warehouse_id=ids["wh_b"],
            qty=5, idempotency_key="tr-fail",
        )

    a = await svc.get_stock(ids["tenant_id"], ids["sku_id"], ids["wh_a"])
    b = await svc.get_stock(ids["tenant_id"], ids["sku_id"], ids["wh_b"])
    assert (a.on_hand, b.on_hand) == (2, 5)
    assert await _txn_count(db_session, ids["tenant_id"]) == 0


async def test_transfer_second_leg_failure_undoes_first(db_session, ids_factory):
    """调出仓够扣、调入仓不存在 —— 第一步必须被回滚掉。

    这是真正检验原子性的场景：失败发生在第二个 UPDATE 之后，
    如果只靠"不 commit"而不 rollback，session 会留下脏状态。
    """
    from sqlalchemy import delete

    from app.models.inventory import WarehouseStock

    ids = await ids_factory(db_session, on_hand=10)
    # 删掉 B 仓的库存行，让第二步必然失败
    await db_session.execute(
        delete(WarehouseStock).where(WarehouseStock.warehouse_id == ids["wh_b"])
    )
    await db_session.commit()

    svc = InventoryService(db_session)
    with pytest.raises(StockRowNotFound):
        await svc.transfer(
            tenant_id=ids["tenant_id"], sku_id=ids["sku_id"],
            from_warehouse_id=ids["wh_a"], to_warehouse_id=ids["wh_b"],
            qty=4, idempotency_key="tr-leg2",
        )

    # A 仓必须还是 10，流水一条都不能有
    stock = await svc.get_stock(ids["tenant_id"], ids["sku_id"], ids["wh_a"])
    assert stock.on_hand == 10
    assert await _txn_count(db_session, ids["tenant_id"]) == 0


async def test_transfer_to_same_warehouse_rejected(db_session, ids_factory):
    ids = await ids_factory(db_session)
    svc = InventoryService(db_session)
    with pytest.raises(InventoryError):
        await svc.transfer(
            tenant_id=ids["tenant_id"], sku_id=ids["sku_id"],
            from_warehouse_id=ids["wh_a"], to_warehouse_id=ids["wh_a"],
            qty=1, idempotency_key="tr-same",
        )


# ---------------------------------------------------------------------------
# ⑥ 盘点
# ---------------------------------------------------------------------------
async def test_adjust_up_and_down(db_session, ids_factory):
    ids = await ids_factory(db_session, on_hand=10)
    svc = InventoryService(db_session)

    down = await svc.adjust(
        tenant_id=ids["tenant_id"], sku_id=ids["sku_id"], warehouse_id=ids["wh_a"],
        counted_on_hand=7, idempotency_key="adj-1", remark="盘亏 3",
    )
    assert down.on_hand == 7

    up = await svc.adjust(
        tenant_id=ids["tenant_id"], sku_id=ids["sku_id"], warehouse_id=ids["wh_a"],
        counted_on_hand=9, idempotency_key="adj-2", remark="盘盈 2",
    )
    assert up.on_hand == 9

    txns = (await db_session.execute(
        select(StockTransaction).where(StockTransaction.txn_type == StockTxnType.ADJUST)
    )).scalars().all()
    assert sorted(t.qty_delta for t in txns) == [-3, 2]


async def test_adjust_without_difference_still_records(db_session, ids_factory):
    """盘点无差异也要留痕，否则无法证明"盘过"。"""
    ids = await ids_factory(db_session, on_hand=10)
    svc = InventoryService(db_session)

    r = await svc.adjust(
        tenant_id=ids["tenant_id"], sku_id=ids["sku_id"], warehouse_id=ids["wh_a"],
        counted_on_hand=10, idempotency_key="adj-3",
    )
    assert r.on_hand == 10
    txn = (await db_session.execute(
        select(StockTransaction).where(StockTransaction.idempotency_key == "adj-3")
    )).scalar_one()
    assert txn.qty_delta == 0


# ---------------------------------------------------------------------------
# 租户隔离 & 并发
# ---------------------------------------------------------------------------
async def test_cross_tenant_access_is_not_found(db_session, ids_factory):
    ids = await ids_factory(db_session)
    svc = InventoryService(db_session)

    with pytest.raises(StockRowNotFound):
        await svc.get_stock(999, ids["sku_id"], ids["wh_a"])
    with pytest.raises(StockRowNotFound):
        await svc.outbound(
            tenant_id=999, sku_id=ids["sku_id"], warehouse_id=ids["wh_a"],
            qty=1, idempotency_key="cross",
        )


async def test_concurrent_outbound_no_oversell(session_factory, db_session, ids_factory):
    """10 个并发各出库 1 件，库存 5 —— 必须恰好 5 个成功。"""
    ids = await ids_factory(db_session, on_hand=5)

    async def one(i: int) -> bool:
        async with session_factory() as session:
            svc = InventoryService(session)
            try:
                await svc.outbound(
                    tenant_id=ids["tenant_id"], sku_id=ids["sku_id"],
                    warehouse_id=ids["wh_a"], qty=1, idempotency_key=f"cc-{i}",
                )
                return True
            except InsufficientStock:
                return False

    results = await asyncio.gather(*[one(i) for i in range(10)])
    assert sum(results) == 5

    svc = InventoryService(db_session)
    stock = await svc.get_stock(ids["tenant_id"], ids["sku_id"], ids["wh_a"])
    assert stock.on_hand == 0
    assert await _txn_count(db_session, ids["tenant_id"]) == 5


async def test_concurrent_reserve_no_oversell(session_factory, db_session, ids_factory):
    """并发预留同样不能超：库存 3，8 个请求各要 1。"""
    ids = await ids_factory(db_session, on_hand=3)

    async def one(i: int) -> bool:
        async with session_factory() as session:
            svc = InventoryService(session)
            try:
                await svc.reserve(
                    tenant_id=ids["tenant_id"], sku_id=ids["sku_id"],
                    warehouse_id=ids["wh_a"], qty=1,
                    idempotency_key=f"rr-{i}", ref_type="sales_order", ref_id=f"SO-{i}",
                )
                return True
            except InsufficientStock:
                return False

    results = await asyncio.gather(*[one(i) for i in range(8)])
    assert sum(results) == 3

    stock = await InventoryService(db_session).get_stock(
        ids["tenant_id"], ids["sku_id"], ids["wh_a"]
    )
    assert stock.reserved == 3
    assert stock.available == 0


# ---------------------------------------------------------------------------
# 流水完整性
# ---------------------------------------------------------------------------
async def test_every_change_has_ledger_snapshot(db_session, ids_factory):
    """每条流水的 on_hand_after / reserved_after 必须与当时的库存一致。"""
    ids = await ids_factory(db_session, on_hand=10)
    svc = InventoryService(db_session)

    await svc.inbound(tenant_id=ids["tenant_id"], sku_id=ids["sku_id"],
                      warehouse_id=ids["wh_a"], qty=5, idempotency_key="s-1")
    await svc.reserve(tenant_id=ids["tenant_id"], sku_id=ids["sku_id"],
                      warehouse_id=ids["wh_a"], qty=2, idempotency_key="s-2",
                      ref_type="sales_order", ref_id="SO-9")
    await svc.outbound(tenant_id=ids["tenant_id"], sku_id=ids["sku_id"],
                       warehouse_id=ids["wh_a"], qty=3, idempotency_key="s-3")

    txns = (await db_session.execute(
        select(StockTransaction).order_by(StockTransaction.id)
    )).scalars().all()

    # 最后一次操作的快照应与当前库存一致
    assert txns[-1].on_hand_after == 12
    assert txns[-1].reserved_after == 2

    # 每条流水的 delta 与其类型方向一致
    for t in txns:
        if t.txn_type == StockTxnType.INBOUND:
            assert t.qty_delta > 0
        elif t.txn_type == StockTxnType.OUTBOUND:
            assert t.qty_delta < 0
