"""权限服务：角色、权限校验、数据范围、默认初始化。

关于"迁移期兼容"：

存量用户一个角色都没有，如果直接强制校验会全部 403。所以：

- `settings.ENFORCE_PERMISSIONS = False`（默认）时，**没有分配角色的用户放行**，
  已在跑的业务不受影响；
- 设为 `True` 后严格校验，缺权限返回 403；
- 新注册用户会自动拿到租户的 `admin`（租户内第一个用户）或 `operator` 角色。

上线步骤：先把 `ENFORCE_PERMISSIONS=False` 跑一段 → 用 `seed_tenant_defaults` 建角色
→ 给存量用户 `assign_role` → 再打开开关。
"""

from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.rbac import (
    DataScope,
    Department,
    Permission,
    PermissionType,
    Role,
    RolePermission,
    UserRole,
)


# ---------------------------------------------------------------------------
# 权限字典（平台统一维护）
# ---------------------------------------------------------------------------
PERMISSION_CATALOG: list[tuple[str, str, PermissionType, str, str]] = [
    # code, name, type, resource, action
    ("product.view", "商品查看", PermissionType.MENU, "product", "view"),
    ("product.manage", "商品维护", PermissionType.API, "product", "manage"),
    ("inventory.view", "库存查看", PermissionType.MENU, "inventory", "view"),
    ("inventory.adjust", "库存调整", PermissionType.API, "inventory", "adjust"),
    ("purchase.view", "采购查看", PermissionType.MENU, "purchase", "view"),
    ("purchase.create", "创建采购单", PermissionType.API, "purchase", "create"),
    ("purchase.approve", "审核采购单", PermissionType.API, "purchase", "approve"),
    ("purchase.receive", "采购收货", PermissionType.API, "purchase", "receive"),
    ("sales.view", "销售查看", PermissionType.MENU, "sales", "view"),
    ("sales.create", "创建销售订单", PermissionType.API, "sales", "create"),
    ("sales.approve", "审核销售订单", PermissionType.API, "sales", "approve"),
    ("sales.ship", "确认发货", PermissionType.API, "sales", "ship"),
    ("system.role.manage", "角色管理", PermissionType.API, "system", "role_manage"),
    ("system.audit.view", "审计查看", PermissionType.MENU, "system", "audit_view"),
    ("data.scope", "数据范围", PermissionType.DATA, "data", "scope"),
]

# 默认角色 → 权限集合（"*" 表示全部）
ROLE_TEMPLATES: list[tuple[str, str, DataScope, list[str]]] = [
    ("admin", "管理员", DataScope.ALL, ["*"]),
    ("manager", "部门主管", DataScope.DEPARTMENT, [
        "product.view", "product.manage", "inventory.view", "inventory.adjust",
        "purchase.view", "purchase.create", "purchase.approve", "purchase.receive",
        "sales.view", "sales.create", "sales.approve", "sales.ship",
        "system.audit.view", "data.scope",
    ]),
    ("operator", "业务员", DataScope.SELF, [
        "product.view", "inventory.view",
        "purchase.view", "purchase.create", "purchase.receive",
        "sales.view", "sales.create", "sales.ship", "data.scope",
    ]),
]


class PermissionError_(Exception):
    code = "PERMISSION_DENIED"
    http_status = 403

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class PermissionDenied(PermissionError_):
    code = "PERMISSION_DENIED"
    http_status = 403


