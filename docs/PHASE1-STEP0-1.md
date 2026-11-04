# 第一阶段 · 第 0 步与第 1 步交付说明

> 执行范围：只做「回归测试基线」和「Alembic baseline」，**未开发采购 / 销售 / 租户模块**。
> 代码改动：`app/` 目录 **0 处源码改动**，只新增 `tests/`、`alembic/`、`scripts/`、`pytest.ini`、`docs/MIGRATION.md`。

---

## 一、修改文件清单

### 新增

| 文件 | 作用 |
|---|---|
| `pytest.ini` | pytest 配置（asyncio 模式、测试路径） |
| `tests/conftest.py` | 测试基础设施：嵌入式 PostgreSQL 16 + fakeredis + 每用例独立 engine |
| `tests/test_regression_products.py` | 商品 CRUD 回归（8 项） |
| `tests/test_regression_auth.py` | 注册/登录/鉴权回归（6 项） |
| `tests/test_regression_orders.py` | 订单查询与越权回归（5 项） |
| `tests/test_regression_seckill.py` | 秒杀主链路回归（8 项） |
| `tests/test_seckill_concurrency.py` | 并发不超卖（2 项） |
| `alembic.ini` | Alembic 配置（连接串不落盘，只读 `DATABASE_URL`） |
| `alembic/env.py` | 异步迁移环境，`target_metadata = Base.metadata` |
| `alembic/versions/558c329ee208_baseline_current_schema.py` | baseline 迁移 |
| `scripts/verify_schema.py` | **结构核对闸门**：真实库 vs 模型，有差异即退出码 1 |
| `docs/MIGRATION.md` | 迁移手册：备份、核对、stamp、回滚、后续约定 |

### 修改

| 文件 | 改动 |
|---|---|
| `.gitignore` | 追加 `.pgdata-*/`、`.pgtmp/`、`.pg_uri.txt`、`docs/TEST_RESULTS.txt` |

> `app/` 下 65 个源文件**一个都没改**（只有 `__pycache__` 因运行产生）。

---

## 二、数据库迁移记录

| 项 | 内容 |
|---|---|
| 版本号 | `558c329ee208` |
| 描述 | baseline: current schema（users / products / seckill_products / orders / operation_logs） |
| 父版本 | 无（首个版本） |
| 生成方式 | `alembic revision --autogenerate`，基准为 `app/models` 的 5 个模型 |
| 是否含数据迁移 | 否（纯 DDL，与现有 `create_all` 结构等价） |

### 实测流程（全部真跑，输出见下）

```
① 空库            alembic upgrade head        → exit 0
② 结构核对        verify_schema.py            → 结构一致 [OK]，5 张表
③ 回滚            alembic downgrade base      → exit 0
④ 回滚后核对      verify_schema.py            → 库为空，exit 1（符合预期）
⑤ 再次升级        alembic upgrade head        → exit 0（幂等验证）
⑥ 再次核对        verify_schema.py            → 结构一致 [OK]
⑦ 当前版本        alembic current             → 558c329ee208 (head)
```

### 发现并修复的真实 bug

**downgrade 不删 PostgreSQL 枚举类型**，导致第二次 `upgrade` 直接失败：

```
[SQL: CREATE TYPE orderstatus AS ENUM ('PENDING', 'PAID', 'SHIPPED', 'COMPLETED', 'CANCELLED')]
sqlalchemy.exc.DuplicateObject: type "orderstatus" already exists
```

修复：在 baseline 的 `downgrade()` 末尾补

```python
sa.Enum(name='orderstatus').drop(op.get_bind(), checkfirst=True)
```

修复后 ⑤⑥ 通过。这个 bug 如果不测「回滚→再升级」是发现不了的。

### 现有库接入（模拟生产）

用 `create_all` 建库模拟现有生产库，然后：

```
verify_schema.py  → 结构一致 [OK]
alembic stamp head → exit 0
alembic current    → 558c329ee208 (head)
verify_schema.py  → 结构一致 [OK]
```

### 闸门有效性验证（证明 stamp 不是盲的）

对同一个库人为改结构（删列 + 加索引）后：

```
发现 2 处结构差异 [FAIL]，禁止直接 stamp：
  - [索引差异] remove_index ix_products_name
  - [缺失列] users.full_name
exit=1
```

说明 `verify_schema.py` 确实能挡住「结构漂移后硬 stamp」。

---

## 三、测试用例与真实结果

``` 
platform win32 -- Python 3.12.10, pytest-8.3.4
collected 29 items
tests\test_regression_auth.py ......      [ 20%]
tests\test_regression_orders.py .....     [ 37%]
tests\test_regression_products.py ........[ 65%]
tests\test_regression_seckill.py ........ [ 93%]
tests\test_seckill_concurrency.py ..      [100%]
============================= 29 passed in 24.35s =============================
```

