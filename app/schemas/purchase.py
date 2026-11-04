"""采购相关 Schema。"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class SupplierCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=255)
    contact: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    country: str = Field("CN", max_length=2)


class SupplierResponse(BaseModel):
    id: int
    code: str
    name: str
    contact: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    country: str
    is_active: bool

    class Config:
        from_attributes = True


class POItemCreate(BaseModel):
    sku_id: int
    warehouse_id: int
    qty: int = Field(..., gt=0)
    unit_price: Decimal = Field(..., ge=0)


class PurchaseOrderCreate(BaseModel):
    supplier_id: int
    items: list[POItemCreate] = Field(..., min_length=1)
    currency: str = Field("CNY", max_length=3)
    remark: Optional[str] = None


class POItemResponse(BaseModel):
    id: int
    sku_id: int
    warehouse_id: int
    qty: int
    unit_price: Decimal
    received_qty: int

    class Config:
        from_attributes = True


class PurchaseOrderResponse(BaseModel):
    id: int
    order_no: str
    supplier_id: int
    status: str
    total_amount: Decimal
    currency: str
    created_at: datetime

    class Config:
        from_attributes = True


class PurchaseOrderDetail(PurchaseOrderResponse):
    items: list[POItemResponse] = []


class ReceiptLineIn(BaseModel):
    po_item_id: int
    qty: int = Field(..., gt=0)


class ConfirmReceiptRequest(BaseModel):
    lines: list[ReceiptLineIn] = []      # 空 = 按入库任务全收


class ReceiptResponse(BaseModel):
    applied: bool
    receipt_id: int
    po_status: str
    inbound_qty: int
