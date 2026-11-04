"""销售服务：销售订单状态机 + 预留 + 出库 + 物流。

库存动作全部委托 `InventoryService`：

    审核通过 → reserve（按明细预留，幂等键 so:{id}:reserve:{item_id}）
    取消     → release（释放上面的预留）
    确认发货 → commit_reservation（预留转出库，真正扣减 + 写 outbound 流水）

状态机：

    pending → approved | cancelled
    approved → picking | cancelled
    picking → shipped | picking
    shipped → completed
    completed / cancelled 为终态
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import SKU, Warehouse
from app.models.sales import (
    Customer,
    LogisticsTracking,
    SalesOrder,
    SalesOrderItem,
    SalesOrderStatus,
    Shipment,
    ShipmentItem,
    ShipmentStatus,
    TrackingStatus,
)
from app.services.inventory_service import InventoryService


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class SalesError(Exception):
    code = "SALES_ERROR"
    http_status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class SalesNotFound(SalesError):
    code = "SALES_NOT_FOUND"
    http_status = 404


class SalesTransitionError(SalesError):
    code = "INVALID_TRANSITION"
    http_status = 409


class ShipmentQuantityError(SalesError):
    code = "SHIPMENT_QUANTITY_INVALID"
    http_status = 409


ALLOWED: dict[SalesOrderStatus, set[SalesOrderStatus]] = {
    SalesOrderStatus.PENDING: {SalesOrderStatus.APPROVED, SalesOrderStatus.CANCELLED},
    SalesOrderStatus.APPROVED: {SalesOrderStatus.PICKING, SalesOrderStatus.CANCELLED},
    SalesOrderStatus.PICKING: {SalesOrderStatus.SHIPPED, SalesOrderStatus.PICKING},
    SalesOrderStatus.SHIPPED: {SalesOrderStatus.COMPLETED},
    SalesOrderStatus.COMPLETED: set(),
    SalesOrderStatus.CANCELLED: set(),
}


@dataclass
class ShipLine:
    sales_order_item_id: int
    qty: int


class SalesService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.inventory = InventoryService(db)

    # -----------------------------------------------------------------------
    # 客户
    # -----------------------------------------------------------------------
    async def create_customer(
        self, *, tenant_id: int, code: str, name: str,
        contact: Optional[str] = None, email: Optional[str] = None,
        phone: Optional[str] = None, country: str = "CN",
        address: Optional[str] = None,
    ) -> Customer:
        exists = (
            await self.db.execute(
                select(Customer).where(
                    Customer.tenant_id == tenant_id, Customer.code == code
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            raise SalesError(f"客户编码已存在：{code}")

        customer = Customer(
            tenant_id=tenant_id, code=code, name=name, contact=contact,
            email=email, phone=phone, country=country, address=address,
        )
        self.db.add(customer)
        await self.db.commit()
        return customer

    # -----------------------------------------------------------------------
    # 销售订单
    # -----------------------------------------------------------------------
    async def create_order(
        self, *,
        tenant_id: int,
        customer_id: int,
        items: Iterable[dict],
        user_id: Optional[str] = None,
        created_by: Optional[str] = None,
        currency: str = "CNY",
        shipping_address: Optional[str] = None,
        remark: Optional[str] = None,
    ) -> SalesOrder:
        items = list(items)
        if not items:
            raise SalesError("销售订单至少要有一条明细")

        customer = (
            await self.db.execute(
                select(Customer).where(
                    Customer.id == customer_id, Customer.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if customer is None:
            raise SalesNotFound(f"客户不存在：{customer_id}")

        order = SalesOrder(
            tenant_id=tenant_id,
            order_no=f"SO{datetime.utcnow():%Y%m%d%H%M%S}{uuid.uuid4().hex[:4].upper()}",
            customer_id=customer_id,
            user_id=user_id,
            status=SalesOrderStatus.PENDING,
            currency=currency,
            shipping_address=shipping_address or customer.address,
            remark=remark,
            created_by=created_by,
            total_amount=Decimal("0"),
        )
        self.db.add(order)
        await self.db.flush()

        total = Decimal("0")
        seen = set()
        for it in items:
            sku_id, wh_id = int(it["sku_id"]), int(it["warehouse_id"])
            qty = int(it["qty"])
            price = Decimal(str(it["unit_price"]))
            if qty <= 0:
                raise SalesError("明细数量必须大于 0")
            if price < 0:
                raise SalesError("明细单价不能为负")
            if (sku_id, wh_id) in seen:
                raise SalesError("同一 SKU + 仓库不能重复，请合并数量")
            seen.add((sku_id, wh_id))

            sku = (
                await self.db.execute(
                    select(SKU).where(SKU.id == sku_id, SKU.tenant_id == tenant_id)
                )
            ).scalar_one_or_none()
            if sku is None:
                raise SalesNotFound(f"SKU 不存在：{sku_id}")
            wh = (
                await self.db.execute(
                    select(Warehouse).where(
                        Warehouse.id == wh_id, Warehouse.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if wh is None:
                raise SalesNotFound(f"仓库不存在：{wh_id}")

            self.db.add(
                SalesOrderItem(
                    tenant_id=tenant_id, sales_order_id=order.id, sku_id=sku_id,
                    warehouse_id=wh_id, qty=qty, unit_price=price,
                    reserved_qty=0, shipped_qty=0,
                )
            )
            total += price * qty

        order.total_amount = total.quantize(Decimal("0.0001"))
        await self.db.commit()
        await self.db.refresh(order)
        return order

    async def get_order(self, tenant_id: int, order_id: int) -> SalesOrder:
        order = (
            await self.db.execute(
                select(SalesOrder).where(
                    SalesOrder.id == order_id, SalesOrder.tenant_id == tenant_id
                ).execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if order is None:
            raise SalesNotFound(f"销售订单不存在：{order_id}")
        return order

    async def _items(self, order_id: int) -> list[SalesOrderItem]:
        return (
            await self.db.execute(
                select(SalesOrderItem).where(SalesOrderItem.sales_order_id == order_id)
                .execution_options(populate_existing=True)
            )
        ).scalars().all()

    def _transition(self, order: SalesOrder, target: SalesOrderStatus) -> None:
        if target not in ALLOWED[order.status]:
            raise SalesTransitionError(
                f"销售订单不能从 {order.status.value} 变为 {target.value}"
            )
        order.status = target

    async def approve(self, tenant_id: int, order_id: int, approved_by: str) -> SalesOrder:
        """审核通过：**此时才预留库存**，避免未审核的订单把货占死。"""
        order = await self.get_order(tenant_id, order_id)
        if order.status != SalesOrderStatus.PENDING:
            raise SalesTransitionError(
                f"只有待审核订单能审核，当前 {order.status.value}"
            )

        items = await self._items(order.id)
        reserved_ids = []
        for item in items:
            result = await self.inventory.reserve(
                tenant_id=tenant_id, sku_id=item.sku_id,
                warehouse_id=item.warehouse_id, qty=item.qty,
                idempotency_key=f"so:{order.id}:reserve:{item.id}",
                ref_type="sales_order", ref_id=str(order.id),
            )
            item.reserved_qty = item.qty
            reserved_ids.append(result.reservation_id)

        self._transition(order, SalesOrderStatus.APPROVED)
        order.approved_by = approved_by
        order.approved_at = datetime.utcnow()
        await self.db.commit()
        return order

    async def cancel(self, tenant_id: int, order_id: int) -> SalesOrder:
        """取消：把已预留的库存放回去。"""
        order = await self.get_order(tenant_id, order_id)
        if order.status not in (SalesOrderStatus.PENDING, SalesOrderStatus.APPROVED):
            raise SalesTransitionError(f"当前状态 {order.status.value} 不能取消")

        if order.status == SalesOrderStatus.APPROVED:
            for item in await self._items(order.id):
                if item.reserved_qty <= 0:
                    continue
                # 找到预留单再释放
                from app.models.inventory import StockReservation

                reservation_id = (
                    await self.db.execute(
                        select(StockReservation.id).where(
                            StockReservation.tenant_id == tenant_id,
                            StockReservation.idempotency_key
                            == f"so:{order.id}:reserve:{item.id}",
                        )
                    )
                ).scalar_one_or_none()
                if reservation_id is not None:
                    await self.inventory.release(
                        tenant_id=tenant_id, reservation_id=reservation_id,
                        idempotency_key=f"so:{order.id}:release:{item.id}",
                    )
                item.reserved_qty = 0

        self._transition(order, SalesOrderStatus.CANCELLED)
        await self.db.commit()
        return order

    # -----------------------------------------------------------------------
    # 发货
    # -----------------------------------------------------------------------
    async def create_shipment(
        self, tenant_id: int, order_id: int, *, operator_id: Optional[str] = None
    ) -> Shipment:
        order = await self.get_order(tenant_id, order_id)
        if order.status not in (SalesOrderStatus.APPROVED, SalesOrderStatus.PICKING):
            raise SalesTransitionError(
                f"当前状态 {order.status.value} 不能发货，需先审核通过"
            )

        items = await self._items(order.id)
        remaining = [i for i in items if i.qty - i.shipped_qty > 0]
        if not remaining:
            raise SalesError("订单已全部发货")

        shipment = Shipment(
            tenant_id=tenant_id,
            shipment_no=f"SH{datetime.utcnow():%Y%m%d%H%M%S}{uuid.uuid4().hex[:4].upper()}",
            sales_order_id=order.id,
            warehouse_id=remaining[0].warehouse_id,
            status=ShipmentStatus.DRAFT,
            idempotency_key=f"shipment-task:{order.id}:{uuid.uuid4().hex}",
            operator_id=operator_id,
        )
        self.db.add(shipment)
        await self.db.flush()

        for item in remaining:
            self.db.add(
                ShipmentItem(
                    tenant_id=tenant_id, shipment_id=shipment.id,
                    sales_order_item_id=item.id, sku_id=item.sku_id,
                    qty=item.qty - item.shipped_qty,
                )
            )

        if order.status == SalesOrderStatus.APPROVED:
            self._transition(order, SalesOrderStatus.PICKING)

        await self.db.commit()
        await self.db.refresh(shipment)
        return shipment

    async def confirm_shipment(
        self, tenant_id: int, shipment_id: int, *,
        lines: Optional[list[ShipLine]] = None,
        carrier: Optional[str] = None, tracking_no: Optional[str] = None,
    ) -> dict:
        """确认发货：预留转出库（真正扣库存 + outbound 流水）。"""
        shipment = (
            await self.db.execute(
                select(Shipment).where(
                    Shipment.id == shipment_id, Shipment.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if shipment is None:
            raise SalesNotFound(f"发货单不存在：{shipment_id}")

        if shipment.status != ShipmentStatus.DRAFT:
            order = await self.get_order(tenant_id, shipment.sales_order_id)
            return {"applied": False, "shipment_id": shipment.id,
                    "order_status": order.status.value, "shipped_qty": 0}

        order = await self.get_order(tenant_id, shipment.sales_order_id)
        items = (
            await self.db.execute(
                select(ShipmentItem).where(ShipmentItem.shipment_id == shipment.id)
            )
        ).scalars().all()
        overrides = {l.sales_order_item_id: l.qty for l in (lines or [])}

        total = 0
        for si in items:
            qty = overrides.get(si.sales_order_item_id, si.qty)
            if qty <= 0:
                continue

            order_item = (
                await self.db.execute(
                    select(SalesOrderItem).where(SalesOrderItem.id == si.sales_order_item_id)
                )
            ).scalar_one()

            remaining = order_item.qty - order_item.shipped_qty
            if qty > remaining:
                raise ShipmentQuantityError(
                    f"明细 {order_item.id} 发货数量 {qty} 超过未发数量 {remaining}"
                )

            # 找到预留单并核销
            from app.models.inventory import StockReservation

            reservation_id = (
                await self.db.execute(
                    select(StockReservation.id).where(
                        StockReservation.tenant_id == tenant_id,
                        StockReservation.idempotency_key
                        == f"so:{order.id}:reserve:{order_item.id}",
                    )
                )
            ).scalar_one_or_none()

            if reservation_id is not None:
                await self.inventory.commit_reservation(
                    tenant_id=tenant_id, reservation_id=reservation_id,
                    idempotency_key=f"so:{order.id}:ship:{order_item.id}",
                    remark=f"销售出库 SO#{order.id}",
                )
            else:
                # 没有预留（例如人工改过数据）就走直接出库，仍然会写流水
                await self.inventory.outbound(
                    tenant_id=tenant_id, sku_id=order_item.sku_id,
                    warehouse_id=order_item.warehouse_id, qty=qty,
                    idempotency_key=f"so:{order.id}:outbound:{order_item.id}",
                    ref_type="sales_order", ref_id=str(order.id),
                )

            order_item.shipped_qty += qty
            order_item.reserved_qty = max(0, order_item.reserved_qty - qty)
            si.qty = qty
            total += qty

        shipment.status = ShipmentStatus.SHIPPED
        shipment.shipped_at = datetime.utcnow()
        shipment.carrier = carrier or shipment.carrier
        shipment.tracking_no = tracking_no or shipment.tracking_no

        all_items = await self._items(order.id)
        fully = all(i.shipped_qty >= i.qty for i in all_items)
        if fully:
            self._transition(order, SalesOrderStatus.SHIPPED)
            order.shipped_at = datetime.utcnow()
        elif order.status != SalesOrderStatus.PICKING:
            self._transition(order, SalesOrderStatus.PICKING)

        await self.db.commit()
        return {"applied": True, "shipment_id": shipment.id,
                "order_status": order.status.value, "shipped_qty": total}

    # -----------------------------------------------------------------------
    # 物流
    # -----------------------------------------------------------------------
    async def add_tracking(
        self, *, tenant_id: int, shipment_id: int,
        carrier: str, tracking_no: str, status: TrackingStatus,
        description: Optional[str] = None, occurred_at: Optional[datetime] = None,
    ) -> LogisticsTracking:
        shipment = (
            await self.db.execute(
                select(Shipment).where(
                    Shipment.id == shipment_id, Shipment.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if shipment is None:
            raise SalesNotFound(f"发货单不存在：{shipment_id}")

        row = LogisticsTracking(
            tenant_id=tenant_id, shipment_id=shipment_id, carrier=carrier,
            tracking_no=tracking_no, status=status, description=description,
            occurred_at=occurred_at or datetime.utcnow(),
        )
        self.db.add(row)

        shipment.carrier = carrier
        shipment.tracking_no = tracking_no

        if status == TrackingStatus.DELIVERED:
            shipment.status = ShipmentStatus.DELIVERED
            order = await self.get_order(tenant_id, shipment.sales_order_id)
            if order.status == SalesOrderStatus.SHIPPED:
                self._transition(order, SalesOrderStatus.COMPLETED)
                order.completed_at = datetime.utcnow()

        await self.db.commit()
        return row

    async def complete(self, tenant_id: int, order_id: int) -> SalesOrder:
        order = await self.get_order(tenant_id, order_id)
        self._transition(order, SalesOrderStatus.COMPLETED)
        order.completed_at = datetime.utcnow()
        await self.db.commit()
        return order
