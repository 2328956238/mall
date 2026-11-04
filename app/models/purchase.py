"""采购域模型（第二阶段）。

流程：

    创建采购单(draft) → 提交审核(submitted) → 审核通过(approved)
        → 仓库收货(receiving) → 全部收完(completed)

    任何未收货前可 cancelled。

五张表：

    suppliers
        └─< purchase_orders >─ purchase_order_items ─┐
                    │                                 │
                    └─< purchase_receipts >─ purchase_receipt_items

设计约定：

1. **金额 Decimal(18,4)**，与库存域一致。
2. **枚举存 value**（draft / submitted …），不用大写 name。
3. **状态流转只走 `PurchaseService`**，不允许直接改 `status` 字段。
4. **收货必须幂等**：`purchase_receipts.idempotency_key` 租户内唯一。
5. **收货数量不能超过未收数量**，由 Service 校验 + 数据库 CHECK 兜底。
"""

import enum
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
class POStatus(str, enum.Enum):
    DRAFT = "draft"            # 草稿
    SUBMITTED = "submitted"    # 已提交，等审核
    APPROVED = "approved"      # 审核通过，等收货
    RECEIVING = "receiving"    # 部分收货
    COMPLETED = "completed"    # 全部收货完成
    REJECTED = "rejected"      # 审核驳回
    CANCELLED = "cancelled"    # 取消


class ReceiptStatus(str, enum.Enum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"


def _enum(py_enum, name: str) -> Enum:
    return Enum(py_enum, name=name, values_callable=lambda e: [m.value for m in e])


# ---------------------------------------------------------------------------
# 供应商
# ---------------------------------------------------------------------------
class Supplier(Base, TimestampMixin):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, default=1, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="CN")
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_suppliers_tenant_code"),
    )


# ---------------------------------------------------------------------------
# 采购单
# ---------------------------------------------------------------------------
class PurchaseOrder(Base, TimestampMixin):
    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, default=1, index=True
    )
    order_no: Mapped[str] = mapped_column(String(64), nullable=False)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[POStatus] = mapped_column(
        _enum(POStatus, "po_status"), nullable=False, default=POStatus.DRAFT
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    remark: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    approved_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    items: Mapped[list["PurchaseOrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "order_no", name="uq_po_tenant_no"),
        CheckConstraint("total_amount >= 0", name="ck_po_total_nonneg"),
        Index("ix_po_tenant_status", "tenant_id", "status"),
    )


class PurchaseOrderItem(Base, TimestampMixin):
    __tablename__ = "purchase_order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, default=1, index=True
    )
    po_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sku_id: Mapped[int] = mapped_column(
        ForeignKey("skus.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    received_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    order: Mapped["PurchaseOrder"] = relationship(back_populates="items")

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_po_item_qty_positive"),
        CheckConstraint("unit_price >= 0", name="ck_po_item_price_nonneg"),
        CheckConstraint("received_qty >= 0", name="ck_po_item_received_nonneg"),
        CheckConstraint("received_qty <= qty", name="ck_po_item_received_lte_qty"),
        UniqueConstraint("po_id", "sku_id", "warehouse_id", name="uq_po_item_sku_wh"),
    )


# ---------------------------------------------------------------------------
# 收货单
# ---------------------------------------------------------------------------
class PurchaseReceipt(Base, TimestampMixin):
    __tablename__ = "purchase_receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, default=1, index=True
    )
    receipt_no: Mapped[str] = mapped_column(String(64), nullable=False)
    po_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[ReceiptStatus] = mapped_column(
        _enum(ReceiptStatus, "receipt_status"), nullable=False, default=ReceiptStatus.DRAFT
    )
    # 收货幂等键：同一张单重复提交只入库一次
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    received_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    operator_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    remark: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    items: Mapped[list["PurchaseReceiptItem"]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "receipt_no", name="uq_receipt_tenant_no"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_receipt_idempotency"),
    )


class PurchaseReceiptItem(Base, TimestampMixin):
    __tablename__ = "purchase_receipt_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, default=1, index=True
    )
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_receipts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    po_item_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_order_items.id", ondelete="RESTRICT"), nullable=False
    )
    sku_id: Mapped[int] = mapped_column(
        ForeignKey("skus.id", ondelete="RESTRICT"), nullable=False
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False)

    receipt: Mapped["PurchaseReceipt"] = relationship(back_populates="items")

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_receipt_item_qty_positive"),
    )
