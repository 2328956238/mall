"""审计写入：把一次写操作记到 `operation_logs`。

单独一个模块而不是塞进中间件，是为了能被业务代码直接复用
（例如服务内部记录"谁审核了这张单"）。

租户与用户的来源：
- 优先解析请求头里的 JWT（审计要在业务提交之后跑，此时拿不到依赖注入的 User）
- 解析失败就记成匿名，仍然留痕
"""

import logging
import time
import uuid

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.operation_log import OperationLog
from app.utils.security import decode_access_token

logger = logging.getLogger(__name__)


def _parse_actor(authorization: str) -> tuple[str | None, uuid.UUID | None]:
    if not authorization.lower().startswith("bearer "):
        return None, None
    token = authorization[7:].strip()
    payload = decode_access_token(token)
    if not payload:
        return None, None
    sub = payload.get("sub")
    try:
        return sub, uuid.UUID(sub) if sub else None
    except (ValueError, TypeError):
        return sub, None


async def write_operation_log(
    *,
    method: str,
    path: str,
    status_code: int,
    client_ip: str = "",
    authorization: str = "",
    action: str | None = None,
    resource: str | None = None,
    resource_id: str | None = None,
    elapsed_ms: int | None = None,
) -> None:
    user_id_str, user_uuid = _parse_actor(authorization)

    tenant_id = 1
    if user_uuid is not None:
        try:
            async with AsyncSessionLocal() as session:
                from app.models.user import User

                row = (
                    await session.execute(
                        select(User.tenant_id).where(User.id == user_uuid)
                    )
                ).scalar_one_or_none()
                if row is not None:
                    tenant_id = int(row)
        except Exception:
            logger.debug("审计解析租户失败，回退默认租户", exc_info=True)

    # 路径形如 /api/v1/purchase/orders/12/approve
    segments = [s for s in path.split("/") if s]
    if resource is None:
        resource = segments[2] if len(segments) > 2 else "unknown"
    if action is None:
        action = segments[-1] if segments else "unknown"

    async with AsyncSessionLocal() as session:
        session.add(
            OperationLog(
                tenant_id=tenant_id,
                user_id=user_uuid,
                action=action[:100],
                resource=resource[:100],
                resource_id=resource_id,
                request_method=method[:10],
                request_path=path[:500],
                request_ip=(client_ip or "")[:45],
                response_status=status_code,
                execution_time_ms=elapsed_ms,
            )
        )
        await session.commit()
