from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from app.db.session import AsyncSessionLocal
from app.models.operation_log import OperationLog
import time
from uuid import UUID

class LogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        response = await call_next(request)
        process_time = int((time.time() - start_time) * 1000)

        # 异步记录日志（避免阻塞）
        async with AsyncSessionLocal() as db:
            user_id = None
            if hasattr(request.state, "user_id"):
                user_id = request.state.user_id
            log = OperationLog(
                user_id=user_id,
                action=request.method,
                resource=request.url.path,
                request_method=request.method,
                request_path=request.url.path,
                request_ip=request.client.host,
                response_status=response.status_code,
                execution_time_ms=process_time
            )
            db.add(log)
            await db.commit()
        return response