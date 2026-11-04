import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.db.session import engine
from app.models.base import Base
from app.api.v1 import auth, users, products, seckill, orders, payment, prompt, chat, agent, purchase, sales
from app.admin import products_admin, seckill_admin, orders_admin
from app.middleware.log_middleware import LogMiddleware
from app.middleware.prometheus import setup_metrics
from app.websocket.order_notify import router as websocket_router
from app.tasks.scheduler import start_scheduler, shutdown_scheduler
from contextlib import asynccontextmanager

# 基于 main.py 文件位置计算前端路径，兼容从项目根目录或 app/ 目录启动
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
STATIC_DIR = os.path.join(FRONTEND_DIR, "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时创建表（生产环境建议用 alembic）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # 启动后台任务（Redis Stream 消费者）
    import asyncio
    from app.services.seckill_service import SeckillService
    asyncio.create_task(SeckillService.consume_orders())
    # 启动定时任务
    start_scheduler()
    yield
    # 关闭时清理
    shutdown_scheduler()
    await engine.dispose()


app = FastAPI(title=settings.APP_NAME, debug=settings.DEBUG, lifespan=lifespan)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def read_root():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


# CORS 中间件 - 允许所有来源跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 中间件
app.add_middleware(LogMiddleware)

# 设置 Prometheus 监控指标
try:
    setup_metrics(app)
except Exception as e:
    import logging
    logging.warning(f"Prometheus 监控初始化失败（如未安装依赖可忽略）: {e}")

# API v1 路由
app.include_router(auth.router, prefix="/api/v1", tags=["auth"])
app.include_router(users.router, prefix="/api/v1", tags=["users"])
app.include_router(products.router, prefix="/api/v1", tags=["products"])
app.include_router(seckill.router, prefix="/api/v1", tags=["seckill"])
app.include_router(orders.router, prefix="/api/v1", tags=["orders"])
app.include_router(payment.router, prefix="/api/v1", tags=["payment"])
app.include_router(prompt.router, prefix="/api/v1", tags=["prompt"])
app.include_router(chat.router, prefix="/api/v1", tags=["chat"])
app.include_router(agent.router, prefix="/api/v1", tags=["agent"])
app.include_router(purchase.router, prefix="/api/v1", tags=["purchase"])
app.include_router(sales.router, prefix="/api/v1", tags=["sales"])

# 管理端路由
app.include_router(products_admin.router, prefix="/admin/api", tags=["admin"])
app.include_router(seckill_admin.router, prefix="/admin/api", tags=["admin"])
app.include_router(orders_admin.router, prefix="/admin/api", tags=["admin"])

# WebSocket 路由
app.include_router(websocket_router, prefix="", tags=["websocket"])


@app.get("/health")
async def health():
    return {"status": "ok"}



# ---------------------------------------------------------------------------
# 业务异常 → 统一 HTTP 响应
# 库存/采购服务抛的是自定义异常，这里统一映射，避免 500
# ---------------------------------------------------------------------------
from fastapi import Request
from fastapi.responses import JSONResponse


def _error_response(exc):
    return JSONResponse(
        status_code=getattr(exc, "http_status", 400),
        content={
            "detail": getattr(exc, "message", str(exc)),
            "code": getattr(exc, "code", "ERROR"),
        },
    )


from app.services.inventory_service import InventoryError
from app.services.purchase_service import PurchaseError


@app.exception_handler(InventoryError)
async def _inventory_error_handler(request: Request, exc):
    return _error_response(exc)


@app.exception_handler(PurchaseError)
async def _purchase_error_handler(request: Request, exc):
    return _error_response(exc)


from app.services.sales_service import SalesError


@app.exception_handler(SalesError)
async def _sales_error_handler(request: Request, exc):
    return _error_response(exc)