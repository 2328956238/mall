# Flash Sales Platform 完整项目文档

## 1. 项目概述

这是一个基于 **FastAPI** 的高并发秒杀平台，支持商品秒杀、订单管理、支付、实时通知和监控。

### 技术栈
- **后端框架**: FastAPI (Python 3.11+)
- **数据库**: PostgreSQL (SQLAlchemy Async)
- **缓存/队列**: Redis (库存管理、限流)
- **消息队列**: Kafka (订单异步处理)
- **任务调度**: APScheduler
- **认证授权**: JWT (JSON Web Tokens)
- **监控**: Prometheus + Grafana
- **WebSocket**: 实时订单通知
- **前端**: Streamlit

---

## 2. 项目结构

```
/home/xiao/Restapi/
├── app/                          # 主应用目录
│   ├── main.py                   # FastAPI 应用入口
│   ├── config.py                 # 配置管理
│   ├── dependencies.py           # 依赖注入
│   ├── exceptions.py             # 自定义异常
│   │
│   ├── api/v1/                   # API 路由层
│   │   ├── auth.py              # 认证 (登录/Token)
│   │   ├── users.py             # 用户管理
│   │   ├── products.py          # 商品管理
│   │   ├── orders.py            # 订单管理
│   │   ├── payment.py           # 支付 (已修复)
│   │   ├── seckill.py           # 秒杀接口
│   │   ├── prompt.py            # AI 提示增强
│   │   └── rbac.py              # 权限管理
│   │
│   ├── schemas/                  # Pydantic 模型 (请求/响应验证)
│   │   ├── user.py
│   │   ├── product.py
│   │   ├── order.py
│   │   ├── payment.py
│   │   ├── seckill.py
│   │   ├── prompt.py
│   │   └── role.py
│   │
│   ├── models/                   # SQLAlchemy ORM 模型 (数据库表)
│   │   ├── base.py              # 基础模型
│   │   ├── user.py
│   │   ├── product.py
│   │   ├── order.py
│   │   ├── payment.py
│   │   ├── seckill_product.py
│   │   ├── role.py
│   │   ├── permission.py
│   │   └── operation_log.py
│   │
│   ├── services/                 # 业务逻辑层
│   │   ├── user_service.py
│   │   ├── product_service.py
│   │   ├── order_service.py
│   │   ├── seckill_service.py   # 秒杀核心逻辑
│   │   ├── payment_service.py
│   │   ├── rbac_service.py
│   │   ├── prompt_service.py    # AI 提示增强
│   │   └── kafka_producer.py
│   │
│   ├── db/                       # 数据库连接
│   │   ├── session.py           # AsyncSession 工厂
│   │   ├── redis_client.py      # Redis 客户端
│   │   └── redis_stream.py      # Redis Stream
│   │
│   ├── middleware/               # 中间件
│   │   ├── log_middleware.py    # 日志中间件
│   │   ├── prometheus.py        # 监控中间件
│   │   └── trace.py             # 分布式追踪
│   │
│   ├── tasks/                    # 定时任务
│   │   ├── scheduler.py         # APScheduler 调度器
│   │   └── jobs.py              # 定时任务定义
│   │
│   ├── admin/                    # 管理后台 API
│   │   ├── products_admin.py
│   │   ├── seckill_admin.py
│   │   └── orders_admin.py
│   │
│   ├── utils/                    # 工具函数
│   │   ├── security.py          # JWT/密码加密
│   │   ├── logger.py            # 日志工具
│   │   └── limiter.py           # 限流工具
│   │
│   ├── websocket/                # WebSocket 处理
│   │   └── order_notify.py
│   │
│   └── kafka/                    # Kafka 配置
│       └── init-topics.sh
│
├── frontend/                      # Streamlit 前端
│   ├── app.py                    # 主应用 (已修复)
│   └── start.sh                  # 启动脚本
│
├── scripts/                       # Shell 脚本
│   ├── init_db.sh               # 初始化数据库
│   └── backup_db.sh             # 备份数据库
│
├── .github/workflows/            # CI/CD
│   └── ci-cd.yml
│
├── requirements.txt               # Python 依赖
├── README.md                      # 项目说明
└── docker-compose.yml             # Docker 配置
```

