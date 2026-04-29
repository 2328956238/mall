import asyncio
import logging
from datetime import datetime
from sqlalchemy import select, delete
from app.db.session import AsyncSessionLocal
from app.db.redis_client import get_redis
from app.models.seckill_product import SeckillProduct
from app.models.order import Order, OrderStatus
from app.config import settings

logger = logging.getLogger(__name__)


async def release_expired_seckill_stock_async():
    """释放已过期秒杀活动的 Redis 库存"""
    try:
        redis = await get_redis()
        async with AsyncSessionLocal() as db:
            # 查询已结束的秒杀活动
            now = datetime.utcnow()
            result = await db.execute(
                select(SeckillProduct).where(SeckillProduct.end_time < now)
            )
            expired_seckills = result.scalars().all()

            if not expired_seckills:
                logger.debug("没有过期的秒杀活动需要处理")
                return

            for seckill in expired_seckills:
                stock_key = f"{settings.SECKILL_STOCK_PREFIX}{seckill.id}"

                # 获取 Redis 剩余库存
                remaining = await redis.get(stock_key)
                if remaining is not None:
                    remaining = int(remaining)
                    # 如果还有剩余库存，记录日志并同步回数据库
                    if remaining > 0:
                        logger.info(
                            f"秒杀活动 {seckill.id} 已结束，剩余库存 {remaining} 件"
                        )
                        # 将 Redis 剩余库存回写数据库（已售数量 = 初始 - 剩余）
                        seckill.stock_count = remaining
                    else:
                        # 库存已耗尽，数据库标记为 0
                        seckill.stock_count = 0

                    # 清理 Redis 中的秒杀键
                    await redis.delete(stock_key)
                    limit_pattern = f"{settings.SECKILL_LIMIT_PREFIX}*:{seckill.id}"
                    cursor = 0
                    while True:
                        cursor, keys = await redis.scan(
                            cursor=cursor, match=limit_pattern, count=100
                        )
                        if keys:
                            await redis.delete(*keys)
                        if cursor == 0:
                            break
                else:
                    # Redis key 不存在（可能已被清理），保持数据库原值不变
                    logger.debug(f"秒杀活动 {seckill.id} 的 Redis 库存 key 已不存在，跳过")

            await db.commit()
            logger.info(f"已处理 {len(expired_seckills)} 个过期秒杀活动")

    except Exception as e:
        logger.error(f"释放过期秒杀库存失败: {e}")


def release_expired_seckill_stock():
    asyncio.run(release_expired_seckill_stock_async())


async def generate_daily_report_async():
    """生成每日销售报告"""
    try:
        async with AsyncSessionLocal() as db:
            today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

            # 查询今日所有订单
            result = await db.execute(
                select(Order).where(Order.created_at >= today)
            )
            today_orders = result.scalars().all()

            total_orders = len(today_orders)
            total_revenue = sum(o.total_amount for o in today_orders)
            completed_orders = sum(
                1 for o in today_orders if o.status == OrderStatus.COMPLETED.value
            )
            cancelled_orders = sum(
                1 for o in today_orders if o.status == OrderStatus.CANCELLED.value
            )

            report = {
                "date": today.date().isoformat(),
                "total_orders": total_orders,
                "total_revenue": round(total_revenue, 2),
                "completed_orders": completed_orders,
                "cancelled_orders": cancelled_orders,
                "pending_orders": total_orders - completed_orders - cancelled_orders,
            }

            logger.info(f"每日销售报告: {report}")
            print(f"每日销售报告: {report}")
            return report

    except Exception as e:
        logger.error(f"生成每日报告失败: {e}")


def generate_daily_report():
    asyncio.run(generate_daily_report_async())