from pydantic import BaseModel, Field, field_validator
from typing import Optional
from datetime import datetime

class SeckillProductBase(BaseModel):
    product_id: int = Field(..., description="关联的商品ID")
    seckill_price: float = Field(..., gt=0, description="秒杀价")
    stock_count: int = Field(0, ge=0, description="秒杀库存数")
    start_time: datetime = Field(..., description="秒杀开始时间")
    end_time: datetime = Field(..., description="秒杀结束时间")

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def coerce_datetime(cls, v):
        """将带时区的datetime转为naive UTC, 兼容ISO字符串"""
        if isinstance(v, str):
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if v is not None and v.tzinfo is not None:
            return v.replace(tzinfo=None)
        return v

class SeckillProductCreate(SeckillProductBase):
    pass

class SeckillProductUpdate(BaseModel):
    seckill_price: Optional[float] = Field(None, gt=0)
    stock_count: Optional[int] = Field(None, ge=0)
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def coerce_datetime(cls, v):
        """将带时区的datetime转为naive UTC, 兼容ISO字符串"""
        if v is None:
            return v
        if isinstance(v, str):
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if v.tzinfo is not None:
            return v.replace(tzinfo=None)
        return v

class SeckillProductResponse(SeckillProductBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class SeckillRequest(BaseModel):
    """秒杀请求"""
    seckill_product_id: int = Field(..., description="秒杀产品ID")

class SeckillResponse(BaseModel):
    """秒杀响应"""
    success: bool
    message: str
    code: Optional[str] = None
    order_id: Optional[str] = None