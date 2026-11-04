"""回归：健康检查与商品 CRUD（改造前必须锁住的行为）。"""


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_create_product(client):
    r = await client.post(
        "/api/v1/products/",
        json={"name": "iPhone 15", "description": "最新款", "price": 7999.99, "stock": 100},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "iPhone 15"
    assert float(body["price"]) == 7999.99   # Decimal 序列化为字符串，转 float 比较
    assert body["stock"] == 100
    assert body["is_on_sale"] is True
    assert body["id"] > 0


async def test_get_product(client, make_product):
    p = await make_product(name="AirPods", price=1499.0, stock=5)
    r = await client.get(f"/api/v1/products/{p['id']}")
    assert r.status_code == 200
    assert r.json()["name"] == "AirPods"


async def test_get_product_404(client):
    r = await client.get("/api/v1/products/999999")
    assert r.status_code == 404


async def test_list_products(client, make_product):
    await make_product(name="A")
    await make_product(name="B")
    r = await client.get("/api/v1/products/")
    assert r.status_code == 200
    names = {x["name"] for x in r.json()}
    assert {"A", "B"} <= names


async def test_update_product(client, make_product):
    """第 4 步起 stock 是只读汇总字段，只能改其它字段。

    （这条断言随语义变化更新过：原先是 price+stock 一起改，
      现在改 stock 会返回 409，见 test_product_stock_sync.py）
    """
    p = await make_product(price=100.0, stock=10)
    r = await client.put(f"/api/v1/products/{p['id']}", json={"price": 88.0})
    assert r.status_code == 200
    assert float(r.json()["price"]) == 88.0
    assert r.json()["stock"] == 10

    blocked = await client.put(f"/api/v1/products/{p['id']}", json={"stock": 3})
    assert blocked.status_code == 409


async def test_delete_product(client, make_product):
    p = await make_product()
    r = await client.delete(f"/api/v1/products/{p['id']}")
    assert r.status_code == 200
    assert (await client.get(f"/api/v1/products/{p['id']}")).status_code == 404


async def test_create_product_validation(client):
    """价格必须 > 0，库存不能为负 —— 现有校验不能被改坏。"""
    r = await client.post("/api/v1/products/", json={"name": "X", "price": 0, "stock": 1})
    assert r.status_code == 422
    r = await client.post("/api/v1/products/", json={"name": "X", "price": 10, "stock": -1})
    assert r.status_code == 422