| 文件 | 数量 | 覆盖点 |
|---|---|---|
| `test_regression_products.py` | 8 | 创建/详情/404/列表/更新/删除/参数校验（price>0、stock≥0） |
| `test_regression_auth.py` | 6 | 注册、重复注册 400、登录、错误密码 401、无 token 401、`/users/me` |
| `test_regression_orders.py` | 5 | 我的订单列表、订单详情、**越权 403**、404、未登录 401 |
| `test_regression_seckill.py` | 8 | 秒杀成功（两处库存同步扣减 + 订单金额）、限流 429、售罄 410、未开始/已结束 410、活动不存在 410、未登录 401、活动列表 |
| `test_seckill_concurrency.py` | 2 | **10 并发抢 5 件 → 恰好 5 成功、库存为 0、订单 5 笔、无负数**；同一用户并发只成功 1 次 |

### 测试基础设施的三个关键决定

1. **跑在真实 PostgreSQL 16 上**（pgserver 嵌入式实例，端口随机）——SQLite 的行锁语义不同，测不出超卖。
2. **Redis 用 fakeredis（带 Lua）**——`token_bucket` 用 EVAL 脚本，不带 Lua 的 fakeredis 会直接报错。
3. **每个用例独立 engine + NullPool，并强制 Selector 事件循环**——asyncpg 连接不能跨事件循环复用；
   Windows 默认 Proactor 循环在连接释放时会抛 `NoneType has no attribute send`。

### 踩到并修好的环境问题（记录备查）

| 问题 | 处理 |
|---|---|
| `fastapi 0.104.1` + `pydantic 2.13` 不兼容（`FieldInfo.in_`） | 建独立 venv，pydantic 固定 2.5.0 |
| PyPI 镜像不稳定，依赖装到一半卡住 | 分批安装 / 换阿里云镜像 |
| pgserver 锁文件在 `%LOCALAPPDATA%\Temp` 不可写 | 把 `PostgresServer._lock` 改到项目内 `.pgtmp/` |
| session 级 fixture 与函数级用例跨 loop | 全部 fixture 函数级 + NullPool + Selector 循环 |
| ORM 对象 `expire_all()` 后访问属性触发懒加载 | 改用 `execution_options(populate_existing=True)` |

---

## 四、现有接口兼容性检查

| 检查项 | 结果 |
|---|---|
| 路由数量 | 39 条，与改造前一致（未增删） |
| `GET /health` | 200 `{"status":"ok"}` |
| 商品 CRUD | 全部通过，响应字段未变 |
| 秒杀行为 | 成功时**同时扣减 `products.stock` 与 `seckill_products.stock_count`**（现有双库存行为被测试锁定） |
| 订单越权 | 403，行为未变 |
| 限流 | 429，行为未变 |
| `app/` 源码 | 未修改 |

> 注意：测试**锁定的是当前行为**，包括"双库存同步扣减"这个待改造点。
> 第 5 步把秒杀切到 `warehouse_stock` 时，这条断言要**同步更新**，否则会出现"测试通过但语义已变"的假绿。

---

## 五、尚未解决的问题

1. **`orders.seckill_product_id` 仍是 NOT NULL**
   普通销售订单依然建不出来。第一阶段后续迁移才改可空 + 加 `order_type`。

2. **金额仍是 Float**
   `products.price`、`orders.total_amount`、`seckill_products.seckill_price` 未迁移到 `Numeric`。
   迁移时还要同步处理 FastAPI 的 Decimal 序列化（默认会变字符串）和前端 `main.js` 的金额展示。

3. **库存仍是双真相**
   `products.stock` 与 `seckill_products.stock_count` 各自扣减，没有 `warehouse_stock`、没有流水、没有预留。
   这是第二阶段的核心改造对象。

4. **测试与仓库声明的版本不一致**
   仓库 `requirements.txt` 钉的是 `fastapi 0.104.1 / pydantic 2.5.0`，但本机实测必须显式降级 pydantic 才能跑；
   另外 `prometheus-fastapi-instrumentator` 实际用了 8.1.0（仓库写 6.1.0）。
   **建议后续把 requirements 收敛成"实测可跑"的版本**，否则别人 clone 下来跑不起来。

5. **没有 CI**
   仓库有 `.github/workflows/ci-cd.yml`，但没有测试可跑。现在 `tests/` 有了，
   但 CI 里还需要一个 PostgreSQL service（GitHub Actions 可用 service container），本机这份配置用的是嵌入式 PG，两者要分开。

6. **`app/models/__init__.py` 是空文件**
   导致 `import app.models` 不会注册任何模型，`alembic autogenerate` 会生成空迁移（本次踩到）。
   目前靠在 `alembic/env.py` 里逐个 import 绕过；**根因建议后续在 `__init__.py` 里统一导出**。

7. **前端静态页未做兼容测试**
   `app/frontend/static/scripts/main.js`（46KB）没有端到端测试，后续改接口字段时需要人工验证或补 Playwright。

---

## 六、下一步（等你确认后再动手）

按之前的六步计划，第 2 步是「新增 5 张库存表（纯新增，零风险）」：

```
skus / warehouses / warehouse_stocks / stock_transactions / stock_reservations
```

配套 `InventoryService`（入库/出库/预留/释放/调拨/盘点）+ 幂等 + 流水完整性测试。
**在你确认前不会改动 `products` / `orders` 等既有表。**
