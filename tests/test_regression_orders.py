"""回归：订单查询与越权控制。"""


async def _create_seckill_order(client, token, headers, sid):
    r = await client.post(f"/api/v1/seckill/{sid}", headers=headers(token))
    assert r.status_code == 200, r.text
    return r.json()["order_id"]


async def test_list_my_orders(
    client, make_user, auth_headers, make_product, make_seckill
):
    token = await make_user("o1@example.com")
    p = await make_product(stock=10)
    sp = await make_seckill(p["id"], stock=10)
    order_id = await _create_seckill_order(client, token, auth_headers, sp.id)

    r = await client.get("/api/v1/orders/", headers=auth_headers(token))
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data) == 1
    assert data[0]["id"] == order_id
    assert data[0]["status"] == "pending"


async def test_get_my_order(client, make_user, auth_headers, make_product, make_seckill):
    token = await make_user("o2@example.com")
    p = await make_product(stock=10)
    sp = await make_seckill(p["id"], stock=10)
    order_id = await _create_seckill_order(client, token, auth_headers, sp.id)

    r = await client.get(f"/api/v1/orders/{order_id}", headers=auth_headers(token))
    assert r.status_code == 200
    assert r.json()["data"]["id"] == order_id


async def test_cannot_read_others_order(
    client, make_user, auth_headers, make_product, make_seckill
):
    """A 的订单 B 不能看 —— 改造后必须保持。"""
    owner = await make_user("owner@example.com")
    other = await make_user("other@example.com")
    p = await make_product(stock=10)
    sp = await make_seckill(p["id"], stock=10)
    order_id = await _create_seckill_order(client, owner, auth_headers, sp.id)

    r = await client.get(f"/api/v1/orders/{order_id}", headers=auth_headers(other))
    assert r.status_code == 403


async def test_order_not_found(client, make_user, auth_headers):
    token = await make_user("o3@example.com")
    r = await client.get(
        "/api/v1/orders/00000000-0000-0000-0000-000000000000",
        headers=auth_headers(token),
    )
    assert r.status_code == 404


async def test_orders_requires_auth(client):
    assert (await client.get("/api/v1/orders/")).status_code == 401
