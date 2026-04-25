from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.dependencies import get_current_admin
from app.db.session import get_db
from app.models.seckill_product import SeckillProduct
from app.models.product import Product
from app.models.user import User
from app.schemas.seckill import SeckillProductCreate, SeckillProductUpdate, SeckillProductResponse
from app.schemas.common import APIResponse
from app.db.redis_client import get_redis
from app.config import settings
from app.utils.cache import safe_delete, safe_delete_pattern
from typing import List

router = APIRouter(prefix="/seckill", tags=["admin-seckill"])


@router.get("/", response_model=APIResponse[List[SeckillProductResponse]])
async def admin_list_seckill(
    skip: int = 0,
    limit: int = 100,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理员：获取秒杀产品列表"""
    result = await db.execute(select(SeckillProduct).offset(skip).limit(limit))
    seckill_products = result.scalars().all()
    return APIResponse(data=seckill_products)


@router.post("/", response_model=APIResponse[SeckillProductResponse])
async def admin_create_seckill(
    seckill_in: SeckillProductCreate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """管理员：创建秒杀产品，同时初始化 Redis 库存"""
    # 验证关联商品存在
    product = await db.get(Product, seckill_in.product_id)
    if not product:
        raise HTTPException(404, "关联的商品不存在")

    seckill = SeckillProduct(
        product_id=seckill_in.product_id,
        seckill_price=seckill_in.seckill_price,
        stock_count=seckill_in.stock_count,
        start_time=seckill_in.start_time.replace(tzinfo=None),
        end_time=seckill_in.end_time.replace(tzinfo=None),
    )
    db.add(seckill)
    await db.commit()
    await db.refresh(seckill)

    # 初始化 Redis 库存
    stock_key = f"{settings.SECKILL_STOCK_PREFIX}{seckill.id}"
    await redis.set(stock_key, seckill.stock_count)

    # 清理秒杀列表缓存
    await safe_delete_pattern(f"{settings.CACHE_PREFIX}seckill:products:*", redis=redis)
    await safe_delete(f"{settings.CACHE_PREFIX}seckill:id:{seckill.id}", redis=redis)

    return APIResponse(data=seckill, message="秒杀产品创建成功")


@router.put("/{seckill_id}", response_model=APIResponse[SeckillProductResponse])
async def admin_update_seckill(
    seckill_id: int,
    seckill_in: SeckillProductUpdate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """管理员：更新秒杀产品信息"""
    seckill = await db.get(SeckillProduct, seckill_id)
    if not seckill:
        raise HTTPException(404, "秒杀产品不存在")

    update_data = seckill_in.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(seckill, key, value)

    await db.commit()
    await db.refresh(seckill)

    # 如果更新了库存，同步到 Redis
    if "stock_count" in update_data:
        stock_key = f"{settings.SECKILL_STOCK_PREFIX}{seckill.id}"
        await redis.set(stock_key, seckill.stock_count)

    # 清理相关缓存
    await safe_delete(f"{settings.CACHE_PREFIX}seckill:id:{seckill.id}", redis=redis)
    await safe_delete_pattern(f"{settings.CACHE_PREFIX}seckill:products:*", redis=redis)

    return APIResponse(data=seckill, message="秒杀产品更新成功")


@router.delete("/{seckill_id}")
async def admin_delete_seckill(
    seckill_id: int,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """管理员：删除秒杀产品"""
    seckill = await db.get(SeckillProduct, seckill_id)
    if not seckill:
        raise HTTPException(404, "秒杀产品不存在")

    await db.delete(seckill)
    await db.commit()

    # 清理 Redis 库存
    stock_key = f"{settings.SECKILL_STOCK_PREFIX}{seckill.id}"
    await redis.delete(stock_key)

    # 清理相关缓存
    await safe_delete(f"{settings.CACHE_PREFIX}seckill:id:{seckill.id}", redis=redis)
    await safe_delete_pattern(f"{settings.CACHE_PREFIX}seckill:products:*", redis=redis)

    return APIResponse(message="秒杀产品已删除")