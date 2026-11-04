"""采购服务：状态机 + 收货入库。

职责边界：
- **只管采购单的状态流转与收货单据**，库存增减一律委托 `InventoryService`
  （复用第 3 步的入库、幂等、流水能力，不另起一套）。
- 状态流转只允许走本文件的 `_transition`，任何非法跳转直接抛错。

合法流转：

    draft      → submitted | cancelled
    submitted  → approved | rejected | cancelled
    approved   → receiving | cancelled
    receiving  → completed | receiving（继续收剩下的）
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
from app.models.purchase import (
    POStatus,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReceipt,
    PurchaseReceiptItem,
    ReceiptStatus,
    Supplier,
)
from app.services.inventory_service import InventoryService


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class PurchaseError(Exception):
    code = "PURCHASE_ERROR"
    http_status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class PurchaseNotFound(PurchaseError):
    code = "PURCHASE_NOT_FOUND"
    http_status = 404


class InvalidTransition(PurchaseError):
    code = "INVALID_TRANSITION"
    http_status = 409


class ReceiptQuantityError(PurchaseError):
    code = "RECEIPT_QUANTITY_INVALID"
    http_status = 409


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------
ALLOWED: dict[POStatus, set[POStatus]] = {
    POStatus.DRAFT: {POStatus.SUBMITTED, POStatus.CANCELLED},
    POStatus.SUBMITTED: {POStatus.APPROVED, POStatus.REJECTED, POStatus.CANCELLED},
    POStatus.APPROVED: {POStatus.RECEIVING, POStatus.CANCELLED},
    POStatus.RECEIVING: {POStatus.RECEIVING, POStatus.COMPLETED},
    POStatus.COMPLETED: set(),
    POStatus.REJECTED: set(),
    POStatus.CANCELLED: set(),
}


@dataclass
class ReceiptLine:
    po_item_id: int
    qty: int


class PurchaseService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.inventory = InventoryService(db)

    # -----------------------------------------------------------------------
    # 供应商
    # -----------------------------------------------------------------------
    async def create_supplier(
        self, *, tenant_id: int, code: str, name: str,
        contact: Optional[str] = None, email: Optional[str] = None,
        phone: Optional[str] = None, country: str = "CN",
    ) -> Supplier:
        exists = (
            await self.db.execute(
                select(Supplier).where(
                    Supplier.tenant_id == tenant_id, Supplier.code == code
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            raise PurchaseError(f"供应商编码已存在：{code}")

        supplier = Supplier(
            tenant_id=tenant_id, code=code, name=name, contact=contact,
            email=email, phone=phone, country=country,
        )
        self.db.add(supplier)
        await self.db.commit()
        return supplier

    # -----------------------------------------------------------------------
    # 采购单
    # -----------------------------------------------------------------------
    async def create_po(
        self, *,
        tenant_id: int,
        supplier_id: int,
        items: Iterable[dict],
        created_by: Optional[str] = None,
        remark: Optional[str] = None,
        currency: str = "CNY",
    ) -> PurchaseOrder:
        """创建采购单（草稿）。

        items: [{sku_id, warehouse_id, qty, unit_price}]
        """
        items = list(items)
        if not items:
            raise PurchaseError("采购单至少要有一条明细")

        supplier = (
            await self.db.execute(
                select(Supplier).where(
                    Supplier.id == supplier_id, Supplier.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if supplier is None:
            raise PurchaseNotFound(f"供应商不存在：{supplier_id}")

        total = Decimal("0")
        po = PurchaseOrder(
            tenant_id=tenant_id,
            order_no=f"PO{datetime.utcnow():%Y%m%d%H%M%S}{uuid.uuid4().hex[:4].upper()}",
            supplier_id=supplier_id,
            status=POStatus.DRAFT,
            currency=currency,
            remark=remark,
            created_by=created_by,
            total_amount=Decimal("0"),
        )
        self.db.add(po)
        await self.db.flush()

        seen = set()
        for it in items:
            qty = int(it["qty"])
            price = Decimal(str(it["unit_price"]))
            if qty <= 0:
                raise PurchaseError("明细数量必须大于 0")
            if price < 0:
                raise PurchaseError("明细单价不能为负")

            key = (int(it["sku_id"]), int(it["warehouse_id"]))
            if key in seen:
                raise PurchaseError("同一 SKU + 仓库不能重复出现，请合并数量")
            seen.add(key)

            # SKU / 仓库必须属于该租户
            sku = (
                await self.db.execute(
                    select(SKU).where(SKU.id == key[0], SKU.tenant_id == tenant_id)
                )
            ).scalar_one_or_none()
            if sku is None:
                raise PurchaseNotFound(f"SKU 不存在：{key[0]}")
            wh = (
                await self.db.execute(
                    select(Warehouse).where(
                        Warehouse.id == key[1], Warehouse.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if wh is None:
                raise PurchaseNotFound(f"仓库不存在：{key[1]}")

            self.db.add(
                PurchaseOrderItem(
                    tenant_id=tenant_id, po_id=po.id, sku_id=key[0],
                    warehouse_id=key[1], qty=qty, unit_price=price, received_qty=0,
                )
            )
            total += price * qty

        po.total_amount = total.quantize(Decimal("0.0001"))
        await self.db.commit()
        await self.db.refresh(po)
        return po

    async def get_po(self, tenant_id: int, po_id: int) -> PurchaseOrder:
        po = (
            await self.db.execute(
                select(PurchaseOrder).where(
                    PurchaseOrder.id == po_id, PurchaseOrder.tenant_id == tenant_id
                ).execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if po is None:
            raise PurchaseNotFound(f"采购单不存在：{po_id}")
        return po

    async def _transition(self, po: PurchaseOrder, target: POStatus) -> None:
        if target not in ALLOWED[po.status]:
            raise InvalidTransition(
                f"采购单状态不能从 {po.status.value} 变为 {target.value}"
            )
        po.status = target

    async def submit(self, tenant_id: int, po_id: int) -> PurchaseOrder:
        po = await self.get_po(tenant_id, po_id)
        count = (
            await self.db.execute(
                select(PurchaseOrderItem.id).where(PurchaseOrderItem.po_id == po.id)
            )
        ).first()
        if count is None:
            raise PurchaseError("没有明细的采购单不能提交")

        await self._transition(po, POStatus.SUBMITTED)
        po.submitted_at = datetime.utcnow()
        await self.db.commit()
        return po

    async def approve(self, tenant_id: int, po_id: int, approved_by: str) -> PurchaseOrder:
        po = await self.get_po(tenant_id, po_id)
        await self._transition(po, POStatus.APPROVED)
        po.approved_by = approved_by
        po.approved_at = datetime.utcnow()
        await self.db.commit()
        return po

    async def reject(self, tenant_id: int, po_id: int, reason: str = "") -> PurchaseOrder:
        po = await self.get_po(tenant_id, po_id)
        await self._transition(po, POStatus.REJECTED)
        po.remark = f"{po.remark or ''}\n驳回：{reason}".strip()
        await self.db.commit()
        return po

    async def cancel(self, tenant_id: int, po_id: int) -> PurchaseOrder:
        po = await self.get_po(tenant_id, po_id)
        await self._transition(po, POStatus.CANCELLED)
        await self.db.commit()
        return po

    # -----------------------------------------------------------------------
    # 收货：生成入库任务 → 确认收货
    # -----------------------------------------------------------------------
    async def create_receipt_task(
        self, tenant_id: int, po_id: int, *, operator_id: Optional[str] = None
    ) -> PurchaseReceipt:
        """审核通过后生成"入库任务"（草稿收货单），明细默认带未收数量。"""
        po = await self.get_po(tenant_id, po_id)
        if po.status not in (POStatus.APPROVED, POStatus.RECEIVING):
            raise InvalidTransition(
                f"当前状态 {po.status.value} 不能生成入库任务，需先审核通过"
            )

        items = (
            await self.db.execute(
                select(PurchaseOrderItem).where(PurchaseOrderItem.po_id == po.id)
            )
        ).scalars().all()
        remaining = [i for i in items if i.qty - i.received_qty > 0]
        if not remaining:
            raise PurchaseError("采购单已全部收货，无需再生成入库任务")

        receipt = PurchaseReceipt(
            tenant_id=tenant_id,
            receipt_no=f"RC{datetime.utcnow():%Y%m%d%H%M%S}{uuid.uuid4().hex[:4].upper()}",
            po_id=po.id,
            warehouse_id=remaining[0].warehouse_id,
            status=ReceiptStatus.DRAFT,
            idempotency_key=f"receipt-task:{po.id}:{uuid.uuid4().hex}",
            operator_id=operator_id,
        )
        self.db.add(receipt)
        await self.db.flush()

        for it in remaining:
            self.db.add(
                PurchaseReceiptItem(
                    tenant_id=tenant_id, receipt_id=receipt.id, po_item_id=it.id,
                    sku_id=it.sku_id, qty=it.qty - it.received_qty,
                )
            )

        if po.status == POStatus.APPROVED:
            await self._transition(po, POStatus.RECEIVING)

        await self.db.commit()
        await self.db.refresh(receipt)
        return receipt

    async def confirm_receipt(
        self, tenant_id: int, receipt_id: int, *, lines: Optional[list[ReceiptLine]] = None
    ) -> dict:
        """仓库确认收货：按明细入库 + 写库存流水 + 推进采购单状态。

        幂等：已确认的收货单再次调用直接返回，不重复入库
        （即使中途失败重试，库存流水也有 `receipt:{id}:{item}` 这个幂等键兜底）。
        """
        receipt = (
            await self.db.execute(
                select(PurchaseReceipt).where(
                    PurchaseReceipt.id == receipt_id,
                    PurchaseReceipt.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if receipt is None:
            raise PurchaseNotFound(f"收货单不存在：{receipt_id}")

        if receipt.status == ReceiptStatus.CONFIRMED:
            po = await self.get_po(tenant_id, receipt.po_id)
            return {
                "applied": False,
                "receipt_id": receipt.id,
                "po_status": po.status.value,
                "inbound_qty": 0,
            }

        po = await self.get_po(tenant_id, receipt.po_id)
        items = (
            await self.db.execute(
                select(PurchaseReceiptItem).where(
                    PurchaseReceiptItem.receipt_id == receipt.id
                )
            )
        ).scalars().all()

        # 允许本次只收一部分
        overrides = {l.po_item_id: l.qty for l in (lines or [])}

        total_qty = 0
        for ri in items:
            qty = overrides.get(ri.po_item_id, ri.qty)
            if qty <= 0:
                continue

            po_item = (
                await self.db.execute(
                    select(PurchaseOrderItem).where(PurchaseOrderItem.id == ri.po_item_id)
                )
            ).scalar_one()

            remaining = po_item.qty - po_item.received_qty
            if qty > remaining:
                raise ReceiptQuantityError(
                    f"明细 {po_item.id} 收货数量 {qty} 超过未收数量 {remaining}"
                )

            await self.inventory.inbound(
                tenant_id=tenant_id,
                sku_id=po_item.sku_id,
                warehouse_id=po_item.warehouse_id,
                qty=qty,
                idempotency_key=f"receipt:{receipt.id}:item:{po_item.id}",
                ref_type="purchase_receipt",
                ref_id=str(receipt.id),
                remark=f"采购收货 PO#{po.id}",
            )

            po_item.received_qty += qty
            ri.qty = qty
            total_qty += qty

        receipt.status = ReceiptStatus.CONFIRMED
        receipt.received_at = datetime.utcnow()

        # 全部收完 → completed，否则保持 receiving
        all_items = (
            await self.db.execute(
                select(PurchaseOrderItem).where(PurchaseOrderItem.po_id == po.id)
            )
        ).scalars().all()
        fully = all(i.received_qty >= i.qty for i in all_items)
        if fully:
            if po.status != POStatus.COMPLETED:
                await self._transition(po, POStatus.COMPLETED)
        elif po.status != POStatus.RECEIVING:
            await self._transition(po, POStatus.RECEIVING)

        await self.db.commit()
        return {
            "applied": True,
            "receipt_id": receipt.id,
            "po_status": po.status.value,
            "inbound_qty": total_qty,
        }
