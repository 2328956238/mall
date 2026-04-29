from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.dependencies import get_current_admin
from app.db.session import get_db
from app.models.order import Order, OrderStatus
from app.models.user import User
from app.schemas.order import OrderResponse, OrderUpdate
from app.schemas.common import APIResponse
from typing import List
import uuid

router = APIRouter(prefix="/orders", tags=["admin-orders"])


@router.get("/", response_model=APIResponse[List[OrderResponse]])
async def admin_list_orders(
    skip: int = 0,
    limit: int = 100,
    status: str | None = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理员：获取订单列表，可按状态筛选"""
    query = select(Order)
    if status:
        query = query.where(Order.status == status)
    query = query.offset(skip).limit(limit).order_by(Order.created_at.desc())

    result = await db.execute(query)
    orders = result.scalars().all()
    return APIResponse(data=[OrderResponse.model_validate(o) for o in orders])


@router.get("/{order_id}", response_model=APIResponse[OrderResponse])
async def admin_get_order(
    order_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理员：获取订单详情"""
    try:
        order_uuid = uuid.UUID(order_id)
    except ValueError:
        raise HTTPException(400, "无效的订单ID格式")

    order = await db.get(Order, order_uuid)
    if not order:
        raise HTTPException(404, "订单不存在")

    return APIResponse(data=OrderResponse.model_validate(order))


@router.put("/{order_id}/status", response_model=APIResponse[OrderResponse])
async def admin_update_order_status(
    order_id: str,
    update: OrderUpdate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理员：更新订单状态（发货、取消等）"""
    try:
        order_uuid = uuid.UUID(order_id)
    except ValueError:
        raise HTTPException(400, "无效的订单ID格式")

    order = await db.get(Order, order_uuid)
    if not order:
        raise HTTPException(404, "订单不存在")

    # 验证状态值合法
    valid_statuses = [s.value for s in OrderStatus]
    if update.status and update.status not in valid_statuses:
        raise HTTPException(400, f"无效的状态值，可选值: {', '.join(valid_statuses)}")

    if update.status:
        order.status = update.status
    if update.payment_id:
        order.payment_id = update.payment_id

    await db.commit()
    # refresh 后手动赋值已修改的字段，避免 async refresh 报错
    return APIResponse(data=OrderResponse.model_validate(order), message="订单状态已更新")


@router.get("/stats/summary", response_model=APIResponse)
async def admin_order_stats(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理员：获取订单统计概览"""
    # 各状态订单数量
    result = await db.execute(select(Order.status))
    all_statuses = result.scalars().all()

    stats = {
        "total": len(all_statuses),
        "pending": all_statuses.count(OrderStatus.PENDING.value),
        "paid": all_statuses.count(OrderStatus.PAID.value),
        "shipped": all_statuses.count(OrderStatus.SHIPPED.value),
        "completed": all_statuses.count(OrderStatus.COMPLETED.value),
        "cancelled": all_statuses.count(OrderStatus.CANCELLED.value),
    }

    return APIResponse(data=stats)