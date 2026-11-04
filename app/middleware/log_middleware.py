"""请求日志中间件（含写操作审计）。

第四阶段改造点：
1. 原来**所有**请求都写 `operation_logs`，读请求量太大且没人看 —— 现在只记写操作；
2. 原来没有 `tenant_id` / `user_id`，审计查不出"谁在哪个租户干的" —— 现在从 JWT 解析；
3. 记录失败不影响业务（审计不能把主流程拖挂）。
"""

import logging
import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class LogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        response = await call_next(request)
        elapsed_ms = int((time.time() - start_time) * 1000)

        # 读请求不落库：量大且无审计价值
        if request.method not in _WRITE_METHODS:
            return response

        try:
            from app.core_audit import write_operation_log

            await write_operation_log(
                method=request.method,
                path=str(request.url.path),
                status_code=response.status_code,
                client_ip=request.client.host if request.client else "",
                authorization=request.headers.get("authorization", ""),
                elapsed_ms=elapsed_ms,
            )
        except Exception:
            logger.exception("写审计日志失败 path=%s", request.url.path)

        return response
