import logging
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.models.product import Product
from app.schemas.product import ProductCreate, ProductUpdate
from app.utils.cache import safe_get, safe_delete

logger = logging.getLogger(__name__)

# 缓存key前缀
PRODUCT_CACHE_PREFIX = "product"
# 单个商品缓存TTL（秒），带随机偏移防雪崩
PRODUCT_CACHE_TTL = 300  # 5分钟
# 商品列表缓存TTL更短，因为列表变更频繁
PRODUCT_LIST_CACHE_TTL = 60  # 1分钟


class ProductService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_product_by_id(self, product_id: int) -> Optional[Product]:
        """
        获取单个产品 — 带缓存穿透/击穿/雪崩三重防护
        
        缓存穿透防护：不存在的ID会被缓存空值（30s TTL）
        缓存击穿防护：互斥锁机制防止热点key重建时大量请求打到DB
        缓存雪崩防护：最终TTL = 基础TTL + random(0, 60s)
        """
        cache_key = f"{PRODUCT_CACHE_PREFIX}:{product_id}"
        
        async def _load_from_db() -> Optional[Product]:
            """从数据库加载商品（闭包捕获 product_id）"""
            result = await self.db.execute(select(Product).where(Product.id == product_id))
            obj = result.scalar_one_or_none()
            return obj
        
        return await safe_get(
            cache_key=cache_key,
            fetch_func=_load_from_db,
            ttl=PRODUCT_CACHE_TTL,
            use_mutex=True,  # 热点商品开启互斥锁防击穿
        )

    async def get_products(self, skip: int = 0, limit: int = 100) -> list[Product]:
        """
        获取产品列表 — 带缓存防护
        
        列表缓存使用带分页参数的key，避免不同分页互相影响。
        由于列表变更频繁，TTL较短。
        """
        cache_key = f"{PRODUCT_CACHE_PREFIX}:list:skip={skip}:limit={limit}"
        
        async def _load_products_from_db() -> list[Product]:
            """从数据库加载产品列表（闭包捕获 skip, limit）"""
            result = await self.db.execute(select(Product).offset(skip).limit(limit))
            return result.scalars().all()
        
        result = await safe_get(
            cache_key=cache_key,
            fetch_func=_load_products_from_db,
            ttl=PRODUCT_LIST_CACHE_TTL,
            use_mutex=False,  # 列表缓存非热点，不加互斥锁
        )
        return result or []

    def _build_cache_key(self, product_id: int) -> str:
        """构建商品缓存key"""
        return f"{PRODUCT_CACHE_PREFIX}:{product_id}"

    async def _invalidate_product_cache(self, product_id: int) -> None:
        """删除指定商品的缓存"""
        await safe_delete(self._build_cache_key(product_id))

    async def _invalidate_list_cache(self) -> None:
        """删除所有商品列表缓存"""
        from app.utils.cache import safe_delete_pattern
        await safe_delete_pattern(f"{PRODUCT_CACHE_PREFIX}:list:*")

    async def create_product(self, product_in: ProductCreate) -> Product:
        """创建商品 — 写入DB后清除列表缓存"""
        product = Product(
            name=product_in.name,
            description=product_in.description,
            price=product_in.price,
            stock=product_in.stock,
            is_on_sale=product_in.is_on_sale
        )
        self.db.add(product)
        await self.db.flush()
        await self.db.refresh(product)
        await self.db.commit()
        # 清除列表缓存（新增商品后列表变化）
        await self._invalidate_list_cache()
        logger.info(f"Created product {product.id}, list cache invalidated")
        return product

    async def update_product(self, product_id: int, product_in: ProductUpdate) -> Optional[Product]:
        """更新商品 — 更新DB后清除该商品和列表缓存"""
        product = await self.get_product_by_id(product_id)
        if not product:
            return None
        for key, value in product_in.dict(exclude_unset=True).items():
            setattr(product, key, value)
        await self.db.commit()
        await self.db.refresh(product)
        # 清除相关缓存：该商品缓存 + 列表缓存
        await self._invalidate_product_cache(product_id)
        await self._invalidate_list_cache()
        logger.info(f"Updated product {product_id}, cache invalidated")
        return product

    async def delete_product(self, product_id: int) -> bool:
        """删除商品 — 删除DB后清除该商品和列表缓存"""
        product = await self.get_product_by_id(product_id)
        if not product:
            return False
        await self.db.delete(product)
        await self.db.commit()
        # 清除相关缓存
        await self._invalidate_product_cache(product_id)
        await self._invalidate_list_cache()
        logger.info(f"Deleted product {product_id}, cache invalidated")
        return True