---

## 3. 模块详解

### 3.1 main.py - 应用入口

```python
# 主要功能：
# 1. 创建 FastAPI 应用
# 2. 注册中间件 (LogMiddleware)
# 3. 包含所有 API 路由
# 4. 启动时创建数据库表
# 5. 启动定时任务调度器
# 6. 启动 Kafka 消费者
```

**启动流程：**
```python
1. lifespan() 异步上下文管理器
   ├─ 启动时: 创建数据库表 + 启动消费者 + 启动调度器
   └─ 关闭时: 关闭调度器 + 断开数据库连接

2. 注册路由
   ├─ /api/v1/* (用户 API)
   ├─ /admin/api/* (管理后台 API)
   └─ /ws/* (WebSocket)
```

---

### 3.2 API 层 (app/api/v1/)

| 文件 | 路由前缀 | 功能 | 关键端点 |
|------|----------|------|----------|
| `auth.py` | `/auth` | 登录认证 | `POST /token` 获取 JWT |
| `users.py` | `/users` | 用户 CRUD | `GET /me` 获取当前用户 |
| `products.py` | `/products` | 商品管理 | `GET /` 列表, `POST /` 创建 |
| `orders.py` | `/orders` | 订单管理 | `GET /` 列表 |
| `payment.py` | `/payments` | 支付 | `POST /` 创建, `GET /methods` 支付方式 |
| `seckill.py` | `/seckill` | 秒杀 | `POST /{id}` 参与秒杀 |
| `prompt.py` | `/prompt` | AI 增强 | `POST /enhance` 增强提示 |
| `rbac.py` | `/rbac` | 权限 | `GET /roles` 角色列表 |

---

### 3.3 服务层 (app/services/)

#### 3.3.1 seckill_service.py - 秒杀核心逻辑

```python
class SeckillService:
    @staticmethod
    async def seckill(product_id: str, user_id: str):
        """秒杀流程："""
        1. 检查产品是否存在
        2. Redis 预减库存 (原子操作)
        3. 限流检查 (Redis 计数器)
        4. 发送消息到 Kafka
        5. 返回排队中
```

#### 3.3.2 rbac_service.py - 权限服务

```python
class RBACService:
    @staticmethod
    async def check_menu_permission(db, user_id, menu_name, permission):
        """检查用户是否有菜单权限"""
        
    @staticmethod
    async def create_role(db, role_data):
        """创建角色"""
```

---

### 3.4 数据库层 (app/db/)

```python
# session.py
engine = create_async_engine(settings.DATABASE_URL, echo=settings.DEBUG)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
```

**注意**: 项目使用 **AsyncSession**（异步），所有数据库操作必须使用 async/await。

---

### 3.5 中间件 (app/middleware/)

| 中间件 | 功能 |
|--------|------|
| `log_middleware.py` | 请求/响应日志记录 |
| `prometheus.py` | Prometheus 监控指标 |
| `trace.py` | 分布式追踪 (OpenTelemetry) |

---

## 4. 启动流程

### 4.1 使用 Docker Compose (推荐)

```bash
cd /home/xiao/Restapi/app
docker-compose up -d
```

服务包括：
- PostgreSQL (端口 5432)
- Redis (端口 6379)
- Kafka (端口 9092)
- FastAPI (端口 8000)
- Streamlit (端口 8501)

### 4.2 手动启动

```bash
# 1. 创建虚拟环境
python -m venv .venv
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动数据库 (Docker)
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:15
docker run -d -p 6379:6379 redis:7-alpine

# 4. 初始化数据库
bash scripts/init_db.sh

# 5. 启动 FastAPI
cd app
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# 6. 启动 Streamlit (新终端)
cd ../frontend
streamlit run app.py
```

---

## 5. 脚本说明

### 5.1 scripts/init_db.sh

