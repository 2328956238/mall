from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.models.order import Order, OrderStatus
from app.models.user import User
from app.dependencies import get_current_user
from app.schemas.common import APIResponse
from pydantic import BaseModel
from typing import Optional
import uuid

router = APIRouter(prefix="/pay", tags=["payment"])


class PaymentRequest(BaseModel):
    payment_method: Optional[str] = "alipay"  # wechat 或 alipay


@router.post("/{order_id}")
async def pay_order(
    order_id: str,
    pay_req: PaymentRequest = PaymentRequest(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """支付订单：支持微信支付和支付宝两种方式（模拟）"""
    try:
        order_uuid = uuid.UUID(order_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的订单ID格式")

    order = await db.get(Order, order_uuid)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    # 只能支付自己的订单
    if str(order.user_id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="无权支付该订单")

    if order.status != OrderStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"订单状态不是待支付，当前状态: {order.status.value}")

    # 校验支付方式
    method = pay_req.payment_method or "alipay"
    if method not in ("wechat", "alipay"):
        raise HTTPException(status_code=400, detail="不支持的支付方式，仅支持 wechat 或 alipay")

    method_labels = {"wechat": "微信支付", "alipay": "支付宝"}
    method_label = method_labels.get(method, "支付宝")

    # 模拟支付处理（实际项目中接入支付宝/微信支付 SDK）
    order.status = OrderStatus.PAID
    order.payment_id = str(uuid.uuid4())
    db.add(order)
    await db.flush()
    await db.commit()

    return APIResponse(
        data={
            "order_id": str(order.id),
            "status": order.status.value,
            "payment_id": order.payment_id,
            "total_amount": order.total_amount,
            "payment_method": method,
            "payment_method_label": method_label,
        },
        message=f"{method_label}支付成功"
    )