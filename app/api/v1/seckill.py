from fastapi import APIRouter, Depends, HTTPException, status
from app.dependencies import get_current_user
from app.db.redis_client import get_redis
from app.db.session import get_db
from app.services.seckill_service import SeckillService
from app.models.user import User
from app.models.seckill_product import SeckillProduct
from app.schemas.seckill import SeckillResponse, SeckillProductResponse
from app.exceptions import RateLimitException, OutOfStockException
from app.utils.cache import safe_get, safe_delete, safe_delete_pattern
from app.config import settings
from sqlalchemy import select
from typing import List

router = APIRouter(prefix="/seckill", tags=["seckill"])


@router.get("/products/", response_model=List[SeckillProductResponse])
async def get_seckill_products(
    skip: int = 0,
    limit: int = 100,
    db=Depends(get_db),
    redis=Depends(get_redis),
):
    """获取秒杀商品列表（带缓存防护）"""
    cache_key = f"{settings.CACHE_PREFIX}seckill:products:skip={skip}:limit={limit}"

    async def _load():
        result = await db.execute(select(SeckillProduct).offset(skip).limit(limit))
        return result.scalars().all()

    products = await safe_get(
        cache_key=cache_key,
        fetch_func=_load,
        ttl=settings.CACHE_TTL_SECONDS,
        use_mutex=False,
        redis=redis,
    )
    return [SeckillProductResponse.model_validate(p) for p in (products or [])]


@router.post("/{seckill_product_id}", response_model=SeckillResponse)
async def seckill(
    seckill_product_id: int,
    current_user: User = Depends(get_current_user),
    redis=Depends(get_redis),
    db=Depends(get_db),
):
    """
    参与秒杀活动

    用户参与指定秒杀产品的抢购。系统使用Redis预减库存和分布式锁保证高并发下的数据一致性。

    Args:
        seckill_product_id (int): 秒杀产品ID
        current_user (User): 当前认证用户
        redis: Redis客户端连接
        db: 数据库会话

    Returns:
        SeckillResponse: 秒杀结果

    Raises:
        HTTPException:
            - 401: 用户未认证
            - 410: 秒杀活动已结束或库存不足
            - 429: 请求过于频繁（限流）
    """
    service = SeckillService(redis, db)
    result = await service.pre_seckill(str(current_user.id), seckill_product_id)

    if not result["success"]:
        if result["code"] == "TOO_MANY_REQUESTS":
            raise RateLimitException(detail=result["msg"])
        else:
            raise OutOfStockException(detail=result["msg"])

    return SeckillResponse(
        success=True,
        message=result["msg"],
        code="SUCCESS",
        order_id=result.get("order_id"),
    )