"""秒杀服务。

第 5 步之后，库存真相在 `warehouse_stocks`：

    Redis DECR            只做高并发前置拦截（不是库存真相）
        ↓
    DB 预留（reserve）    真正的判定：可用量够不够
        ↓
    活动配额 -1 + 建订单   （补偿：失败则释放预留 + 回补 Redis）
        ↓
    核销预留（commit）    实际出库，写入流水

任何一步失败都会走补偿：释放预留 + Redis 回补，保证"没抢到"的请求不会少库存。
`reconcile()` 用来对账：Redis 计数器与数据库可用量的差值应当收敛到 0。
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.inventory import SKU, WarehouseStock
from app.models.seckill_product import SeckillProduct
from app.services.inventory_service import (
    InsufficientStock,
    InventoryService,
    StockRowNotFound,
)
from app.utils.cache import safe_delete
from app.utils.limiter import token_bucket

logger = logging.getLogger(__name__)

# 缓存key前缀
SECKILL_CACHE_PREFIX = "seckill"
SECKILL_CACHE_TTL = 120
SECKILL_LIST_CACHE_TTL = 60


class SeckillService:
    def __init__(self, redis_client: Redis, db: AsyncSession):
        self.redis = redis_client
        self.db = db
        self.inventory = InventoryService(db)

    # -----------------------------------------------------------------------
    # 读取活动
    # -----------------------------------------------------------------------
    async def _get_seckill_by_id(self, seckill_product_id: int) -> Optional[SeckillProduct]:
        """直接查 DB。ORM 对象不适合缓存序列化，秒杀前校验查库性能可接受。"""
        result = await self.db.execute(
            select(SeckillProduct).where(SeckillProduct.id == seckill_product_id)
        )
        return result.scalar_one_or_none()

    async def _invalidate_seckill_cache(self, seckill_product_id: int) -> None:
        await safe_delete(f"{SECKILL_CACHE_PREFIX}:{seckill_product_id}")

    async def _invalidate_list_cache(self) -> None:
        await safe_delete(f"{SECKILL_CACHE_PREFIX}:list:all")

    # -----------------------------------------------------------------------
    # 库存绑定解析
    # -----------------------------------------------------------------------
    async def resolve_binding(
        self, seckill: SeckillProduct
    ) -> Optional[tuple[int, int, int]]:
        """返回 (tenant_id, sku_id, warehouse_id)。

        优先用活动上显式绑定的 SKU/仓库；没有绑定则按商品自动解析到
        "第一个 SKU + 该 SKU 库存最多的仓"。都解析不到返回 None（走老逻辑）。
        """
        if seckill.sku_id and seckill.warehouse_id:
            sku = (
                await self.db.execute(select(SKU).where(SKU.id == seckill.sku_id))
            ).scalar_one_or_none()
            if sku is not None:
                return sku.tenant_id, sku.id, seckill.warehouse_id

        row = (
            await self.db.execute(
                select(SKU, WarehouseStock)
                .join(WarehouseStock, WarehouseStock.sku_id == SKU.id)
                .where(SKU.product_id == seckill.product_id)
                .order_by(WarehouseStock.on_hand.desc(), WarehouseStock.id.asc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        sku, stock = row
        return sku.tenant_id, sku.id, stock.warehouse_id

    # -----------------------------------------------------------------------
    # 主入口
    # -----------------------------------------------------------------------
    async def pre_seckill(self, user_id: str, seckill_product_id: int):
        seckill = await self._get_seckill_by_id(seckill_product_id)
        if not seckill:
            return {"success": False, "code": "NOT_FOUND", "msg": "秒杀活动不存在"}

        now = datetime.utcnow()
        if now < seckill.start_time:
            return {"success": False, "code": "NOT_STARTED", "msg": "秒杀活动未开始"}
        if now > seckill.end_time:
            return {"success": False, "code": "EXPIRED", "msg": "秒杀活动已结束"}

        limit_key = f"{settings.SECKILL_LIMIT_PREFIX}{user_id}:{seckill_product_id}"
        allowed = await token_bucket(
            self.redis, limit_key, rate=1, capacity=settings.RATE_LIMIT_PER_USER
        )
        if not allowed:
            return {"success": False, "code": "TOO_MANY_REQUESTS", "msg": "操作太频繁，请稍后重试"}

        stock_key = f"{settings.SECKILL_STOCK_PREFIX}{seckill_product_id}"
        await self._ensure_redis_counter(stock_key, seckill.stock_count)

        if not await self._predecr(stock_key):
            return {"success": False, "code": "OUT_OF_STOCK", "msg": "库存不足"}

        binding = await self.resolve_binding(seckill)
        if binding is None:
            # 兼容期：还没建库存记录的活动走老逻辑（第 6 步回填后不再出现）
            return await self._legacy_order(user_id, seckill, stock_key)

        tenant_id, sku_id, warehouse_id = binding
        return await self._inventory_order(
            user_id=user_id, seckill=seckill, stock_key=stock_key,
            tenant_id=tenant_id, sku_id=sku_id, warehouse_id=warehouse_id,
        )

    # -----------------------------------------------------------------------
    # Redis 前置拦截
    # -----------------------------------------------------------------------
    async def _ensure_redis_counter(self, stock_key: str, stock_count: int) -> None:
        """Redis key 丢失（重启/被清理）时从活动配额初始化，用 SETNX 防并发覆盖。"""
        existing = await self.redis.get(stock_key)
        if existing is None:
            await self.redis.setnx(stock_key, stock_count)

    async def _predecr(self, stock_key: str) -> bool:
        new_stock = await self.redis.decr(stock_key)
        if new_stock < 0:
            await self.redis.incr(stock_key)
            return False
        return True

    async def _rollback_redis(self, stock_key: str) -> None:
        await self.redis.incr(stock_key)

    # -----------------------------------------------------------------------
    # 库存域下单
    # -----------------------------------------------------------------------
    async def _inventory_order(
        self,
        *,
        user_id: str,
        seckill: SeckillProduct,
        stock_key: str,
        tenant_id: int,
        sku_id: int,
        warehouse_id: int,
    ):
        from app.services.order_service import OrderService

        # 先取成普通值：下面一旦 rollback，访问 seckill.id 会触发懒加载并抛
        # MissingGreenlet，把真正的异常盖住。
        seckill_id = seckill.id
        idem = f"seckill:{seckill_id}:{user_id}"
        seckill_price = seckill.seckill_price

        # ① 预留：真正的库存判定
        try:
            reserved = await self.inventory.reserve(
                tenant_id=tenant_id, sku_id=sku_id, warehouse_id=warehouse_id,
                qty=1, idempotency_key=idem,
                ref_type="seckill", ref_id=str(seckill_id),
            )
        except (InsufficientStock, StockRowNotFound):
            await self._rollback_redis(stock_key)
            return {"success": False, "code": "OUT_OF_STOCK", "msg": "库存不足"}

        # ② 活动配额 + 建订单
        order_service = OrderService(self.db)
        try:
            order = await order_service.create_order_from_reservation(
                user_id=user_id, seckill_product_id=seckill_id,
                total_amount=seckill_price,
            )
        except Exception:
            await self._compensate(tenant_id, reserved.reservation_id, idem, stock_key)
            raise

        if order is None:
            await self._compensate(tenant_id, reserved.reservation_id, idem, stock_key)
            return {"success": False, "code": "OUT_OF_STOCK", "msg": "库存不足"}

        # 同上：一旦后面 rollback，order 会过期，这里先取出主键
        order_id = order.id

        # ③ 核销预留：实际出库 + 写流水
        try:
            await self.inventory.commit_reservation(
                tenant_id=tenant_id, reservation_id=reserved.reservation_id,
                idempotency_key=f"{idem}:commit",
            )
        except Exception:
            logger.exception("秒杀核销失败 seckill=%s user=%s", seckill_id, user_id)
            await self._compensate(tenant_id, reserved.reservation_id, idem, stock_key)
            await order_service.cancel_order(order_id)
            raise

        await self._invalidate_seckill_cache(seckill_id)
        await self._invalidate_list_cache()
        return {"success": True, "msg": "下单成功", "order_id": str(order_id)}

    async def _compensate(
        self, tenant_id: int, reservation_id: Optional[int], idem: str, stock_key: str
    ) -> None:
        """补偿：释放预留 + 回补 Redis。两步都不能再把异常抛给调用方。"""
        if reservation_id is not None:
            try:
                await self.inventory.release(
                    tenant_id=tenant_id, reservation_id=reservation_id,
                    idempotency_key=f"{idem}:release",
                )
            except Exception:
                logger.exception("释放预留失败 reservation=%s", reservation_id)
        try:
            await self._rollback_redis(stock_key)
        except Exception:
            logger.exception("Redis 回补失败 key=%s", stock_key)

    # -----------------------------------------------------------------------
    # 兼容期：没有库存记录的活动
    # -----------------------------------------------------------------------
    async def _legacy_order(self, user_id: str, seckill: SeckillProduct, stock_key: str):
        from app.services.order_service import OrderService

        order = await OrderService(self.db).create_order_from_seckill(user_id, seckill.id)
        if order is None:
            await self._rollback_redis(stock_key)
            return {"success": False, "code": "OUT_OF_STOCK", "msg": "库存不足"}
        return {"success": True, "msg": "下单成功", "order_id": str(order.id)}

    # -----------------------------------------------------------------------
    # 对账
    # -----------------------------------------------------------------------
    async def reconcile(self, seckill_product_id: int) -> dict:
        """对比 Redis 计数器与数据库可用量。

        `drift = redis_counter - db_available`：
        - 0    → 一致
        - >0   → Redis 少卖了（有请求被拒但库存还在），需要人工确认
        - <0   → Redis 多卖了（超卖风险），必须告警
        """
        seckill = await self._get_seckill_by_id(seckill_product_id)
        if seckill is None:
            return {"ok": False, "reason": "activity_not_found"}

        stock_key = f"{settings.SECKILL_STOCK_PREFIX}{seckill_product_id}"
        raw = await self.redis.get(stock_key)
        redis_counter = int(raw) if raw is not None else None

        binding = await self.resolve_binding(seckill)
        db_available = None
        if binding is not None:
            _, sku_id, warehouse_id = binding
            try:
                db_available = await self.inventory.get_available(
                    binding[0], sku_id, warehouse_id
                )
            except StockRowNotFound:
                db_available = None

        drift = None
        if redis_counter is not None and db_available is not None:
            drift = redis_counter - db_available
            if drift != 0:
                logger.warning(
                    "秒杀对账不平 seckill=%s redis=%s db=%s drift=%s",
                    seckill_product_id, redis_counter, db_available, drift,
                )

        return {
            "ok": drift == 0,
            "seckill_product_id": seckill_product_id,
            "redis_counter": redis_counter,
            "db_available": db_available,
            "stock_count": seckill.stock_count,
            "drift": drift,
        }

    # -----------------------------------------------------------------------
    # 后台消费者（保留：只确认消息，不再建单）
    # -----------------------------------------------------------------------
    @staticmethod
    async def consume_orders():
        from app.db.redis_client import get_redis

        redis = await get_redis()

        try:
            await redis.xgroup_create(
                settings.SECKILL_QUEUE_NAME, "seckill_consumers", id="0", mkstream=True
            )
        except Exception:
            pass

        while True:
            try:
                result = await redis.xreadgroup(
                    "seckill_consumers", "worker_1",
                    {settings.SECKILL_QUEUE_NAME: ">"}, count=10, block=0,
                )
                if result:
                    for stream_name, messages in result:
                        for message_id, fields in messages:
                            await redis.xack(
                                settings.SECKILL_QUEUE_NAME, "seckill_consumers", message_id
                            )
            except Exception as e:
                logging.error(f"Consumer error: {e}")
                await asyncio.sleep(1)
