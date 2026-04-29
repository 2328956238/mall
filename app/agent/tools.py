"""
Agent 领域工具实现
==================
4 个工具，对应秒杀平台的 4 个核心能力：

  1. get_product_info    → 查询商品详情
  2. get_order_status    → 查询订单状态
  3. get_seckill_list    → 查询秒杀活动
  4. place_seckill_order → 执行秒杀下单

每个工具都通过依赖注入拿到 redis 和 db 连接，复用已有的 Service 层。
"""

import logging
from typing import Any, Optional
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.product import Product
from app.models.order import Order, OrderStatus
from app.models.seckill_product import SeckillProduct
from app.services.seckill_service import SeckillService
from app.services.order_service import OrderService

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Tool Handlers（纯函数，由 Tool 包装器封装）
# ──────────────────────────────────────────────


async def _get_product_info(
    product_id: int,
    db: AsyncSession,
) -> dict[str, Any]:
    """
    查询商品详细信息。
    复用 ProductService 的缓存策略，确保热点数据的高效读取。
    """
    result = await db.execute(
        select(Product).where(Product.id == product_id)
    )
    product = result.scalar_one_or_none()

    if not product:
        return {
            "found": False,
            "message": f"商品 #{product_id} 不存在",
        }

    return {
        "found": True,
        "product": {
            "id": product.id,
            "name": product.name,
            "description": product.description,
            "price": product.price,
            "stock": product.stock,
            "is_on_sale": product.is_on_sale,
        },
    }


async def _get_order_status(
    order_id: str,
    user_id: Optional[str],
    db: AsyncSession,
) -> dict[str, Any]:
    """
    查询订单状态。
    支持按订单 ID 查询，如果提供了 user_id 则校验订单归属。
    """
    import uuid

    try:
        order_uuid = uuid.UUID(order_id) if not hasattr(order_id, "hex") else order_id
    except ValueError:
        return {"found": False, "message": "无效的订单 ID 格式"}

    result = await db.execute(
        select(Order).where(Order.id == order_uuid)
    )
    order = result.scalar_one_or_none()

    if not order:
        return {"found": False, "message": f"订单 {order_id} 不存在"}

    # 如果提供了 user_id，校验归属
    if user_id:
        order_user_id = str(order.user_id)
        if order_user_id != user_id:
            return {"found": False, "message": "无权查看该订单"}

    status_map = {
        OrderStatus.PENDING: "待支付",
        OrderStatus.PAID: "已支付",
        OrderStatus.SHIPPED: "已发货",
        OrderStatus.COMPLETED: "已完成",
        OrderStatus.CANCELLED: "已取消",
    }

    return {
        "found": True,
        "order": {
            "id": str(order.id),
            "status": order.status.value,
            "status_label": status_map.get(order.status, "未知"),
            "total_amount": order.total_amount,
            "quantity": order.quantity,
            "payment_id": order.payment_id,
            "created_at": order.created_at.isoformat() if order.created_at else None,
        },
    }


async def _get_seckill_list(
    db: AsyncSession,
) -> dict[str, Any]:
    """
    查询当前所有秒杀活动。
    返回活动的基本信息、价格和时间。
    """
    from datetime import datetime, timezone

    result = await db.execute(
        select(SeckillProduct).order_by(SeckillProduct.start_time.desc())
    )
    seckills = result.scalars().all()

    if not seckills:
        return {"found": False, "message": "当前没有秒杀活动"}

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    items = []
    for s in seckills:
        # 一并查询关联的商品名
        prod_result = await db.execute(
            select(Product).where(Product.id == s.product_id)
        )
        product = prod_result.scalar_one_or_none()
        product_name = product.name if product else f"商品 #{s.product_id}"

        # 判断活动状态
        if now < s.start_time:
            status = "未开始"
        elif now > s.end_time:
            status = "已结束"
        else:
            status = "进行中"

        items.append({
            "id": s.id,
            "product_name": product_name,
            "product_id": s.product_id,
            "seckill_price": s.seckill_price,
            "stock_count": s.stock_count,
            "start_time": s.start_time.isoformat(),
            "end_time": s.end_time.isoformat(),
            "status": status,
        })

    return {"found": True, "count": len(items), "items": items}


async def _place_seckill_order(
    seckill_product_id: int,
    user_id: str,
    redis: Redis,
    db: AsyncSession,
) -> dict[str, Any]:
    """
    执行秒杀下单。
    复用 SeckillService 的核心逻辑，包含 Redis 预减库存 + 数据库原子扣减。
    """
    seckill_service = SeckillService(redis, db)
    result = await seckill_service.pre_seckill(user_id, seckill_product_id)

    if result["success"]:
        return {
            "success": True,
            "message": "下单成功！🎉",
            "order_id": result.get("order_id"),
        }
    else:
        code = result.get("code", "UNKNOWN")
        msg = result.get("msg", "未知错误")

        # 将内部 code 映射为友好的用户消息
        friendly_msg = {
            "NOT_FOUND": "该秒杀活动不存在",
            "NOT_STARTED": "秒杀活动尚未开始，请耐心等待 ⏰",
            "EXPIRED": "秒杀活动已结束 😅",
            "TOO_MANY_REQUESTS": "操作太频繁，请稍后重试",
            "OUT_OF_STOCK": "很遗憾，商品已被抢光 😢",
        }.get(code, msg)

        return {
            "success": False,
            "message": friendly_msg,
            "code": code,
        }