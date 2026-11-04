from sqlalchemy import Integer, Numeric, String, ForeignKey, Enum
from sqlalchemy.orm import Mapped, mapped_column
from decimal import Decimal
from typing import Optional
from .base import Base, TimestampMixin
import enum
import uuid


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    SHIPPED = "shipped"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, default=1, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    # 第 6 步起可空：普通销售订单没有秒杀活动（第三阶段启用）
    seckill_product_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("seckill_products.id"), nullable=True
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.PENDING)
    payment_id: Mapped[str] = mapped_column(String(100), nullable=True)
