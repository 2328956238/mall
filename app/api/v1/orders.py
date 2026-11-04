from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import get_db
from app.services.order_service import OrderService
from app.schemas.order import OrderResponse
from app.schemas.sales import UnifiedOrder
from app.schemas.common import APIResponse
from app.models.user import User
from app.models.order import Order
from app.dependencies import get_current_user
from typing import List

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/", response_model=APIResponse[List[OrderResponse]])
async def get_orders(
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取当前用户订单列表"""
    service = OrderService(db)
    orders = await service.get_user_orders(str(current_user.id), skip=skip, limit=limit)
    return APIResponse(data=orders)


@router.get("/all", response_model=APIResponse[list[UnifiedOrder]])
async def unified_orders(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """秒杀订单 + 销售订单合并视图（第三阶段的"兼容"要求）。

    必须声明在 `/{order_id}` **之前**，否则 "all" 会被当成 UUID 匹配走。
    """
    from sqlalchemy import select as _select

    from app.models.sales import SalesOrder

    seckill_rows = (
        await db.execute(_select(Order).where(Order.user_id == current_user.id))
    ).scalars().all()
    sales_rows = (
        await db.execute(
            _select(SalesOrder).where(
                SalesOrder.tenant_id == current_user.tenant_id,
                SalesOrder.user_id == str(current_user.id),
            )
        )
    ).scalars().all()

    merged = [
        UnifiedOrder(order_type="seckill", order_id=str(o.id), status=o.status.value,
                     total_amount=o.total_amount, created_at=o.created_at)
        for o in seckill_rows
    ] + [
        UnifiedOrder(order_type="sales", order_id=str(o.id), status=o.status.value,
                     total_amount=o.total_amount, created_at=o.created_at)
        for o in sales_rows
    ]
    merged.sort(key=lambda x: x.created_at, reverse=True)
    return APIResponse(data=merged)


@router.get("/{order_id}", response_model=APIResponse[OrderResponse])
async def get_order(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """根据 ID 获取订单详情"""
    service = OrderService(db)
    order = await service.get_order_by_id(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    # 只能查看自己的订单（管理员可以查看所有）
    if str(order.user_id) != str(current_user.id) and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="无权查看该订单")
    return APIResponse(data=order)