```bash
#!/bin/bash
# 初始化数据库 - 创建所有表
docker compose exec api python -c "
    from app.db.session import engine
    from app.models.base import Base
    import asyncio
    asyncio.run(engine.run_sync(Base.metadata.create_all))
"
echo "Database initialized"
```

### 5.2 scripts/backup_db.sh

```bash
#!/bin/bash
# 备份 PostgreSQL 数据库
docker compose exec postgres pg_dump -U postgres flashsales > backup_$(date +%Y%m%d).sql
```

---

## 6. 修复记录

### 6.1 Streamlit 前端 DuplicateElementId 错误

**文件**: `/home/xiao/frontend/app.py`

**问题**: 多个 `text_input` 使用相同 label 但没有 unique key

**解决**: 添加 `key` 参数
```python
# 修复前
name = st.text_input("名称")
desc = st.text_input("描述")

# 修复后
name = st.text_input("名称", key="product_name")
desc = st.text_input("描述", key="product_desc")
```

### 6.2 Payment API 500 错误

**文件**: `app/api/v1/payment.py`

**问题 1**: 使用同步查询但会话是 AsyncSession
```python
# 修复前 (错误)
payment = db.query(Payment).filter(Payment.id == payment_id).first()

# 修复后 (正确)
result = await db.execute(select(Payment).where(Payment.id == payment_id))
payment = result.scalar_one_or_none()
```

**问题 2**: 路由顺序冲突 (`/methods` 被 `/{payment_id}` 捕获)
```python
# 修复: 把 /methods 路由放在 /{payment_id} 之前
@router.get("/methods", ...)     # 先定义具体路由
@router.get("/{payment_id}", ...)  # 后定义参数化路由
```

---

## 7. API 端点速查

| 方法 | 路径 | 功能 | 需要认证 |
|------|------|------|----------|
| POST | `/api/v1/auth/token` | 登录获取 Token | ❌ |
| GET | `/api/v1/users/me` | 获取当前用户 | ✅ |
| GET | `/api/v1/products/` | 商品列表 | ❌ |
| POST | `/api/v1/products/` | 创建商品 | ✅ |
| GET | `/api/v1/orders/` | 订单列表 | ✅ |
| GET | `/api/v1/payments/` | 支付记录 | ✅ |
| GET | `/api/v1/payments/methods` | 支付方式 | ✅ |
| POST | `/api/v1/seckill/{id}` | 参与秒杀 | ✅ |
| POST | `/api/v1/prompt/enhance` | AI 增强提示 | ❌ |
| GET | `/api/v1/rbac/roles` | 角色列表 | ✅ |

---

## 8. WebSocket 实时通知

```javascript
// 连接方式
const ws = new WebSocket('ws://localhost:8000/ws/orders');

// 接收消息
ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    console.log('订单状态更新:', data);
};
```

---

## 9. 测试

### 9.1 API 测试

```bash
# 测试 health 端点
curl http://localhost:8000/health

# 测试提示增强
curl -X POST "http://localhost:8000/api/v1/prompt/enhance" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "测试", "style": "professional", "language": "zh"}'
```

### 9.2 秒杀测试

```bash
# 1. 登录获取 token
curl -X POST "http://localhost:8000/api/v1/auth/token" \
  -d "username=admin@example.com&password=admin12345678"

# 2. 参与秒杀
curl -X POST "http://localhost:8000/api/v1/seckill/1" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## 10. 监控

- **Prometheus**: http://localhost:9090
- **Grafana**: http://localhost:3000 (默认 admin/admin)
- **API 文档**: http://localhost:8000/docs
- **Streamlit 前端**: http://localhost:8501

---

## 11. 业务流程：从商品创建到支付成功

### 11.1 完整流程图

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  1. 创建商品    │────▶│  2. 创建订单    │────▶│  3. 支付成功    │
│  Products API   │     │  Orders API     │     │  Payments API   │
└─────────────────┘     └─────────────────┘     └─────────────────┘
        │                       │                       │
        ▼                       ▼                       ▼
   Product 模型             Order 模型             Payment 模型
   (product..py)           (order.py)              (payment.py)
```

### 11.2 步骤详解

#### 步骤1: 创建商品 (Product)

