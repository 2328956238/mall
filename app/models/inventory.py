"""库存域模型（第一阶段核心）。

六张表的关系：

    tenants ──< skus >── products            （SKU 挂在商品下，一个商品多个 SKU）
        │           │
        │           └──< warehouse_stocks >── warehouses   （同一 SKU 可在多个仓库）
        │                       │
        │                       ├──< stock_transactions    （只追加的流水）
        │                       └──< stock_reservations    （可过期释放的预留）
        │
        └── 所有表都带 tenant_id，查询必须带租户条件

三个关键约定：

1. **库存三态**：`on_hand`（实际）/ `reserved`（锁定）/ `available = on_hand - reserved`。
   `available` 不落库，用 hybrid_property 计算 —— 落库就会出现第三份真相。

2. **金额用 Decimal**。新表一律 `Numeric(18, 4)`，不再用 Float。
   注意 `products.price` 等老字段仍是 Float，第 6 步才迁移。

3. **枚举存 value 而不是 name**（`values_callable`）。
   老代码 `Enum(OrderStatus)` 存的是 `PENDING` 这种大写 name，容易和业务语义对不上；
   新表统一存 `inbound` 这种小写 value。
"""

import enum
import uuid
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
    BigInteger,
    Numeric,
    String,
    UniqueConstraint,
    JSON,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
class SkuStatus(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class StockTxnType(str, enum.Enum):
    """库存流水类型。六种库存操作各自对应确定的流水类型。"""

    INBOUND = "inbound"            # 入库
    OUTBOUND = "outbound"          # 出库
    RESERVE = "reserve"            # 预留（占用可用量）
    RELEASE = "release"            # 释放预留
    TRANSFER_IN = "transfer_in"    # 调拨入
    TRANSFER_OUT = "transfer_out"  # 调拨出
    ADJUST = "adjust"              # 盘点调整


class ReservationStatus(str, enum.Enum):
    ACTIVE = "active"          # 预留中
    RELEASED = "released"      # 已释放
    COMMITTED = "committed"    # 已转出库（预留变实际扣减）
    EXPIRED = "expired"        # 超时自动释放


def _enum(py_enum, name: str) -> Enum:
    """统一按 value 存库，避免出现 PENDING / pending 混用。"""
    return Enum(py_enum, name=name, values_callable=lambda e: [m.value for m in e])


# ---------------------------------------------------------------------------
# 1. SKU（商品与 SKU 分离）
# ---------------------------------------------------------------------------
class SKU(Base, TimestampMixin):
    __tablename__ = "skus"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sku_code: Mapped[str] = mapped_column(String(64), nullable=False)
    # 规格快照，例如 {"color": "black", "storage": "128G"}
    spec: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    sale_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    cost_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 4), nullable=True)
    status: Mapped[SkuStatus] = mapped_column(
        _enum(SkuStatus, "sku_status"), nullable=False, default=SkuStatus.ACTIVE
    )

    stocks: Mapped[list["WarehouseStock"]] = relationship(back_populates="sku")

    __table_args__ = (
        UniqueConstraint("tenant_id", "sku_code", name="uq_skus_tenant_code"),
        CheckConstraint("sale_price >= 0", name="ck_skus_sale_price_nonneg"),
        CheckConstraint("cost_price IS NULL OR cost_price >= 0", name="ck_skus_cost_price_nonneg"),
        Index("ix_skus_tenant_product", "tenant_id", "product_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SKU {self.sku_code}>"


# ---------------------------------------------------------------------------
# 2. 仓库
# ---------------------------------------------------------------------------
class Warehouse(Base, TimestampMixin):
    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="CN")
    address: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)

    stocks: Mapped[list["WarehouseStock"]] = relationship(back_populates="warehouse")

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_warehouses_tenant_code"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Warehouse {self.code}>"


# ---------------------------------------------------------------------------
# 3. 仓库库存（唯一真相）
# ---------------------------------------------------------------------------
class WarehouseStock(Base, TimestampMixin):
    __tablename__ = "warehouse_stocks"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sku_id: Mapped[int] = mapped_column(
        ForeignKey("skus.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 乐观锁版本号（业务上主要靠 UPDATE ... WHERE 条件做并发控制，这里留作审计与冲突检测）
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    sku: Mapped["SKU"] = relationship(back_populates="stocks")
    warehouse: Mapped["Warehouse"] = relationship(back_populates="stocks")

    @hybrid_property
    def available(self) -> int:
        """可用库存 = 实际 - 锁定。不落库，避免第三份真相。"""
        return self.on_hand - self.reserved

    __table_args__ = (
        UniqueConstraint("sku_id", "warehouse_id", name="uq_stock_sku_warehouse"),
        CheckConstraint("on_hand >= 0", name="ck_stock_on_hand_nonneg"),
        CheckConstraint("reserved >= 0", name="ck_stock_reserved_nonneg"),
        CheckConstraint("reserved <= on_hand", name="ck_stock_reserved_lte_on_hand"),
        Index("ix_stock_tenant_sku", "tenant_id", "sku_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<WarehouseStock sku={self.sku_id} wh={self.warehouse_id} on_hand={self.on_hand} reserved={self.reserved}>"


# ---------------------------------------------------------------------------
# 4. 库存流水（只追加，永不修改）
# ---------------------------------------------------------------------------
class StockTransaction(Base, TimestampMixin):
    __tablename__ = "stock_transactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sku_id: Mapped[int] = mapped_column(
        ForeignKey("skus.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    txn_type: Mapped[StockTxnType] = mapped_column(_enum(StockTxnType, "stock_txn_type"), nullable=False)
    # 变化量：入库/调拨入为正，出库/调拨出为负；预留/释放改的是 reserved，这里记 0 或对应量
    qty_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 变更后的快照，用于对账与审计（不回放流水也能知道结果）
    on_hand_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_after: Mapped[int] = mapped_column(Integer, nullable=False)
    # 来源单据
    ref_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ref_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # 幂等键：同一租户下唯一
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    operator_id: Mapped[Optional[uuid.UUID]] = mapped_column(nullable=True)
    remark: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_stock_txn_idempotency"),
        CheckConstraint("on_hand_after >= 0", name="ck_txn_on_hand_after_nonneg"),
        CheckConstraint("reserved_after >= 0", name="ck_txn_reserved_after_nonneg"),
        Index("ix_txn_tenant_sku_time", "tenant_id", "sku_id", "created_at"),
        Index("ix_txn_ref", "ref_type", "ref_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<StockTransaction {self.txn_type} delta={self.qty_delta}>"


# ---------------------------------------------------------------------------
# 5. 库存预留
# ---------------------------------------------------------------------------
class StockReservation(Base, TimestampMixin):
    __tablename__ = "stock_reservations"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sku_id: Mapped[int] = mapped_column(
        ForeignKey("skus.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ReservationStatus] = mapped_column(
        _enum(ReservationStatus, "reservation_status"),
        nullable=False,
        default=ReservationStatus.ACTIVE,
    )
    ref_type: Mapped[str] = mapped_column(String(32), nullable=False)
    ref_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_reservation_idempotency"),
        CheckConstraint("qty > 0", name="ck_reservation_qty_positive"),
        Index("ix_reservation_tenant_sku_status", "tenant_id", "sku_id", "status"),
        Index("ix_reservation_ref", "ref_type", "ref_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<StockReservation sku={self.sku_id} qty={self.qty} {self.status}>"
