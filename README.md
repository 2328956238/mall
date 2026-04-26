# Flash Sales Platform（高并发秒杀平台）

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104+-green.svg)](https://fastapi.tiangolo.com)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一个基于 FastAPI 的高并发秒杀平台，支持商品秒杀、订单管理、实时通知和监控。系统设计目标为支持万级 QPS 的秒杀场景，保证数据一致性和系统可用性。

## ✨ 特性

- **高并发设计**：Redis 预减库存 + 队列缓冲 + 限流防止超卖
- **异步全链路**：FastAPI + SQLAlchemy Async + Redis Async
- **可观测性**：Prometheus 监控 + 结构化日志 + 分布式追踪（OpenTelemetry）
- **模块化架构**：清晰的分层设计（API → Service → Model），易于扩展
- **实时通知**：WebSocket 实时推送订单状态更新
- **AI 集成**：提示增强服务（支持 OpenAI 和国产模型）

## 🛠️ 技术栈

| 类别 | 技术 | 用途 |
|------|------|------|
| 后端框架 | [FastAPI](https://fastapi.tiangolo.com) | 高性能异步 Web 框架 |
| 数据库 | [PostgreSQL 15](https://www.postgresql.org) + [SQLAlchemy Async](https://docs.sqlalchemy.org) | 关系型数据存储 |
| 缓存/队列 | [Redis 7](https://redis.io) | 库存预减、限流、消息队列 |
| 消息队列 | [Apache Kafka](https://kafka.apache.org) | 订单异步处理 |
| 任务调度 | [APScheduler](https://apscheduler.readthedocs.io) | 定时任务管理 |
| 认证授权 | JWT + OAuth2 | 用户认证与 RBAC 权限控制 |
| 监控 | [Prometheus](https://prometheus.io) + [Grafana](https://grafana.com) | 系统监控与可视化 |
| 容器化 | [Docker](https://docker.com) + Docker Compose | 一键部署 |

## 📁 项目结构

```
flash-sales-platform/
├── app/                        # 主应用
│   ├── api/v1/                 # API 路由层
│   ├── schemas/                # Pydantic 请求/响应模型
│   ├── models/                 # SQLAlchemy ORM 模型
│   ├── services/               # 业务逻辑层
│   ├── db/                     # 数据库连接（PostgreSQL + Redis）
│   ├── middleware/             # 中间件（日志、监控、追踪）
│   ├── tasks/                  # 定时任务
│   ├── admin/                  # 管理后台 API
│   ├── utils/                  # 工具函数（JWT、缓存、限流）
│   ├── websocket/              # WebSocket 实时通知
│   └── frontend/               # 前端静态页面
├── docs/                       # 文档
│   ├── API_TEST_DOCUMENTATION.md
│   ├── PROJECT_DOCUMENTATION.md
│   └── TECHNICAL_DOCUMENTATION.md
├── scripts/                    # 运维脚本
│   ├── init_db.sh              # 数据库初始化
│   └── backup_db.sh            # 数据库备份
├── .env.example                # 环境变量模板
├── docker-compose.yml          # Docker Compose 配置
└── requirements.txt            # Python 依赖
```

## 🚀 快速开始

### 前提条件

- Python 3.11+
- Docker & Docker Compose（可选，推荐）
- PostgreSQL 15（手动运行时需要）
- Redis 7（手动运行时需要）

### 使用 Docker Compose（推荐）

```bash
# 克隆项目
git clone https://github.com/yourusername/flash-sales-platform.git
cd flash-sales-platform

# 配置环境变量
cp .env.example .env
# 编辑 .env 文件，填写必要的配置项

# 启动所有服务
docker compose -f app/docker-compose.yml up -d

# 访问 API 文档
open http://localhost:8000/docs
```

### 手动运行（开发）

```bash
# 1. 创建虚拟环境
python -m venv .venv
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动数据库服务（使用 Docker）
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:15
docker run -d -p 6379:6379 redis:7-alpine

# 4. 初始化数据库
bash scripts/init_db.sh

# 5. 插入测试数据
python -c "import asyncio; from seed_data import seed; asyncio.run(seed())"

# 6. 启动 FastAPI 服务
cd app && uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 访问服务

| 服务 | 地址 |
|------|------|
| API 文档 (Swagger) | http://localhost:8000/docs |
| API 文档 (ReDoc) | http://localhost:8000/redoc |
| 健康检查 | http://localhost:8000/health |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 |

## 📋 API 概览

| 方法 | 路径 | 功能 | 认证 |
|------|------|------|------|
| POST | `/api/v1/auth/token` | 登录获取令牌 | ❌ |
| GET | `/api/v1/users/me` | 获取当前用户 | ✅ |
| GET | `/api/v1/products/` | 商品列表 | ❌ |
| POST | `/api/v1/products/` | 创建商品 | ✅ |
| GET | `/api/v1/orders/` | 订单列表 | ✅ |
| POST | `/api/v1/seckill/{id}` | 参与秒杀 | ✅ |
| POST | `/api/v1/prompt/enhance` | AI 增强提示 | ❌ |

完整 API 文档请参考 [API 测试文档](docs/API_TEST_DOCUMENTATION.md)。

## 🏗️ 核心设计

### 秒杀流程

```
用户请求 → 限流检查(Redis令牌桶) → Redis预减库存 → Kafka消息队列 → 异步创建订单 → WebSocket通知
```

### 高并发保障

1. **库存预减**：秒杀开始前将库存加载到 Redis，使用 `DECR` 原子操作，防止超卖
2. **队列缓冲**：用户请求进入消息队列，后台消费者异步处理，削峰填谷
3. **多层限流**：API 级别 + 用户级别限流（令牌桶算法）
4. **缓存策略**：热点数据缓存 5 分钟，缓存穿透/击穿/雪崩三重防护

## 📊 监控与可观测性

- **Prometheus 指标**：HTTP 请求计数/延迟/错误率、数据库连接池、Redis 命中率
- **结构化日志**：JSON 格式，支持 ELK 集成
- **分布式追踪**：OpenTelemetry 集成，Trace ID 传播

## 🤝 贡献

欢迎贡献代码、提交 Issue 或提出改进建议！

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/amazing-feature`)
3. 提交更改 (`git commit -m 'Add amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 创建 Pull Request

## 📄 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](LICENSE) 文件。

---

*更多技术细节请参考 [技术文档](docs/TECHNICAL_DOCUMENTATION.md)。*# mall