class RoleError(Exception):
    code = "ROLE_ERROR"
    http_status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class PermissionService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # -----------------------------------------------------------------------
    # 初始化
    # -----------------------------------------------------------------------
    async def seed_permissions(self) -> int:
        """把权限字典写进库（幂等）。返回新增数量。"""
        existing = {
            code for (code,) in (
                await self.db.execute(select(Permission.code))
            ).all()
        }
        created = 0
        for code, name, ptype, resource, action in PERMISSION_CATALOG:
            if code in existing:
                continue
            self.db.add(Permission(code=code, name=name, type=ptype,
                                   resource=resource, action=action))
            created += 1
        await self.db.commit()
        return created

    async def seed_tenant_defaults(self, tenant_id: int) -> dict:
        """给租户建默认角色并绑定权限（幂等）。"""
        await self.seed_permissions()

        perm_rows = (await self.db.execute(select(Permission))).scalars().all()
        perm_by_code = {p.code: p for p in perm_rows}

        created_roles = []
        for code, name, scope, codes in ROLE_TEMPLATES:
            role = (
                await self.db.execute(
                    select(Role).where(Role.tenant_id == tenant_id, Role.code == code)
                )
            ).scalar_one_or_none()
            if role is None:
                role = Role(tenant_id=tenant_id, code=code, name=name,
                            data_scope=scope, is_system=True)
                self.db.add(role)
                await self.db.flush()
                created_roles.append(code)

            wanted = (
                list(perm_by_code.values()) if codes == ["*"]
                else [perm_by_code[c] for c in codes if c in perm_by_code]
            )
            have = {
                rp.permission_id for rp in (
                    await self.db.execute(
                        select(RolePermission).where(RolePermission.role_id == role.id)
                    )
                ).scalars().all()
            }
            for perm in wanted:
                if perm.id not in have:
                    self.db.add(RolePermission(role_id=role.id, permission_id=perm.id))

        await self.db.commit()
        return {"roles_created": created_roles}

    # -----------------------------------------------------------------------
    # 查询
    # -----------------------------------------------------------------------
    async def get_user_roles(self, user_id: str) -> list[Role]:
        rows = (
            await self.db.execute(
                select(Role).join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.user_id == str(user_id))
            )
        ).scalars().all()
        return list(rows)

    async def get_user_permissions(self, user_id: str) -> set[str]:
        role_ids = [r.id for r in await self.get_user_roles(user_id)]
        if not role_ids:
            return set()
        rows = (
            await self.db.execute(
                select(Permission.code)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id.in_(role_ids))
            )
        ).all()
        return {code for (code,) in rows}

    async def user_has_permission(self, user_id: str, code: str) -> bool:
        return code in await self.get_user_permissions(user_id)

    async def effective_scope(self, user_id: str) -> DataScope:
        """多个角色取最宽的数据范围。"""
        roles = await self.get_user_roles(user_id)
        if not roles:
            return DataScope.SELF
        order = {DataScope.SELF: 0, DataScope.DEPARTMENT: 1, DataScope.ALL: 2}
        return max((r.data_scope for r in roles), key=lambda s: order[s])

    # -----------------------------------------------------------------------
    # 分配
    # -----------------------------------------------------------------------
    async def assign_role(self, tenant_id: int, user_id: str, role_code: str) -> UserRole:
        role = (
            await self.db.execute(
                select(Role).where(Role.tenant_id == tenant_id, Role.code == role_code)
            )
        ).scalar_one_or_none()
        if role is None:
            raise RoleError(f"角色不存在：{role_code}")

        exists = (
            await self.db.execute(
                select(UserRole).where(
                    UserRole.user_id == str(user_id), UserRole.role_id == role.id
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            return exists

        link = UserRole(tenant_id=tenant_id, user_id=str(user_id), role_id=role.id)
        self.db.add(link)
        await self.db.commit()
        return link

    async def init_tenant_bootstrap(self, tenant_id: int, first_user_id: str) -> None:
        """租户第一次有用户时：建默认角色，并把第一个用户设为 admin。"""
        await self.seed_tenant_defaults(tenant_id)
        try:
            await self.assign_role(tenant_id, first_user_id, "admin")
        except RoleError:
            pass

    # -----------------------------------------------------------------------
    # 部门
    # -----------------------------------------------------------------------
    async def create_department(
        self, *, tenant_id: int, code: str, name: str,
        parent_id: Optional[int] = None,
    ) -> Department:
        if parent_id is not None:
            parent = (
                await self.db.execute(
                    select(Department).where(
                        Department.id == parent_id, Department.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if parent is None:
                raise RoleError(f"上级部门不存在：{parent_id}")
            # parent.path 已经包含 parent 自己的 id，例如 "1" 或 "1/2"
            base_path = parent.path
        else:
            base_path = ""

        dept = Department(tenant_id=tenant_id, code=code, name=name,
                          parent_id=parent_id, path="")
        self.db.add(dept)
        await self.db.flush()
        dept.path = f"{base_path}/{dept.id}" if base_path else str(dept.id)
        await self.db.commit()
        return dept

    async def department_subtree_ids(self, tenant_id: int, department_id: int) -> list[int]:
        dept = (
            await self.db.execute(
                select(Department).where(
                    Department.id == department_id,
                    Department.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if dept is None:
            return []
        marker = f"{dept.path}/" if dept.path else f"{dept.id}/"
        rows = (
            await self.db.execute(
                select(Department.id).where(
                    Department.tenant_id == tenant_id,
                    (Department.path == dept.path) | (Department.path.like(f"{marker}%")),
                )
            )
        ).all()
        return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# 依赖注入：接口权限
# ---------------------------------------------------------------------------
def require_permission(code: str):
    """FastAPI 依赖：校验当前用户是否拥有某个权限。

    `ENFORCE_PERMISSIONS=False` 时放行（迁移期兼容，见模块 docstring）。
    """
    from fastapi import Depends, HTTPException

    from app.db.session import get_db
    from app.dependencies import get_current_user
    from app.models.user import User

    async def _checker(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        if not settings.ENFORCE_PERMISSIONS:
            return current_user

        svc = PermissionService(db)
        roles = await svc.get_user_roles(str(current_user.id))
        if not roles:
            raise HTTPException(
                status_code=403,
                detail={"detail": f"用户没有任何角色，无法执行 {code}", "code": "NO_ROLE"},
            )
        if not await svc.user_has_permission(str(current_user.id), code):
            raise HTTPException(
                status_code=403,
                detail={"detail": f"缺少权限 {code}", "code": "PERMISSION_DENIED"},
            )
        return current_user

    return _checker
