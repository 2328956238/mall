from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.models.order import Order, OrderStatus
from app.models.seckill_product import SeckillProduct
from app.models.product import Product
import uuid

class OrderService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_order_from_seckill(self, user_id: str, seckill_product_id: int):
        """秒杀下单：原子扣减库存 + 创建订单，读写合一防止超卖"""
        # 1. 查询秒杀商品信息
        result = await self.db.execute(
            select(SeckillProduct).where(SeckillProduct.id == seckill_product_id)
        )
        seckill = result.scalar_one_or_none()
        if not seckill:
            return None

        # 2. 原子扣减商品库存（读写合一：一条 SQL 完成"读+判断+写"）
        # UPDATE products SET stock = stock - 1 WHERE id = :id AND stock > 0
        # 数据库行锁保证并发安全，不会超卖
        stmt = (
            update(Product)
            .where(Product.id == seckill.product_id)
            .where(Product.stock > 0)
            .values(stock=Product.stock - 1)
        )
        result = await self.db.execute(stmt)

        if result.rowcount == 0:
            # 库存为0，扣减失败 → 说明已卖完
            return None

        # 3. 扣减秒杀商品的可抢数量（同样原子操作）
        seckill_stmt = (
            update(SeckillProduct)
            .where(SeckillProduct.id == seckill_product_id)
            .where(SeckillProduct.stock_count > 0)
            .values(stock_count=SeckillProduct.stock_count - 1)
        )
        seckill_result = await self.db.execute(seckill_stmt)
        if seckill_result.rowcount == 0:
            # 秒杀库存为0，需要回滚商品库存
            await self.db.execute(
                update(Product)
                .where(Product.id == seckill.product_id)
                .values(stock=Product.stock + 1)
            )
            await self.db.commit()
            return None

        # 4. 处理 user_id - 兼容 asyncpg UUID 和字符串
        if hasattr(user_id, "hex"):
            user_uuid = user_id
        else:
            user_uuid = uuid.UUID(user_id)

        # 5. 创建订单
        order = Order(
            user_id=user_uuid,
            seckill_product_id=seckill_product_id,
            quantity=1,
            total_amount=seckill.seckill_price,
            status=OrderStatus.PENDING
        )
        self.db.add(order)
        await self.db.commit()
        return order

    async def get_order_by_id(self, order_id: str):
        if hasattr(order_id, "hex"):
            order_uuid = order_id
        else:
            order_uuid = uuid.UUID(order_id)
        result = await self.db.execute(select(Order).where(Order.id == order_uuid))
        return result.scalar_one_or_none()

    async def get_orders(self, skip: int = 0, limit: int = 100):
        result = await self.db.execute(select(Order).offset(skip).limit(limit))
        return result.scalars().all()

    async def get_user_orders(self, user_id: str, skip: int = 0, limit: int = 100):
        if hasattr(user_id, "hex"):
            user_uuid = user_id
        else:
            user_uuid = uuid.UUID(user_id)
        result = await self.db.execute(
            select(Order).where(Order.user_id == user_uuid)
            .offset(skip).limit(limit).order_by(Order.created_at.desc())
        )
        return result.scalars().all()
