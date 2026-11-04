"""并发回归：秒杀不能超卖。

这是整个改造最重要的一条回归线 ——
第一阶段会把库存从 `products.stock` 迁到 `warehouse_stock`，
如果迁移后这条测试挂了，说明新库存域没有守住不变量。
"""

import asyncio

from sqlalchemy import func, select


async def test_concurrent_seckill_no_oversell(
    client, make_user, auth_headers, make_product, make_seckill, db_session
):
    from app.models.order import Order
    from app.models.product import Product
    from app.models.seckill_product import SeckillProduct

    stock = 5
    buyers = 10

    tokens = [await make_user(f"buyer{i}@example.com") for i in range(buyers)]
    p = await make_product(stock=stock)
    sp = await make_seckill(p["id"], stock=stock)

    async def buy(token):
        return await client.post(f"/api/v1/seckill/{sp.id}", headers=auth_headers(token))

    results = await asyncio.wait_for(
        asyncio.gather(*[buy(t) for t in tokens]), timeout=60
    )
    codes = [r.status_code for r in results]
    ok = codes.count(200)

    assert ok == stock, f"应该恰好卖出 {stock} 件，实际 {ok}；状态分布={codes}"

    product = (await db_session.execute(select(Product).where(Product.id == p["id"]).execution_options(populate_existing=True))).scalar_one()
    seckill = (
        await db_session.execute(select(SeckillProduct).where(SeckillProduct.id == sp.id).execution_options(populate_existing=True))
    ).scalar_one()
    order_count = (await db_session.execute(select(func.count()).select_from(Order))).scalar()

    assert product.stock == 0, f"商品库存应为 0，实际 {product.stock}"
    assert seckill.stock_count == 0, f"秒杀库存应为 0，实际 {seckill.stock_count}"
    assert order_count == stock, f"应生成 {stock} 笔订单，实际 {order_count}"

    # 不变量：任何库存都不能为负
    assert product.stock >= 0 and seckill.stock_count >= 0


async def test_same_user_only_wins_once(
    client, make_user, auth_headers, make_product, make_seckill
):
    """同一用户并发抢，令牌桶容量 1，只应成功 1 次。"""
    token = await make_user("solo@example.com")
    p = await make_product(stock=10)
    sp = await make_seckill(p["id"], stock=10)

    async def buy():
        return await client.post(f"/api/v1/seckill/{sp.id}", headers=auth_headers(token))

    results = await asyncio.wait_for(
        asyncio.gather(*[buy() for _ in range(5)]), timeout=60
    )
    codes = [r.status_code for r in results]
    assert codes.count(200) == 1, f"同一用户只应成功一次，状态分布={codes}"
