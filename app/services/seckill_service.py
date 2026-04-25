import json
import asyncio
import logging
from datetime import datetime
from typing import Optional
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.utils.limiter import token_bucket
from app.models.seckill_product import SeckillProduct
from app.utils.cache import safe_get, safe_delete, safe_delete_pattern

logger = logging.getLogger(__name__)

# 缓存key前缀
SECKILL_CACHE_PREFIX = "seckill"
# 单个秒杀活动缓存TTL（秒），带随机偏移防雪崩
SECKILL_CACHE_TTL = 120  # 2分钟
# 秒杀活动列表缓存TTL
SECKILL_LIST_CACHE_TTL = 60  # 1分钟


class SeckillService:
    def __init__(self, redis_client: Redis, db: AsyncSession):
        self.redis = redis_client
        self.db = db

    async def _get_seckill_by_id(self, seckill_product_id: int) -> Optional[SeckillProduct]:
        """
        获取秒杀活动信息 — 带缓存穿透/击穿/雪崩三重防护
        
        秒杀活动是热点数据，大量并发查询时需要缓存保护。
        缓存穿透防护：不存在的秒杀ID缓存空值，防止恶意遍历
        缓存击穿防护：互斥锁防止热点秒杀key失效时突发DB压力
        缓存雪崩防护：TTL加随机偏移
        """
        cache_key = f"{SECKILL_CACHE_PREFIX}:{seckill_product_id}"
        
        async def _load_from_db() -> Optional[SeckillProduct]:
            result = await self.db.execute(
                select(SeckillProduct).where(SeckillProduct.id == seckill_product_id)
            )
            return result.scalar_one_or_none()
        
        return await safe_get(
            cache_key=cache_key,
            fetch_func=_load_from_db,
            ttl=SECKILL_CACHE_TTL,
            use_mutex=True,  # 秒杀活动是强热点，必须开启互斥锁
        )

    async def _invalidate_seckill_cache(self, seckill_product_id: int) -> None:
        """删除指定秒杀活动的缓存"""
        await safe_delete(f"{SECKILL_CACHE_PREFIX}:{seckill_product_id}")

    async def _invalidate_list_cache(self) -> None:
        """删除秒杀活动列表缓存"""
        await safe_delete_pattern(f"{SECKILL_CACHE_PREFIX}:list:*")

    async def pre_seckill(self, user_id: str, seckill_product_id: int):
        """
        秒杀核心逻辑 — 带三重缓存防护
        
        执行流程：
        1. 从缓存获取秒杀活动信息（防穿透/击穿/雪崩）
        2. 校验活动时间
        3. 用户频率限制（令牌桶）
        4. Redis 预减库存（快速拦截已售罄）
        5. 数据库原子扣减 + 创建订单
        """
        # 0. 从缓存获取秒杀活动信息（三重防护：穿透+击穿+雪崩）
        seckill = await self._get_seckill_by_id(seckill_product_id)
        if not seckill:
            return {"success": False, "code": "NOT_FOUND", "msg": "秒杀活动不存在"}

        # 校验秒杀活动时间
        now = datetime.utcnow()
        if now < seckill.start_time:
            return {"success": False, "code": "NOT_STARTED", "msg": "秒杀活动未开始"}
        if now > seckill.end_time:
            return {"success": False, "code": "EXPIRED", "msg": "秒杀活动已结束"}

        # 1. 用户频率限制
        limit_key = f"{settings.SECKILL_LIMIT_PREFIX}{user_id}:{seckill_product_id}"
        allowed = await token_bucket(self.redis, limit_key, rate=1, capacity=settings.RATE_LIMIT_PER_USER)
        if not allowed:
            return {"success": False, "code": "TOO_MANY_REQUESTS", "msg": "操作太频繁，请稍后重试"}

        # 2. Redis 预减库存（快速拦截已售罄的情况，减轻数据库压力）
        stock_key = f"{settings.SECKILL_STOCK_PREFIX}{seckill_product_id}"
        new_stock = await self.redis.decr(stock_key)
        if new_stock < 0:
            await self.redis.incr(stock_key)
            return {"success": False, "code": "OUT_OF_STOCK", "msg": "库存不足"}

        # 3. 同步创建订单（原子扣减库存 + 创建订单，读写合一）
        from app.services.order_service import OrderService
        order_service = OrderService(self.db)
        order = await order_service.create_order_from_seckill(user_id, seckill_product_id)

        if not order:
            # 数据库库存扣减失败（说明实际已卖完），回滚 Redis 预减
            await self.redis.incr(stock_key)
            return {"success": False, "code": "OUT_OF_STOCK", "msg": "库存不足"}

        return {
            "success": True,
            "msg": "下单成功",
            "order_id": str(order.id),
        }

    # 消费者保留，但只做通知/日志，不再创建订单
    @staticmethod
    async def consume_orders():
        """后台消费者：处理订单通知等，不再重复创建订单"""
        from app.db.redis_client import get_redis
        redis = await get_redis()

        try:
            await redis.xgroup_create(
                settings.SECKILL_QUEUE_NAME,
                "seckill_consumers",
                id="0",
                mkstream=True
            )
        except Exception:
            pass

        while True:
            try:
                result = await redis.xreadgroup(
                    "seckill_consumers", "worker_1", {settings.SECKILL_QUEUE_NAME: '>'}, count=10, block=0
                )
                if result:
                    for stream_name, messages in result:
                        for message_id, fields in messages:
                            # 只确认消息，不再创建订单
                            await redis.xack(settings.SECKILL_QUEUE_NAME, "seckill_consumers", message_id)
            except Exception as e:
                import logging
                logging.error(f"Consumer error: {e}")
                await asyncio.sleep(1)