**API 端点**: `POST /api/v1/products/`

**请求示例**:
```bash
curl -X POST "http://localhost:8000/api/v1/products/" \
  -H "Content-Type: application/json" \
  -d '{"name": "iPhone 15", "description": "最新款苹果手机", "price": 7999.99, "stock": 100, "is_on_sale": true}'
```

**代码流程**:

```
products.py (API层)
    │
    ├── @router.post("/")  ──→ create_product()
    │
    ▼
product_service.py (服务层)
    │
    ├── ProductService.create_product()
    │   ├── 创建 Product 对象
    │   ├── db.add(product)
    │   ├── await db.commit()
    │   └── await db.refresh(product)
    │
    ▼
product.py (模型层)
    │
    └── Product (SQLAlchemy Model)
        ├── id: int (主键)
        ├── name: str
        ├── description: str
        ├── price: float
        ├── stock: int
        └── is_on_sale: bool
```

**相关文件**:
- `app/api/v1/products.py` - API 路由
- `app/services/product_service.py` - 业务逻辑
- `app/models/product.py` - 数据模型
- `app/schemas/product.py` - 请求/响应验证

---

#### 步骤2: 创建订单 (Order)

**创建方式**:
1. **普通订单**: 通过订单 API 直接创建
2. **秒杀订单**: 通过秒杀系统创建 (高并发流程)

**秒杀订单流程**:
```
seckill.py (API层)
    │
    ├── POST /seckill/{product_id}
    │
    ▼
seckill_service.py (服务层)
    │
    ├── 1. token_bucket() - 用户限流检查
    │   └── 使用 Redis Lua 脚本实现令牌桶
    │
    ├── 2. redis.decr() - 预减库存
    │   └── 原子操作，防止超卖
    │
    ├── 3. add_to_stream() - 发送消息到 Redis Stream
    │   └── 异步处理，削峰填谷
    │
    ▼
OrderService.create_order_from_seckill() (消费者)
    │
    ├── 1. 查询秒杀商品信息
    ├── 2. 扣减数据库库存
    │   └── product.stock -= 1
    └── 3. 创建订单
        └── Order(
            user_id=uuid,
            seckill_product_id=id,
            quantity=1,
            total_amount=seckill_price,
            status=PENDING
        )
```

**订单模型** (`app/models/order.py`):
```python
class Order(Base, TimestampMixin):
    id: Mapped[uuid.UUID]           # 订单ID
    user_id: Mapped[uuid.UUID]      # 用户ID
    seckill_product_id: Mapped[int] # 秒杀商品ID (可为空)
    quantity: Mapped[int]           # 数量
    total_amount: Mapped[float]     # 总金额
    status: Mapped[str]             # 订单状态
```

**订单状态** (`app/models/order.py`):
```python
class OrderStatus(str, Enum):
    PENDING = "pending"       # 待支付
    PAID = "paid"            # 已支付
    SHIPPED = "shipped"      # 已发货
    COMPLETED = "completed"  # 已完成
    CANCELLED = "cancelled"  # 已取消
```

**相关文件**:
- `app/api/v1/seckill.py` - 秒杀 API
- `app/api/v1/orders.py` - 订单 API
- `app/services/seckill_service.py` - 秒杀逻辑
- `app/services/order_service.py` - 订单逻辑
- `app/models/order.py` - 订单模型

---

#### 步骤3: 支付成功 (Payment)

**API 端点**: 
- `POST /api/v1/payments/` - 创建支付
- `POST /api/v1/payments/{payment_id}/process` - 处理支付

**前置条件**:
1. 用户需要先创建支付方式: `POST /api/v1/payments/methods`
2. 支付方式包含: 信用卡、银行转账、PayPal、微信、支付宝等

**支付流程**:
```
payments.py (API层)
    │
    ├── POST /payments/methods  ──→ 创建支付方式
    │       └── PaymentMethod 模型
    │
    ├── POST /payments/  ──→ 创建支付
    │       └── Payment 模型 (status="pending")
    │
    ▼
payment.处理.支付_process()
    │
    ├── 1. 查询支付记录
    ├── 2. 创建交易记录 (Transaction)
    │       └── status="success"
    ├── 3. 更新支付状态
    │       └── status="completed"
    └── 4. 更新支付时间
            └── paid_at=datetime.utcnow()
```

