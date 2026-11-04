from sqlalchemy import String, Integer, Float, Boolean, Numeric, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from decimal import Decimal

from .base import Base, TimestampMixin


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 第 6 步：所有业务数据挂租户。第一阶段固定为默认租户，
    # 第四阶段改成从请求上下文解析。
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, default=1, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=True)
    # 金额统一 Decimal(18,4)，不再用 Float
    price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    # 只读汇总字段：= 该商品所有 SKU 的 on_hand 合计，由 InventoryService 维护。
    # 不能直接改（API 层返回 409）。
    stock: Mapped[int] = mapped_column(Integer, default=0)
    is_on_sale: Mapped[bool] = mapped_column(default=True)
