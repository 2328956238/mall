"""库存服务：六种库存操作的唯一入口。

设计要点
--------
1. **判断与写入合一**
   所有扣减都用「单条 UPDATE ... WHERE 条件」，靠 PostgreSQL 行锁保证并发正确，
   不使用 `SELECT ... FOR UPDATE` 这种两步走（那会把判断和写入拆到两个时点）。

2. **流水与库存同事务**
   库存变了但流水没写 = 账对不上。两者必须在同一个事务里提交。

3. **幂等靠唯一键**
   每个操作必须带 `idempotency_key`，`(tenant_id, idempotency_key)` 唯一。
   重复提交时**返回上次的结果**（`applied=False`），而不是报错 —— 调用方重试是正常行为。

4. **可用量 = 实际 - 锁定**
   出库和预留都只能动「可用量」，不允许把别人锁定的货卖掉。

5. **租户隔离**
   每个查询都带 `tenant_id`，跨租户访问一律按"不存在"处理。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import (
    ReservationStatus,
    StockReservation,
    StockTransaction,
    StockTxnType,
    WarehouseStock,
)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class InventoryError(Exception):
    """库存相关业务异常基类。"""

    code = "INVENTORY_ERROR"
    http_status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class StockRowNotFound(InventoryError):
    code = "STOCK_NOT_FOUND"
    http_status = 404


class InsufficientStock(InventoryError):
    code = "INSUFFICIENT_STOCK"
    http_status = 409


class ReservationNotFound(InventoryError):
    code = "RESERVATION_NOT_FOUND"
    http_status = 404


class ReservationStateError(InventoryError):
    code = "RESERVATION_STATE_INVALID"
    http_status = 409


# ---------------------------------------------------------------------------
# 返回结构
# ---------------------------------------------------------------------------
@dataclass
class StockOpResult:
    """库存操作结果。

    `applied=False` 表示这次调用是**幂等重放**（同一个 key 之前已经执行过），
    库存没有被二次修改，返回值是上次操作后的快照。
    """

    applied: bool
    on_hand: int
    reserved: int
    txn_id: Optional[int] = None
    reservation_id: Optional[int] = None

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------
class InventoryService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # -- 查询 ---------------------------------------------------------------
    async def get_stock(
        self, tenant_id: int, sku_id: int, warehouse_id: int
    ) -> WarehouseStock:
        row = (
            await self.db.execute(
                select(WarehouseStock).where(
                    WarehouseStock.tenant_id == tenant_id,
                    WarehouseStock.sku_id == sku_id,
                    WarehouseStock.warehouse_id == warehouse_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise StockRowNotFound(
                f"库存记录不存在：tenant={tenant_id} sku={sku_id} warehouse={warehouse_id}"
            )
        return row

    async def get_available(self, tenant_id: int, sku_id: int, warehouse_id: int) -> int:
        stock = await self.get_stock(tenant_id, sku_id, warehouse_id)
        return stock.available

    async def total_available(self, tenant_id: int, sku_id: int) -> int:
        """某 SKU 在全部仓库的可用量合计。"""
        rows = (
            await self.db.execute(
                select(WarehouseStock).where(
                    WarehouseStock.tenant_id == tenant_id,
                    WarehouseStock.sku_id == sku_id,
                )
            )
        ).scalars().all()
        return sum(r.available for r in rows)

    # -- 幂等 ---------------------------------------------------------------
    async def _find_txn(self, tenant_id: int, key: str) -> Optional[StockTransaction]:
        return (
            await self.db.execute(
                select(StockTransaction).where(
                    StockTransaction.tenant_id == tenant_id,
                    StockTransaction.idempotency_key == key,
                )
            )
        ).scalar_one_or_none()

    # -- 核心：带条件的原子更新 ---------------------------------------------
    async def _apply(
        self,
        *,
        tenant_id: int,
        sku_id: int,
        warehouse_id: int,
        on_hand_delta: int,
        reserved_delta: int,
    ) -> tuple[int, int]:
        """原子修改库存，返回 (on_hand, reserved)。

        扣减类操作把「够不够」的判断放进 WHERE，交给数据库在持锁状态下判定，
        避免并发下"查的时候够、扣的时候不够"。
        """
        stmt = (
            update(WarehouseStock)
            .where(
                WarehouseStock.tenant_id == tenant_id,
                WarehouseStock.sku_id == sku_id,
                WarehouseStock.warehouse_id == warehouse_id,
            )
            .values(
                on_hand=WarehouseStock.on_hand + on_hand_delta,
                reserved=WarehouseStock.reserved + reserved_delta,
                version=WarehouseStock.version + 1,
            )
            .returning(WarehouseStock.on_hand, WarehouseStock.reserved)
        )

        # 出库/调拨出：只能动可用量
        if on_hand_delta < 0:
            need = -on_hand_delta
            if reserved_delta < 0:
                # 核销预留：货早就被自己锁住了，此时可用量可能就是 0，
                # 不能再拿"可用量够不够"来判断，只看实际库存是否足够。
                stmt = stmt.where(WarehouseStock.on_hand >= need)
            else:
                stmt = stmt.where(WarehouseStock.on_hand - WarehouseStock.reserved >= need)
        # 预留：可用量必须够
        if reserved_delta > 0:
            stmt = stmt.where(
                WarehouseStock.on_hand - WarehouseStock.reserved >= reserved_delta
            )
        # 释放/核销：锁定量必须够
        if reserved_delta < 0:
            stmt = stmt.where(WarehouseStock.reserved >= -reserved_delta)

        result = (await self.db.execute(stmt)).first()
        if result is None:
            # 区分「没有这行」和「量不够」，给调用方准确的错误
            exists = (
                await self.db.execute(
                    select(WarehouseStock.id).where(
                        WarehouseStock.tenant_id == tenant_id,
                        WarehouseStock.sku_id == sku_id,
                        WarehouseStock.warehouse_id == warehouse_id,
                    )
                )
            ).first()
            # 走到这里说明本次操作失败：必须回滚，否则调拨这类多步操作
            # 会出现"调出仓已扣、调入仓没加"的半成品状态。
            await self.db.rollback()
            if exists is None:
                raise StockRowNotFound(
                    f"库存记录不存在：tenant={tenant_id} sku={sku_id} warehouse={warehouse_id}"
                )
            raise InsufficientStock(
                f"可用库存不足：sku={sku_id} warehouse={warehouse_id} "
                f"需要 {abs(on_hand_delta) or abs(reserved_delta)}"
            )
        on_hand, reserved = result
        return int(on_hand), int(reserved)

    # -- 写流水 -------------------------------------------------------------
    async def _write_txn(
        self,
        *,
        tenant_id: int,
        sku_id: int,
        warehouse_id: int,
        txn_type: StockTxnType,
        qty_delta: int,
        on_hand_after: int,
        reserved_after: int,
        idempotency_key: str,
        ref_type: Optional[str] = None,
        ref_id: Optional[str] = None,
        operator_id: Optional[uuid.UUID] = None,
        remark: Optional[str] = None,
    ) -> StockTransaction:
        txn = StockTransaction(
            tenant_id=tenant_id,
            sku_id=sku_id,
            warehouse_id=warehouse_id,
            txn_type=txn_type,
            qty_delta=qty_delta,
            on_hand_after=on_hand_after,
            reserved_after=reserved_after,
            idempotency_key=idempotency_key,
            ref_type=ref_type,
            ref_id=ref_id,
            operator_id=operator_id,
            remark=remark,
        )
        self.db.add(txn)
        await self.db.flush()
        return txn

    # -----------------------------------------------------------------------
    # ① 入库
    # -----------------------------------------------------------------------
    async def inbound(
        self,
        *,
        tenant_id: int,
        sku_id: int,
        warehouse_id: int,
        qty: int,
        idempotency_key: str,
        ref_type: Optional[str] = None,
        ref_id: Optional[str] = None,
        operator_id: Optional[uuid.UUID] = None,
        remark: Optional[str] = None,
    ) -> StockOpResult:
        if qty <= 0:
            raise InventoryError("入库数量必须大于 0")

        existing = await self._find_txn(tenant_id, idempotency_key)
        if existing is not None:
            return StockOpResult(
                applied=False,
                on_hand=existing.on_hand_after,
                reserved=existing.reserved_after,
                txn_id=existing.id,
            )

        on_hand, reserved = await self._apply(
            tenant_id=tenant_id, sku_id=sku_id, warehouse_id=warehouse_id,
            on_hand_delta=qty, reserved_delta=0,
        )
        txn = await self._write_txn(
            tenant_id=tenant_id, sku_id=sku_id, warehouse_id=warehouse_id,
            txn_type=StockTxnType.INBOUND, qty_delta=qty,
            on_hand_after=on_hand, reserved_after=reserved,
            idempotency_key=idempotency_key,
            ref_type=ref_type, ref_id=ref_id, operator_id=operator_id, remark=remark,
        )
        await self._sync_for_sku(tenant_id, sku_id)
        await self._commit_idempotent()
        return StockOpResult(applied=True, on_hand=on_hand, reserved=reserved, txn_id=txn.id)

    # -----------------------------------------------------------------------
    # ② 出库
    # -----------------------------------------------------------------------
    async def outbound(
        self,
        *,
        tenant_id: int,
        sku_id: int,
        warehouse_id: int,
        qty: int,
        idempotency_key: str,
        ref_type: Optional[str] = None,
        ref_id: Optional[str] = None,
        operator_id: Optional[uuid.UUID] = None,
        remark: Optional[str] = None,
    ) -> StockOpResult:
        if qty <= 0:
            raise InventoryError("出库数量必须大于 0")

        existing = await self._find_txn(tenant_id, idempotency_key)
        if existing is not None:
            return StockOpResult(
                applied=False,
                on_hand=existing.on_hand_after,
                reserved=existing.reserved_after,
                txn_id=existing.id,
            )

        on_hand, reserved = await self._apply(
            tenant_id=tenant_id, sku_id=sku_id, warehouse_id=warehouse_id,
            on_hand_delta=-qty, reserved_delta=0,
        )
        txn = await self._write_txn(
            tenant_id=tenant_id, sku_id=sku_id, warehouse_id=warehouse_id,
            txn_type=StockTxnType.OUTBOUND, qty_delta=-qty,
            on_hand_after=on_hand, reserved_after=reserved,
            idempotency_key=idempotency_key,
            ref_type=ref_type, ref_id=ref_id, operator_id=operator_id, remark=remark,
        )
        await self._sync_for_sku(tenant_id, sku_id)
        await self._commit_idempotent()
        return StockOpResult(applied=True, on_hand=on_hand, reserved=reserved, txn_id=txn.id)

    # -----------------------------------------------------------------------
    # ③ 预留
    # -----------------------------------------------------------------------
    async def reserve(
        self,
        *,
        tenant_id: int,
        sku_id: int,
        warehouse_id: int,
        qty: int,
        idempotency_key: str,
        ref_type: str,
        ref_id: str,
        expires_at: Optional[datetime] = None,
        operator_id: Optional[uuid.UUID] = None,
    ) -> StockOpResult:
        if qty <= 0:
            raise InventoryError("预留数量必须大于 0")

        existing = await self._find_txn(tenant_id, idempotency_key)
        if existing is not None:
            reservation_id = (
                await self.db.execute(
                    select(StockReservation.id).where(
                        StockReservation.tenant_id == tenant_id,
                        StockReservation.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            return StockOpResult(
                applied=False,
                on_hand=existing.on_hand_after,
                reserved=existing.reserved_after,
                txn_id=existing.id,
                reservation_id=reservation_id,
            )

        on_hand, reserved = await self._apply(
            tenant_id=tenant_id, sku_id=sku_id, warehouse_id=warehouse_id,
            on_hand_delta=0, reserved_delta=qty,
        )
        reservation = StockReservation(
            tenant_id=tenant_id, sku_id=sku_id, warehouse_id=warehouse_id,
            qty=qty, status=ReservationStatus.ACTIVE,
            ref_type=ref_type, ref_id=ref_id,
            idempotency_key=idempotency_key, expires_at=expires_at,
        )
        self.db.add(reservation)
        await self.db.flush()

        txn = await self._write_txn(
            tenant_id=tenant_id, sku_id=sku_id, warehouse_id=warehouse_id,
            txn_type=StockTxnType.RESERVE, qty_delta=0,
            on_hand_after=on_hand, reserved_after=reserved,
            idempotency_key=idempotency_key,
            ref_type=ref_type, ref_id=ref_id, operator_id=operator_id,
        )
        await self._sync_for_sku(tenant_id, sku_id)
        await self._commit_idempotent()
        return StockOpResult(
            applied=True, on_hand=on_hand, reserved=reserved,
            txn_id=txn.id, reservation_id=reservation.id,
        )

    # -----------------------------------------------------------------------
    # ④ 释放预留
    # -----------------------------------------------------------------------
    async def release(
        self,
        *,
        tenant_id: int,
        reservation_id: int,
        idempotency_key: str,
        operator_id: Optional[uuid.UUID] = None,
        remark: Optional[str] = None,
    ) -> StockOpResult:
        existing = await self._find_txn(tenant_id, idempotency_key)
        if existing is not None:
            return StockOpResult(
                applied=False,
                on_hand=existing.on_hand_after,
                reserved=existing.reserved_after,
                txn_id=existing.id,
            )

        res = await self._get_reservation(tenant_id, reservation_id)
        if res.status != ReservationStatus.ACTIVE:
            # 已释放/已核销的预留再释放，视为幂等成功（不报错、不重复减）
            return StockOpResult(
                applied=False,
                on_hand=await self._on_hand_of(res),
                reserved=await self._reserved_of(res),
                reservation_id=res.id,
            )

        on_hand, reserved = await self._apply(
            tenant_id=tenant_id, sku_id=res.sku_id, warehouse_id=res.warehouse_id,
            on_hand_delta=0, reserved_delta=-res.qty,
        )
        res.status = ReservationStatus.RELEASED
        await self._sync_for_sku(tenant_id, res.sku_id)
        txn = await self._write_txn(
            tenant_id=tenant_id, sku_id=res.sku_id, warehouse_id=res.warehouse_id,
            txn_type=StockTxnType.RELEASE, qty_delta=0,
            on_hand_after=on_hand, reserved_after=reserved,
            idempotency_key=idempotency_key,
            ref_type=res.ref_type, ref_id=res.ref_id, operator_id=operator_id, remark=remark,
        )
        await self._commit_idempotent()
        return StockOpResult(
            applied=True, on_hand=on_hand, reserved=reserved,
            txn_id=txn.id, reservation_id=res.id,
        )

    # -----------------------------------------------------------------------
    # ④' 预留核销（真正出库）
    # -----------------------------------------------------------------------
    async def commit_reservation(
        self,
        *,
        tenant_id: int,
        reservation_id: int,
        idempotency_key: str,
        operator_id: Optional[uuid.UUID] = None,
        remark: Optional[str] = None,
    ) -> StockOpResult:
        existing = await self._find_txn(tenant_id, idempotency_key)
        if existing is not None:
            return StockOpResult(
                applied=False,
                on_hand=existing.on_hand_after,
                reserved=existing.reserved_after,
                txn_id=existing.id,
            )

        res = await self._get_reservation(tenant_id, reservation_id)
        if res.status == ReservationStatus.COMMITTED:
            return StockOpResult(
                applied=False,
                on_hand=await self._on_hand_of(res),
                reserved=await self._reserved_of(res),
                reservation_id=res.id,
            )
        if res.status != ReservationStatus.ACTIVE:
            raise ReservationStateError(
                f"预留 {reservation_id} 当前状态 {res.status.value}，不能核销"
            )

        # 预留转出库：实际与锁定同时减少
        on_hand, reserved = await self._apply(
            tenant_id=tenant_id, sku_id=res.sku_id, warehouse_id=res.warehouse_id,
            on_hand_delta=-res.qty, reserved_delta=-res.qty,
        )
        res.status = ReservationStatus.COMMITTED
        await self._sync_for_sku(tenant_id, res.sku_id)
        txn = await self._write_txn(
            tenant_id=tenant_id, sku_id=res.sku_id, warehouse_id=res.warehouse_id,
            txn_type=StockTxnType.OUTBOUND, qty_delta=-res.qty,
            on_hand_after=on_hand, reserved_after=reserved,
            idempotency_key=idempotency_key,
            ref_type=res.ref_type, ref_id=res.ref_id, operator_id=operator_id, remark=remark,
        )
        await self._commit_idempotent()
        return StockOpResult(
            applied=True, on_hand=on_hand, reserved=reserved,
            txn_id=txn.id, reservation_id=res.id,
        )

    # -----------------------------------------------------------------------
    # ⑤ 调拨
    # -----------------------------------------------------------------------
    async def transfer(
        self,
        *,
        tenant_id: int,
        sku_id: int,
        from_warehouse_id: int,
        to_warehouse_id: int,
        qty: int,
        idempotency_key: str,
        operator_id: Optional[uuid.UUID] = None,
        remark: Optional[str] = None,
    ) -> tuple[StockOpResult, StockOpResult]:
        """仓库间调拨。两条库存变更 + 两条流水，同一个事务。"""
        if qty <= 0:
            raise InventoryError("调拨数量必须大于 0")
        if from_warehouse_id == to_warehouse_id:
            raise InventoryError("调出仓与调入仓不能相同")

        existing = await self._find_txn(tenant_id, idempotency_key)
        if existing is not None:
            raise InventoryError(
                f"幂等键 {idempotency_key} 已被使用，调拨不支持重放，请换新键或先查询"
            )

        out_on_hand, out_reserved = await self._apply(
            tenant_id=tenant_id, sku_id=sku_id, warehouse_id=from_warehouse_id,
            on_hand_delta=-qty, reserved_delta=0,
        )
        in_on_hand, in_reserved = await self._apply(
            tenant_id=tenant_id, sku_id=sku_id, warehouse_id=to_warehouse_id,
            on_hand_delta=qty, reserved_delta=0,
        )

        txn_out = await self._write_txn(
            tenant_id=tenant_id, sku_id=sku_id, warehouse_id=from_warehouse_id,
            txn_type=StockTxnType.TRANSFER_OUT, qty_delta=-qty,
            on_hand_after=out_on_hand, reserved_after=out_reserved,
            idempotency_key=f"{idempotency_key}:out",
            ref_type="transfer", ref_id=idempotency_key,
            operator_id=operator_id, remark=remark,
        )
        txn_in = await self._write_txn(
            tenant_id=tenant_id, sku_id=sku_id, warehouse_id=to_warehouse_id,
            txn_type=StockTxnType.TRANSFER_IN, qty_delta=qty,
            on_hand_after=in_on_hand, reserved_after=in_reserved,
            idempotency_key=f"{idempotency_key}:in",
            ref_type="transfer", ref_id=idempotency_key,
            operator_id=operator_id, remark=remark,
        )
        await self._sync_for_sku(tenant_id, sku_id)
        await self.db.commit()
        return (
            StockOpResult(applied=True, on_hand=out_on_hand, reserved=out_reserved, txn_id=txn_out.id),
            StockOpResult(applied=True, on_hand=in_on_hand, reserved=in_reserved, txn_id=txn_in.id),
        )

    # -----------------------------------------------------------------------
    # ⑥ 盘点
    # -----------------------------------------------------------------------
    async def adjust(
        self,
        *,
        tenant_id: int,
        sku_id: int,
        warehouse_id: int,
        counted_on_hand: int,
        idempotency_key: str,
        operator_id: Optional[uuid.UUID] = None,
        remark: Optional[str] = None,
    ) -> StockOpResult:
        """盘点：把实际库存改成实盘数，差额记 adjust 流水。"""
        if counted_on_hand < 0:
            raise InventoryError("实盘数量不能为负")

        existing = await self._find_txn(tenant_id, idempotency_key)
        if existing is not None:
            return StockOpResult(
                applied=False,
                on_hand=existing.on_hand_after,
                reserved=existing.reserved_after,
                txn_id=existing.id,
            )

        stock = await self.get_stock(tenant_id, sku_id, warehouse_id)
        delta = counted_on_hand - stock.on_hand
        if delta == 0:
            # 没差异也要留痕，否则"盘点过但没记录"
            txn = await self._write_txn(
                tenant_id=tenant_id, sku_id=sku_id, warehouse_id=warehouse_id,
                txn_type=StockTxnType.ADJUST, qty_delta=0,
                on_hand_after=stock.on_hand, reserved_after=stock.reserved,
                idempotency_key=idempotency_key,
                ref_type="stocktake", ref_id=idempotency_key,
                operator_id=operator_id, remark=remark or "盘点无差异",
            )
            await self._sync_for_sku(tenant_id, sku_id)
            await self._commit_idempotent()
            return StockOpResult(
                applied=True, on_hand=stock.on_hand, reserved=stock.reserved, txn_id=txn.id
            )

        on_hand, reserved = await self._apply(
            tenant_id=tenant_id, sku_id=sku_id, warehouse_id=warehouse_id,
            on_hand_delta=delta, reserved_delta=0,
        )
        txn = await self._write_txn(
            tenant_id=tenant_id, sku_id=sku_id, warehouse_id=warehouse_id,
            txn_type=StockTxnType.ADJUST, qty_delta=delta,
            on_hand_after=on_hand, reserved_after=reserved,
            idempotency_key=idempotency_key,
            ref_type="stocktake", ref_id=idempotency_key,
            operator_id=operator_id, remark=remark,
        )
        await self._sync_for_sku(tenant_id, sku_id)
        await self._commit_idempotent()
        return StockOpResult(applied=True, on_hand=on_hand, reserved=reserved, txn_id=txn.id)

    # -----------------------------------------------------------------------
    # products.stock 汇总同步（第 4 步）
    # -----------------------------------------------------------------------
    async def sync_product_stock(self, tenant_id: int, product_id: int) -> int:
        """把 `products.stock` 重算为该商品所有 SKU 的 on_hand 合计。

        第一阶段之后 `products.stock` 只是**只读汇总字段**，真正的库存在
        `warehouse_stocks`。任何直接改 `products.stock` 的入口都应被拒绝，
        否则汇总值和明细值会分叉。
        """
        from app.models.inventory import SKU
        from app.models.product import Product

        total = (
            await self.db.execute(
                select(func.coalesce(func.sum(WarehouseStock.on_hand), 0))
                .join(SKU, SKU.id == WarehouseStock.sku_id)
                .where(SKU.product_id == product_id, WarehouseStock.tenant_id == tenant_id)
            )
        ).scalar_one()

        await self.db.execute(
            update(Product).where(Product.id == product_id).values(stock=int(total))
        )
        return int(total)

    async def _sync_for_sku(self, tenant_id: int, sku_id: int) -> None:
        """按 SKU 反查商品并同步汇总值。"""
        from app.models.inventory import SKU

        product_id = (
            await self.db.execute(select(SKU.product_id).where(SKU.id == sku_id))
        ).scalar_one_or_none()
        if product_id is not None:
            await self.sync_product_stock(tenant_id, product_id)

    # -----------------------------------------------------------------------
    # 内部工具
    # -----------------------------------------------------------------------
    async def _get_reservation(self, tenant_id: int, reservation_id: int) -> StockReservation:
        res = (
            await self.db.execute(
                select(StockReservation).where(
                    StockReservation.id == reservation_id,
                    StockReservation.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if res is None:
            raise ReservationNotFound(f"预留不存在：id={reservation_id} tenant={tenant_id}")
        return res

    async def _on_hand_of(self, res: StockReservation) -> int:
        stock = await self.get_stock(res.tenant_id, res.sku_id, res.warehouse_id)
        return stock.on_hand

    async def _reserved_of(self, res: StockReservation) -> int:
        stock = await self.get_stock(res.tenant_id, res.sku_id, res.warehouse_id)
        return stock.reserved

    async def _commit_idempotent(self) -> None:
        """提交事务；唯一键冲突说明并发重复提交，回滚后按"已执行过"返回。"""
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise
