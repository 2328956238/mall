"""第四阶段：SaaS 权限体系测试。

覆盖：权限字典、默认角色、角色分配、接口权限、数据范围、部门树、操作审计。
"""

from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.models.inventory import SKU, Warehouse, WarehouseStock
from app.models.operation_log import OperationLog
from app.models.product import Product
from app.models.rbac import DataScope, Department, Permission, Role, RolePermission, UserRole
from app.models.tenant import Tenant
from app.services.permission_service import (
    ROLE_TEMPLATES,
    PermissionService,
    RoleError,
    require_permission,
)


# ---------------------------------------------------------------------------
# 权限字典与默认角色
# ---------------------------------------------------------------------------
async def test_seed_permissions_idempotent(db_session):
    svc = PermissionService(db_session)
    first = await svc.seed_permissions()
    second = await svc.seed_permissions()

    total = (await db_session.execute(select(Permission))).scalars().all()
    assert first == len(total)
    assert second == 0                      # 第二次不再新增


async def test_seed_tenant_defaults(db_session):
    ids = await _tenant(db_session, "t-rbac")
    svc = PermissionService(db_session)
    result = await svc.seed_tenant_defaults(ids["tenant_id"])

    roles = (
        await db_session.execute(
            select(Role).where(Role.tenant_id == ids["tenant_id"]).order_by(Role.code)
        )
    ).scalars().all()
    assert {r.code for r in roles} == {t[0] for t in ROLE_TEMPLATES}

    admin = next(r for r in roles if r.code == "admin")
    assert admin.data_scope == DataScope.ALL

    # admin 拿全部权限
    links = (
        await db_session.execute(
            select(RolePermission).where(RolePermission.role_id == admin.id)
        )
    ).scalars().all()
    all_perms = (await db_session.execute(select(Permission))).scalars().all()
    assert len(links) == len(all_perms)

    # 幂等：再跑一次不新增角色
    again = await svc.seed_tenant_defaults(ids["tenant_id"])
    assert again["roles_created"] == []


async def test_assign_role_and_permissions(db_session):
    ids = await _tenant(db_session, "t-rbac2")
    svc = PermissionService(db_session)
    await svc.seed_tenant_defaults(ids["tenant_id"])

    await svc.assign_role(ids["tenant_id"], "user-1", "operator")
    perms = await svc.get_user_permissions("user-1")

    assert "sales.create" in perms
    assert "purchase.approve" not in perms      # 业务员没有审核权
    assert await svc.effective_scope("user-1") == DataScope.SELF


async def test_effective_scope_takes_widest(db_session):
    ids = await _tenant(db_session, "t-rbac3")
    svc = PermissionService(db_session)
    await svc.seed_tenant_defaults(ids["tenant_id"])

    await svc.assign_role(ids["tenant_id"], "user-2", "operator")   # self
    await svc.assign_role(ids["tenant_id"], "user-2", "manager")    # department
    assert await svc.effective_scope("user-2") == DataScope.DEPARTMENT


async def test_assign_unknown_role(db_session):
    ids = await _tenant(db_session, "t-rbac4")
    svc = PermissionService(db_session)
    with pytest.raises(RoleError):
        await svc.assign_role(ids["tenant_id"], "user-3", "nope")


async def test_bootstrap_first_user_is_admin(db_session):
    ids = await _tenant(db_session, "t-rbac5")
    svc = PermissionService(db_session)
    await svc.init_tenant_bootstrap(ids["tenant_id"], "first-user")

    assert "system.role.manage" in await svc.get_user_permissions("first-user")


