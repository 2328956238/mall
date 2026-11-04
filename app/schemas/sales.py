"""销售相关 Schema。"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class CustomerCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=255)
    contact: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    country: str = Field("CN", max_length=2)
    address: Optional[str] = None


class CustomerResponse(BaseModel):
    id: int
    code: str
    name: str
    contact: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    country: str
    address: Optional[str] = None
    is_active: bool

    class Config:
        from_attributes = True


class SalesItemCreate(BaseModel):
    sku_id: int
    warehouse_id: int
    qty: int = Field(..., gt=0)
    unit_price: Decimal = Field(..., ge=0)


class SalesOrderCreate(BaseModel):
    customer_id: int
    items: list[SalesItemCreate] = Field(..., min_length=1)
    currency: str = Field("CNY", max_length=3)
    shipping_address: Optional[str] = None
    remark: Optional[str] = None


class SalesItemResponse(BaseModel):
    id: int
    sku_id: int
    warehouse_id: int
    qty: int
    unit_price: Decimal
    reserved_qty: int
    shipped_qty: int

    class Config:
        from_attributes = True


class SalesOrderResponse(BaseModel):
    id: int
    order_no: str
    customer_id: int
    status: str
    total_amount: Decimal
    currency: str
    created_at: datetime

    class Config:
        from_attributes = True


class SalesOrderDetail(BaseModel):
    id: int
    order_no: str
    customer_id: int
    status: str
    total_amount: Decimal
    currency: str
    shipping_address: Optional[str] = None
    created_at: datetime
    items: list[SalesItemResponse] = []


class ShipLineIn(BaseModel):
    sales_order_item_id: int
    qty: int = Field(..., gt=0)


class ConfirmShipmentRequest(BaseModel):
    lines: list[ShipLineIn] = []
    carrier: Optional[str] = None
    tracking_no: Optional[str] = None


class ShipmentResult(BaseModel):
    applied: bool
    shipment_id: int
    order_status: str
    shipped_qty: int


class TrackingCreate(BaseModel):
    carrier: str
    tracking_no: str
    status: str = "in_transit"
    description: Optional[str] = None
    occurred_at: Optional[datetime] = None


class TrackingResponse(BaseModel):
    id: int
    shipment_id: int
    carrier: str
    tracking_no: str
    status: str
    description: Optional[str] = None
    occurred_at: datetime

    class Config:
        from_attributes = True


class UnifiedOrder(BaseModel):
    """`/orders/all` 的条目：把秒杀订单与销售订单合成一种形状。"""

    order_type: str          # seckill | sales
    order_id: str
    status: str
    total_amount: Decimal
    created_at: datetime
