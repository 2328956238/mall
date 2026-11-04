from sqlalchemy import Integer, Numeric, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from decimal import Decimal
from typing import Optional
from .base import Base, TimestampMixin
from datetime import datetime


class SeckillProduct(Base, TimestampMixin):
    __tablename__ = "seckill_products"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, default=1, index=True
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    # 库存域绑定（第 5 步）：绑定后库存真相在 warehouse_stocks，
    # 这里的 stock_count 只是展示用的活动配额计数。
    sku_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("skus.id", ondelete="SET NULL", name="fk_seckill_products_sku_id"),
        nullable=True, index=True
    )
    warehouse_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("warehouses.id", ondelete="SET NULL",
                   name="fk_seckill_products_warehouse_id"), nullable=True
    )
    # 金额 Decimal
    seckill_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    stock_count: Mapped[int] = mapped_column(Integer, default=0)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