# ---------------------------------------------------------------------------
# 接口权限
# ---------------------------------------------------------------------------
async def _register(client, email, password="Password123"):
    """注册并返回 (token, user_id)。

    角色分配必须用 **UUID**：JWT 里的 sub 是用户主键，
    用邮箱当 user_id 会查不到角色（踩过）。
    """
    r = await client.post("/api/v1/auth/register", json={
        "email": email, "full_name": "T", "password": password,
    })
    assert r.status_code == 201, r.text
    uid = r.json()["data"]["id"]
    r = await client.post("/api/v1/auth/token",
                          data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"], uid

async def test_permission_denied_without_role(client, db_session, make_user, auth_headers, monkeypatch):
    from app.config import settings

    await _tenant(db_session, "t-rbac6")
    token = await make_user("nobody@example.com")
    monkeypatch.setattr(settings, "ENFORCE_PERMISSIONS", True)

    r = await client.post("/api/v1/purchase/suppliers",
                          json={"code": "X1", "name": "X"}, headers=auth_headers(token))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "NO_ROLE"


async def test_permission_allowed_with_admin(
    client, db_session, make_user, auth_headers, monkeypatch
):
    from app.config import settings

    await _tenant(db_session, "t-rbac7")
    token, uid = await _register(client, "admin@example.com")
    svc = PermissionService(db_session)
    await svc.seed_tenant_defaults(1)
    await svc.assign_role(1, uid, "admin")

    monkeypatch.setattr(settings, "ENFORCE_PERMISSIONS", True)
    r = await client.post("/api/v1/purchase/suppliers",
                          json={"code": "X2", "name": "X"}, headers=auth_headers(token))
    assert r.status_code == 201, r.text


async def test_operator_cannot_approve(db_session, monkeypatch):
    """角色权限表的组合确实能表达"能建单但不能审核"。"""
    from app.config import settings

    ids = await _tenant(db_session, "t-rbac8")
    svc = PermissionService(db_session)
    await svc.seed_tenant_defaults(ids["tenant_id"])
    await svc.assign_role(ids["tenant_id"], "op-1", "operator")
    await svc.assign_role(ids["tenant_id"], "mgr-1", "manager")

    assert not await svc.user_has_permission("op-1", "purchase.approve")
    assert await svc.user_has_permission("mgr-1", "purchase.approve")


# ---------------------------------------------------------------------------
# 数据范围
# ---------------------------------------------------------------------------
async def test_self_scope_filters_sales_orders(
    client, db_session, make_user, auth_headers, monkeypatch
):
    from app.config import settings

    ids = await _seed_sales(db_session)
    svc = PermissionService(db_session)
    await svc.seed_tenant_defaults(1)

    token_a, uid_a = await _register(client, "a@example.com")
    token_b, uid_b = await _register(client, "b@example.com")

    # 两个用户各建一张单
    for token in (token_a, token_b):
        r = await client.post("/api/v1/sales/orders", json={
            "customer_id": ids["customer_id"],
            "items": [{"sku_id": ids["sku1"], "warehouse_id": ids["wh"],
                       "qty": 1, "unit_price": "10.00"}],
        }, headers=auth_headers(token))
        assert r.status_code == 201, r.text

    # 默认（不强制）两人都能看到两张
    r = await client.get("/api/v1/sales/orders", headers=auth_headers(token_a))
    assert len(r.json()["data"]) == 2

    # 开启强制 + 只给 self 范围 → 各自只看到自己的
    await svc.assign_role(1, uid_a, "operator")
    await svc.assign_role(1, uid_b, "operator")
    monkeypatch.setattr(settings, "ENFORCE_PERMISSIONS", True)

    r = await client.get("/api/v1/sales/orders", headers=auth_headers(token_a))
    assert len(r.json()["data"]) == 1


# ---------------------------------------------------------------------------
# 部门
# ---------------------------------------------------------------------------
async def test_department_tree(db_session):
    ids = await _tenant(db_session, "t-rbac9")
    svc = PermissionService(db_session)

    root = await svc.create_department(tenant_id=ids["tenant_id"], code="HQ", name="总部")
    child = await svc.create_department(
        tenant_id=ids["tenant_id"], code="SZ", name="深圳分部", parent_id=root.id
    )
    grand = await svc.create_department(
        tenant_id=ids["tenant_id"], code="SZ-1", name="深圳一组", parent_id=child.id
    )

    assert root.path == str(root.id)
    assert child.path == f"{root.path}/{child.id}"
    assert grand.path.startswith(child.path)

    subtree = await svc.department_subtree_ids(ids["tenant_id"], root.id)
    assert {root.id, child.id, grand.id} <= set(subtree)

    only_child = await svc.department_subtree_ids(ids["tenant_id"], child.id)
    assert root.id not in only_child
    assert grand.id in only_child


async def test_department_unknown_parent(db_session):
    ids = await _tenant(db_session, "t-rbac10")
    svc = PermissionService(db_session)
    with pytest.raises(RoleError):
        await svc.create_department(
            tenant_id=ids["tenant_id"], code="X", name="X", parent_id=99999
        )


# ---------------------------------------------------------------------------
# 操作审计
# ---------------------------------------------------------------------------
async def test_audit_written_for_write_requests(client, db_session, make_user, auth_headers):
    await _tenant(db_session, "t-audit")
    token = await make_user("auditor@example.com")

    r = await client.post("/api/v1/purchase/suppliers",
                          json={"code": "AUD1", "name": "审计供应商"},
                          headers=auth_headers(token))
    assert r.status_code == 201

    rows = (
        await db_session.execute(
            select(OperationLog).where(OperationLog.request_method == "POST")
        )
    ).scalars().all()
    assert len(rows) >= 1
    latest = rows[-1]
    assert latest.request_path.endswith("/purchase/suppliers")
    assert latest.response_status == 201
    assert latest.tenant_id == 1


async def test_audit_skips_read_requests(client, db_session, make_user, auth_headers):
    await _tenant(db_session, "t-audit2")
    token = await make_user("reader@example.com")

    await client.get("/api/v1/purchase/suppliers", headers=auth_headers(token))

    rows = (
        await db_session.execute(
            select(OperationLog).where(OperationLog.request_method == "GET")
        )
    ).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
async def _tenant(session, code) -> dict:
    tenant = Tenant(code=code, name="权限租户", status="active")
    session.add(tenant)
    await session.flush()
    await session.commit()
    return {"tenant_id": tenant.id}


async def _seed_sales(session) -> dict:
    """在默认租户下建客户 + SKU + 仓库库存。"""
    tenant_id = 1
    product = Product(tenant_id=tenant_id, name="权限商品", description="t",
                      price=Decimal("10.0000"), stock=0, is_on_sale=True)
    session.add(product)
    await session.flush()

    sku = SKU(tenant_id=tenant_id, product_id=product.id, sku_code="RBAC-SKU",
              spec={}, sale_price=Decimal("10.0000"))
    wh = Warehouse(tenant_id=tenant_id, code="W-RBAC", name="权限仓", country="CN")
    session.add_all([sku, wh])
    await session.flush()
    session.add(WarehouseStock(tenant_id=tenant_id, sku_id=sku.id,
                               warehouse_id=wh.id, on_hand=10, reserved=0))

    from app.models.sales import Customer

    customer = Customer(tenant_id=tenant_id, code="RBAC-C", name="权限客户")
    session.add(customer)
    await session.commit()

    return {"customer_id": customer.id, "sku1": sku.id, "wh": wh.id}
