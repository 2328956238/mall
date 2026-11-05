"""SaaS 权限域模型（第四阶段）。

五张表 + 两处扩展：

    tenants（第一阶段已建）
      ├─< departments （自关联树）
      ├─< roles >─< role_permissions >─ permissions
      └─< user_roles >─ users（users 加 department_id）

设计约定：

1. **权限是全局字典**（`permissions.code` 全局唯一，如 `purchase.order.approve`），
   角色是租户内的（`(tenant_id, code)` 唯一）。这样平台可以统一维护权限清单，
   各租户自己组合角色。

2. **数据权限挂在角色上**（`data_scope`）：
   - `all`        看本租户全部数据
   - `department` 只看本部门（含子部门）
   - `self`       只看自己创建的

3. **操作审计复用已有的 `operation_logs` 表**（第一阶段已补 `tenant_id`）。

4. 迁移期兼容：`ENFORCE_PERMISSIONS=False` 时校验放行，
   避免存量用户（没有任何角色）突然全部 403。详见 `permission_service.py`。
"""

import enum
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
class PermissionType(str, enum.Enum):
    MENU = "menu"    # 菜单可见性
    API = "api"      # 接口调用
    DATA = "data"    # 数据范围（配合 role.data_scope）


class DataScope(str, enum.Enum):
    ALL = "all"                # 本租户全部
    DEPARTMENT = "department"  # 本部门及子部门
    SELF = "self"              # 仅本人


def _enum(py_enum, name: str) -> Enum:
    return Enum(py_enum, name=name, values_callable=lambda e: [m.value for m in e])


# ---------------------------------------------------------------------------
# 部门（自关联树）
# ---------------------------------------------------------------------------
class Department(Base, TimestampMixin):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, default=1, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("departments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 物化路径 "1/4/9"，便于一次查询拿到整棵子树（避免递归 CTE）
    path: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_departments_tenant_code"),
        Index("ix_departments_path", "path"),
    )


# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------
class Role(Base, TimestampMixin):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, default=1, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    data_scope: Mapped[DataScope] = mapped_column(
        _enum(DataScope, "data_scope"), nullable=False, default=DataScope.SELF
    )
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    permissions: Mapped[list["RolePermission"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_roles_tenant_code"),
    )


# ---------------------------------------------------------------------------
# 权限字典（全局）
# ---------------------------------------------------------------------------
class Permission(Base, TimestampMixin):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[PermissionType] = mapped_column(
        _enum(PermissionType, "permission_type"), nullable=False, default=PermissionType.API
    )
    resource: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        Index("ix_permissions_resource", "resource", "action"),
    )


# ---------------------------------------------------------------------------
# 关联表
# ---------------------------------------------------------------------------
class UserRole(Base, TimestampMixin):
    __tablename__ = "user_roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, default=1, index=True
    )
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True
    )

    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_roles"),
    )


class RolePermission(Base, TimestampMixin):
    __tablename__ = "role_permissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    permission_id: Mapped[int] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False, index=True
    )

    role: Mapped["Role"] = relationship(back_populates="permissions")

    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permissions"),
    )