**支付模型** (`app/models/payment.py`):
```python
class Payment(Base, TimestampMixin):
    id: uuid.UUID              # 支付ID
    amount: float              # 金额
    currency: str              # 货币 (CNY/USD)
    user_id: uuid.UUID         # 用户ID
    payment_method_id: uuid.UUID  # 支付方式ID
    status: str                # 支付状态
    transaction_id: str        # 交易ID
    description: str           # 描述
    paid_at: datetime          # 支付时间

class PaymentMethod(Base, TimestampMixin):
    id: uuid.UUID              # 支付方式ID
    user_id: uuid.UUID         # 用户ID
    type: str                  # 类型 (credit_card/bank_transfer/paypal/wechat/alipay)
    provider: str              # 提供商
    account_number: str        # 账号
    is_default: bool           # 是否默认
```

**支付状态**:
- `pending` - 待支付
- `completed` - 已完成
- `failed` - 失败
- `refunded` - 已退款

**相关文件**:
- `app/api/v1/payment.py` - 支付 API (已修复)
- `app/schemas/payment.py` - 支付 Schema
- `app/models/payment.py` - 支付模型

---

### 11.3 数据模型关系

```
User (用户)
  ├── 1:N Orders (订单)
  │     └── OrderStatus: PENDING → PAID → SHIPPED → COMPLETED
  │
  ├── 1:N PaymentMethods (支付方式)
  │     └── type: credit_card, bank_transfer, paypal, wechat, alipay
  │
  └── 1:N Payments (支付)
        └── status: pending → completed / failed / refunded

Product (商品)
  ├── 1:N SeckillProducts (秒杀商品)
  │     └── seckill_price < price
  │
  └── stock: 库存 (秒杀时用于扣减)

SeckillProduct (秒杀商品)
  ├── 1:N Orders (秒杀订单)
  │
  └── stock_count: 秒杀库存
```

---

### 11.4 API 调用顺序示例

```bash
# 1. 登录获取 Token
curl -X POST "http://localhost:8000/api/v1/auth/token" \
  -d "username=admin@example.com&password=admin12345678"
# 返回: {"access_token": "...", "token_type": "bearer"}
TOKEN="your_access_token"

# 2. 创建商品
curl -X POST "http://localhost:8000/api/v1/products/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "iPhone 15", "description": "最新款", "price": 7999.99, "stock": 100}'

# 3. 参与秒杀 (创建订单)
curl -X POST "http://localhost:8000/api/v1/seckill/1" \
  -H "Authorization: Bearer $TOKEN"

# 4. 创建支付方式
curl -X POST "http://localhost:8000/api/v1/payments/methods" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type": "credit_card", "provider": "visa", "account_number": "4111111111111111", "is_default": true}'

# 5. 创建支付 (待支付)
curl -X POST "http://localhost:8000/api/v1/payments/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"amount": 7999.99, "currency": "CNY", "user_id": "user-uuid", "description": "iPhone 15"}'

# 6. 处理支付 (完成支付)
curl -X POST "http://localhost:8000/api/v1/payments/payment-uuid/process" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"transaction_id": "tx-123", "amount": 7999.99, "currency": "CNY", "status": "success"}'
```

---

### 11.5 关键业务逻辑

| 功能 | 实现方式 | 位置 |
|------|----------|------|
| 限流 | Redis 令牌桶 (Lua 脚本) | `app/utils/limiter.py` |
| 库存预减 | Redis DECR 原子操作 | `app/services/seckill_service.py` |
| 异步下单 | Redis Stream | `app/db/redis_stream.py` |
| 数据库事务 | AsyncSession + await commit | 各 Service |
| 支付状态机 | Enum 状态管理 | `app/models/payment.py` |
| WebSocket 通知 | FastAPI WebSocket | `app/websocket/order_notify.py` |
