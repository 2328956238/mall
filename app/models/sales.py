"""销售与履约域模型（第三阶段）。

为什么单独开 `sales_orders` 而不复用 `orders`：

老 `orders` 表是秒杀专用，`status` 用的是 sealed 的 `OrderStatus` 枚举，
要往里加 `approved` / `picking` 就得 `ALTER TYPE ADD VALUE`（PG 限制多、回滚麻烦）。
而需求明确要求"保留秒杀业务的特殊处理逻辑"，所以：

- 秒杀订单：原样留在 `orders`，一行不改
- 销售订单：新建 `sales_orders` 一族表，状态机独立
- 对用户展示：`/api/v1/orders/all` 把两边按时间合并，列表看起来是一份

流程：

    客户 → 销售订单(pending) → 审核(approved，此时预留库存)
         → 生成发货单(draft) → 确认发货(shipped，核销预留=出库)
         → 物流轨迹 → 完成(completed)

    未发货前可取消，取消时释放预留。
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
class SalesOrderStatus(str, enum.Enum):
    PENDING = "pending"        # 待审核
    APPROVED = "approved"      # 已审核，库存已预留
    PICKING = "picking"        # 已生成发货单，待出库
    SHIPPED = "shipped"        # 已出库
    COMPLETED = "completed"    # 已签收
    CANCELLED = "cancelled"    # 已取消


class ShipmentStatus(str, enum.Enum):
    DRAFT = "draft"            # 待出库
    SHIPPED = "shipped"        # 已出库
    DELIVERED = "delivered"    # 已签收


class TrackingStatus(str, enum.Enum):
    COLLECTED = "collected"            # 已揽收
    IN_TRANSIT = "in_transit"          # 运输中
    OUT_FOR_DELIVERY = "out_for_delivery"  # 派送中
    DELIVERED = "delivered"            # 已签收
    EXCEPTION = "exception"            # 异常


def _enum(py_enum, name: str) -> Enum:
    return Enum(py_enum, name=name, values_callable=lambda e: [m.value for m in e])


# ---------------------------------------------------------------------------
# 客户
# ---------------------------------------------------------------------------
class Customer(Base, TimestampMixin):
    __tablename__ = "customers"

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
    address: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_customers_tenant_code"),
    )


# ---------------------------------------------------------------------------
# 销售订单
# ---------------------------------------------------------------------------
class SalesOrder(Base, TimestampMixin):
    __tablename__ = "sales_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, default=1, index=True
    )
    order_no: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # 下单用户（可选：后台代客下单时为空）
    user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[SalesOrderStatus] = mapped_column(
        _enum(SalesOrderStatus, "sales_order_status"),
        nullable=False, default=SalesOrderStatus.PENDING,
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    shipping_address: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    remark: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    approved_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    shipped_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    items: Mapped[list["SalesOrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "order_no", name="uq_sales_order_no"),
        CheckConstraint("total_amount >= 0", name="ck_sales_total_nonneg"),
        Index("ix_sales_tenant_status", "tenant_id", "status"),
    )


class SalesOrderItem(Base, TimestampMixin):
    __tablename__ = "sales_order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, default=1, index=True
    )
    sales_order_id: Mapped[int] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sku_id: Mapped[int] = mapped_column(
        ForeignKey("skus.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    # 已预留（审核时写入）、已发货（出库时写入）
    reserved_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shipped_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    order: Mapped["SalesOrder"] = relationship(back_populates="items")

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_sales_item_qty_positive"),
        CheckConstraint("unit_price >= 0", name="ck_sales_item_price_nonneg"),
        CheckConstraint("shipped_qty <= qty", name="ck_sales_item_shipped_lte_qty"),
        UniqueConstraint("sales_order_id", "sku_id", "warehouse_id", name="uq_sales_item"),
    )


# ---------------------------------------------------------------------------
# 发货单
# ---------------------------------------------------------------------------
class Shipment(Base, TimestampMixin):
    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, default=1, index=True
    )
    shipment_no: Mapped[str] = mapped_column(String(64), nullable=False)
    sales_order_id: Mapped[int] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[ShipmentStatus] = mapped_column(
        _enum(ShipmentStatus, "shipment_status"), nullable=False, default=ShipmentStatus.DRAFT
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    carrier: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tracking_no: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    shipped_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    operator_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    remark: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    items: Mapped[list["ShipmentItem"]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "shipment_no", name="uq_shipment_no"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_shipment_idempotency"),
    )


class ShipmentItem(Base, TimestampMixin):
    __tablename__ = "shipment_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, default=1, index=True
    )
    shipment_id: Mapped[int] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sales_order_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="RESTRICT"), nullable=False
    )
    sku_id: Mapped[int] = mapped_column(
        ForeignKey("skus.id", ondelete="RESTRICT"), nullable=False
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False)

    shipment: Mapped["Shipment"] = relationship(back_populates="items")

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_shipment_item_qty_positive"),
    )


# ---------------------------------------------------------------------------
# 物流轨迹
# ---------------------------------------------------------------------------
class LogisticsTracking(Base, TimestampMixin):
    __tablename__ = "logistics_tracking"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, default=1, index=True
    )
    shipment_id: Mapped[int] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    carrier: Mapped[str] = mapped_column(String(64), nullable=False)
    tracking_no: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[TrackingStatus] = mapped_column(
        _enum(TrackingStatus, "tracking_status"), nullable=False
    )
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_tracking_shipment_time", "shipment_id", "occurred_at"),
    )
