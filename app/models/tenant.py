"""租户模型。

第一阶段只建最小实现：租户本身的身份 + 状态。
部门 / 角色 / 权限 / 数据权限在第四阶段补齐 —— 但 `tenant_id` 从这一阶段起就进表，
避免以后对每张业务表做"加列 + 回填 + 改查询 + 改测试"的二次手术。
"""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class TenantStatus(str):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 用字符串而不是 Enum：租户状态后续可能扩展，改字符串不需要迁移 ENUM 类型
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Tenant {self.code}>"
