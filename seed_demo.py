"""演示数据一键铺底（幂等，可重复跑）。

建这些东西：
  - 两个账号：admin@demo.com / Admin12345（管理员）、user@demo.com / User12345（普通用户）
  - 一个仓库 + 两个商品 + 对应 SKU + 库存
  - 一个进行中的秒杀活动（绑到 SKU/仓库，走库存域）
  - 一个供应商、一个客户（采购/销售页面不至于空着）

用法（服务已经起来、库已迁移）：
    set DATABASE_URL=postgresql+asyncpg://postgres:@127.0.0.1:PORT/flash_sales
    .venv\\Scripts\\python.exe seed_demo.py

重复执行不会报错：已存在的按唯一键跳过。
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

import app.models  # noqa: F401,E402  统一导出所有模型
from app.models.base import Base  # noqa: E402
from app.models.inventory import SKU, SkuStatus, Warehouse, WarehouseStock  # noqa: E402
from app.models.product import Product  # noqa: E402
from app.models.purchase import Supplier  # noqa: E402
from app.models.sales import Customer  # noqa: E402
from app.models.seckill_product import SeckillProduct  # noqa: E402
from app.models.user import User  # noqa: E402
from app.utils.security import get_password_hash  # noqa: E402

TENANT = 1

ACCOUNTS = [
    {"email": "admin@demo.com", "password": "Admin12345", "full_name": "管理员", "is_admin": True},
    {"email": "user@demo.com", "password": "User12345", "full_name": "普通用户", "is_admin": False},
]

PRODUCTS = [
    {"name": "iPhone 15 Pro 256G", "description": "A17 Pro / 钛金属", "price": Decimal("7999.0000"),
     "stock": 100, "sku_code": "IP15P-256", "spec": {"color": "原色钛", "storage": "256G"}},
    {"name": "iPhone 14 128G", "description": "上一代，走量款", "price": Decimal("4999.0000"),
     "stock": 60, "sku_code": "IP14-128", "spec": {"color": "星光色", "storage": "128G"}},
]


async def _one(session, model, **where):
    stmt = select(model)
    for k, v in where.items():
        stmt = stmt.where(getattr(model, k) == v)
    return (await session.execute(stmt)).scalars().first()


async def main() -> None:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("未设置 DATABASE_URL，例如 postgresql+asyncpg://postgres:@127.0.0.1:57952/flash_sales")

    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as s:
        # ---- 账号 ----
        for acc in ACCOUNTS:
            user = await _one(s, User, email=acc["email"])
            if user is None:
                s.add(User(tenant_id=TENANT, email=acc["email"], full_name=acc["full_name"],
                           hashed_password=get_password_hash(acc["password"]),
                           is_active=True, is_admin=acc["is_admin"]))
            else:
                # 已存在就只保证密码和权限正确，不覆盖其他字段
                user.is_admin = acc["is_admin"]
                user.is_active = True
        await s.commit()

        # ---- 仓库 ----
        wh = await _one(s, Warehouse, tenant_id=TENANT, code="WH-SZ")
        if wh is None:
            wh = Warehouse(tenant_id=TENANT, code="WH-SZ", name="深圳仓", country="CN",
                           address="深圳市宝安区", is_active=True)
            s.add(wh)
            await s.commit()

        # ---- 商品 / SKU / 库存 ----
        for p in PRODUCTS:
            product = await _one(s, Product, name=p["name"])
            if product is None:
                product = Product(tenant_id=TENANT, name=p["name"], description=p["description"],
                                  price=p["price"], stock=p["stock"], is_on_sale=True)
                s.add(product)
                await s.commit()

            sku = await _one(s, SKU, tenant_id=TENANT, sku_code=p["sku_code"])
            if sku is None:
                sku = SKU(tenant_id=TENANT, product_id=product.id, sku_code=p["sku_code"],
                          spec=p["spec"], sale_price=p["price"], status=SkuStatus.ACTIVE)
                s.add(sku)
                await s.commit()

            stock = await _one(s, WarehouseStock, sku_id=sku.id, warehouse_id=wh.id)
            if stock is None:
                s.add(WarehouseStock(tenant_id=TENANT, sku_id=sku.id, warehouse_id=wh.id,
                                     on_hand=p["stock"], reserved=0, version=0))
                await s.commit()
            else:
                stock.on_hand = max(stock.on_hand, p["stock"])
                await s.commit()

        # ---- 秒杀活动（进行中，绑 SKU + 仓库）----
        first = await _one(s, Product, name=PRODUCTS[0]["name"])
        first_sku = await _one(s, SKU, tenant_id=TENANT, sku_code=PRODUCTS[0]["sku_code"])
        if await _one(s, SeckillProduct, product_id=first.id) is None:
            now = datetime.utcnow()
            s.add(SeckillProduct(tenant_id=TENANT, product_id=first.id, sku_id=first_sku.id,
                                 warehouse_id=wh.id, seckill_price=Decimal("6999.0000"),
                                 stock_count=10, start_time=now - timedelta(hours=1),
                                 end_time=now + timedelta(days=7)))
            await s.commit()

        # ---- 供应商 / 客户 ----
        if await _one(s, Supplier, tenant_id=TENANT, code="SUP001") is None:
            s.add(Supplier(tenant_id=TENANT, code="SUP001", name="深圳华强电子",
                           contact="李工", phone="0755-00000000", country="CN"))
        if await _one(s, Customer, tenant_id=TENANT, code="CUS001") is None:
            s.add(Customer(tenant_id=TENANT, code="CUS001", name="张三",
                           contact="张三", phone="13800000000", country="CN"))
        await s.commit()

    await engine.dispose()

    print("演示数据已就绪：")
    for acc in ACCOUNTS:
        role = "管理员" if acc["is_admin"] else "普通用户"
        print(f"  {acc['email']:<16} / {acc['password']:<12}  {role}")
    print(f"  商品 {len(PRODUCTS)} 个、仓库 1 个（深圳仓）、秒杀活动 1 个（iPhone 15 Pro 6999）")
    print("  供应商 SUP001 / 客户 CUS001")


if __name__ == "__main__":
    asyncio.run(main())
