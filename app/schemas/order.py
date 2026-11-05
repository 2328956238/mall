from decimal import Decimal

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
import uuid

class OrderStatus(str):
    PENDING = "pending"
    PAID = "paid"
    SHIPPED = "shipped"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

class OrderBase(BaseModel):
    user_id: uuid.UUID
    seckill_product_id: Optional[int] = None
    quantity: int = Field(1, ge=1)
    total_amount: Decimal = Field(..., gt=0)

class OrderCreate(BaseModel):
    user_id: str
    seckill_product_id: Optional[int] = None

class OrderUpdate(BaseModel):
    status: Optional[str] = None
    payment_id: Optional[str] = None

class OrderResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    seckill_product_id: Optional[int] = None
    quantity: int
    total_amount: Decimal
    status: str
    payment_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True