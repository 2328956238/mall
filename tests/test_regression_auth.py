"""回归：认证与用户接口。"""


async def test_register(client):
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": "a@example.com", "full_name": "A", "password": "Password123"},
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["email"] == "a@example.com"
    assert data["is_active"] is True
    assert data["is_admin"] is False


async def test_register_duplicate(client):
    payload = {"email": "b@example.com", "full_name": "B", "password": "Password123"}
    assert (await client.post("/api/v1/auth/register", json=payload)).status_code == 201
    assert (await client.post("/api/v1/auth/register", json=payload)).status_code == 400


async def test_login_ok(client):
    await client.post(
        "/api/v1/auth/register",
        json={"email": "c@example.com", "full_name": "C", "password": "Password123"},
    )
    r = await client.post(
        "/api/v1/auth/token",
        data={"username": "c@example.com", "password": "Password123"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert len(body["access_token"]) > 20


async def test_login_wrong_password(client):
    await client.post(
        "/api/v1/auth/register",
        json={"email": "d@example.com", "full_name": "D", "password": "Password123"},
    )
    r = await client.post(
        "/api/v1/auth/token",
        data={"username": "d@example.com", "password": "wrong-pass"},
    )
    assert r.status_code == 401


async def test_me_requires_token(client):
    assert (await client.get("/api/v1/users/me")).status_code == 401


async def test_me_with_token(client, make_user, auth_headers):
    token = await make_user("e@example.com")
    r = await client.get("/api/v1/users/me", headers=auth_headers(token))
    assert r.status_code == 200
    assert r.json()["data"]["email"] == "e@example.com"
