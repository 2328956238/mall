# 开发问题记录

> 记录第 0 步（回归测试）与第 1 步（Alembic baseline）执行过程中**真实遇到并解决的问题**。
> 每条都写清楚：现象 → 原因 → 处理 → 影响面。

---

## 一、环境类

### P1. 本机没有 Docker，也没有 PostgreSQL

**现象**：`docker`、`psql`、`pg_ctl` 均不存在，5432 端口无监听。

**影响**：`docker-compose.yml` 无法验证；测试没法连真库。

**处理**：用 `pgserver`（自带 PostgreSQL 16 二进制）拉起嵌入式实例，端口随机、数据目录在项目内。
`conftest.py` 启动时建库，测试结束自动停。

**副作用**：pgserver 是测试期依赖，`requirements.txt` 里没有。已记录，见 P7。

---

### P2. `fastapi 0.104.1` 与 `pydantic 2.13` 不兼容

**现象**：导入 `app.main` 时报

```
AttributeError: 'FieldInfo' object has no attribute 'in_'
```

**原因**：全局环境里 fastapi 是 0.104.1（项目 `requirements.txt` 钉的版本），
但 pydantic 被别的项目升到了 2.13.4。fastapi 0.104 依赖 pydantic 2.4~2.5 的 `FieldInfo` 结构。

**处理**：为 mall 单独建 `.venv`，把 pydantic 固定在 2.5.0 + pydantic-core 2.14.1。

**教训**：`requirements.txt` 钉了版本，但没钉住**传递依赖**，换台机器就会炸。

---

### P3. 缺少三个未被 `requirements.txt` 完整覆盖的包

| 包 | 触发点 | 说明 |
|---|---|---|
| `email-validator` | `app/schemas/user.py` 的 `EmailStr` | `requirements.txt` 写的是 `pydantic[email]`，某些镜像下不会装 |
| `prometheus-fastapi-instrumentator` | `app/middleware/prometheus.py` 顶层 import | `main.py` 里 `setup_metrics` 包了 try/except，但**import 本身在 try 外面**，缺包直接启动失败 |
| `prometheus_client` | 上一个包的依赖 | 用 `--no-deps` 装时会漏 |

**影响**：`app/main.py` 的容错写法没有真正容错 —— try/except 挡不住 import 失败。

**处理**：装上并记录。**建议后续把 `from app.middleware.prometheus import setup_metrics` 也包进 try**。

---

### P4. PyPI 镜像不稳定，`pip install` 卡死

**现象**：`pip install` 十多分钟无输出，CPU 空转，`site-packages` 里一个包都没装上。

**处理**：
- 换阿里云镜像（清华镜像索引本身 0.9s 可通，但下载会卡）
- 分批安装，**不用 `--only-binary` 一次性装一堆**
- 实在不通时改用"复用全局已装依赖 + 只补差异"的方式（把 venv 的 `include-system-site-packages` 打开）

---

### P5. pgserver 的进程锁目录不可写

**现象**：

```
PermissionError: [Errno 13] Permission denied:
  C:\Users\...\AppData\Local\Temp\python_PostgresServer\python_PostgresServer\.lockfile
```

**原因**：pgserver 用 `platformdirs.user_runtime_path()` 定位锁文件，在 Windows 上固定指向
`%LOCALAPPDATA%\Temp`。该目录对部分解释器不可写（能读不能写，创建文件报 `WinError 5`）。

**处理**：在 `conftest.py` 里提前替换类属性：

```python
import pgserver.postgres_server as _pgs
_pgs.PostgresServer._lock = fasteners.InterProcessLock(str(项目内路径 / ".pgserver.lock"))
```

**副作用**：必须在 `get_server()` **之前**执行，否则无效。

---

## 二、测试框架类

### P6. asyncpg 连接跨事件循环 → `attached to a different loop`

**现象**：

```
RuntimeError: Task ... got Future ... attached to a different loop
```

**原因**：pytest-asyncio 默认给每个用例一个新事件循环；而会话级 fixture 里建的 engine
会把连接（连同它的 Future）绑在**另一个**循环上。asyncpg 明确禁止跨循环复用连接。

**处理**（三条一起上才干净）：

1. 所有 fixture 改成**函数级**
2. engine 用 `NullPool`（不缓存连接，用完就断）
3. 应用模块级 `app.db.session.engine` 也换成 `NullPool`
   （否则任何漏掉依赖注入的调用点都会踩到跨循环）

### P6.1 Windows Proactor 循环在连接释放时崩溃

**现象**：把上面的问题解决后，第二个用例报

```
AttributeError: 'NoneType' object has no attribute 'send'   (proactor_events.py:402)
```

**原因**：Windows 默认 `ProactorEventLoop` 在循环关闭后仍有传输层回调。

**处理**：`conftest.py` 顶部切到 Selector 循环：

```python
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
```

---

### P7. `expire_all()` 后访问属性触发懒加载 → `MissingGreenlet`

**现象**：

```
sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called
```

**原因**：测试里用 `db_session.expire_all()` 想让 ORM 重新读库，但之后同步访问
`product.stock` 会触发**隐式 IO**，而异步 Session 不允许隐式 IO。

**处理**：不用 `expire_all()`，改成在同一查询里刷新：

```python
select(Product).where(...).execution_options(populate_existing=True)
```

---

## 三、迁移类（最有价值的两个）

### P8. `alembic autogenerate` 生成了**空迁移**

**现象**：`alembic revision --autogenerate` 输出 `pass`，看起来"没有差异"，
实际上**一张表都没识别到**。

**原因**：`app/models/__init__.py` 是**空文件**。alembic 的 `env.py` 里写 `import app.models`，
但这个包什么都不导入，所有模型类根本没注册到 `Base.metadata`。
（应用本身没暴露这个问题，因为 `main.py` 会 `import app.api.v1.*`，路由间接导入了模型。）

**处理（临时）**：`alembic/env.py` 里逐个 import 模型模块。
**处理（根治）**：把 `app/models/__init__.py` 改成统一导出。

**教训**：空迁移 ≠ 结构一致。第一次 autogenerate 之后**必须打开文件看内容**，
看到 `pass` 就要怀疑自己。

---

### P9. `downgrade` 不删 PostgreSQL 枚举类型，导致二次升级失败

**现象**：

```
alembic downgrade base   → 成功
alembic upgrade head     → 失败
[SQL: CREATE TYPE orderstatus AS ENUM ('PENDING', 'PAID', ...)]
DuplicateObject: type "orderstatus" already exists
```

**原因**：`op.drop_table('orders')` 只删表，**不删表用到的 ENUM 类型**。
PostgreSQL 的 ENUM 是独立 schema 对象，必须显式 drop。

**处理**：在 `downgrade()` 末尾补

```python
sa.Enum(name='orderstatus').drop(op.get_bind(), checkfirst=True)
```

**教训**：只测 `upgrade` 永远发现不了这个。**必须测 `upgrade → downgrade → upgrade`**。

---

### P10. 连接串不能写进 `alembic.ini`

**处理**：`alembic.ini` 里不填 `sqlalchemy.url`，`env.py` 只从环境变量 `DATABASE_URL` 读取，
没设就直接报错退出。

**理由**：`alembic.ini` 是要进仓库的，写死连接串等于把生产库密码提交上去。

---

### P11. 在 pytest 用例里直接调 `alembic.command` 会撞事件循环

**现象**：

```
RuntimeError: asyncio.run() cannot be called from a running event loop
```

**原因**：`alembic/env.py` 的 `run_migrations_online()` 内部用 `asyncio.run()` 起自己的循环，
而 pytest 的异步用例本身已经在一个运行中的循环里。

**处理**：迁移测试改成**子进程**跑 `python -m alembic ...`，用 `asyncio.to_thread(subprocess.run)`
避免阻塞事件循环，也避免 Windows Selector 循环不支持 `create_subprocess_exec` 的问题。

---

### P12. `rollback()` 之后访问 ORM 对象同样触发 `MissingGreenlet`

**现象**：P7 修好后，一个"验证唯一键冲突"的用例又报同样的错。

**原因**：`await session.rollback()` 会把 Session 里的对象全部标记为过期。
此时再访问 `t1.id`（哪怕只是拼一个新的 ORM 对象）就会触发隐式 IO。

**处理**：**在可能回滚之前，先把需要的主键取成普通 int**：

```python
ids = {"t1": (t1.id, sku1.id, wh1.id)}   # 先落地
...
await session.rollback()                  # 之后的代码只用 ids，不碰 ORM 对象
```

**教训**：异步 Session 里"对象"和"值"必须分清，凡是跨 `commit/rollback/expire` 边界的，
一律先取成值。

---

### P13. pgserver 数据目录被强杀后损坏，无法重启

**现象**：

```
initdb: error: directory ".pgdata-baseline" exists but is not empty
```

**原因**：上一轮测试/调试结束时直接 `Stop-Process` 杀了 postgres，
数据目录里残留 `postmaster.pid` 等文件，pgserver 认为目录已存在但又不完整。

**处理**：删除数据目录后重新 `get_server()` 会自动 initdb。
**测试环境的数据目录是可再生的**，删掉没有损失；生产环境另说。

---

### P14. 核销预留时用"可用量"判断，导致把货全部锁住之后核销失败

**现象**：并发秒杀测试里，5 件货被 5 个请求全部预留后，`commit_reservation` 报
`InsufficientStock`，订单建了却没真正出库。

**原因**：扣减库存的 SQL 守卫写的是 `on_hand - reserved >= qty`。
核销预留是"从自己锁住的那份里出"，此时可用量恰好是 0，这个条件永远不成立。

**处理**：区分两种扣减场景——

```python
if on_hand_delta < 0:
    if reserved_delta < 0:      # 核销预留：只看实际库存够不够
        stmt = stmt.where(WarehouseStock.on_hand >= need)
    else:                        # 普通出库：只能动可用量
        stmt = stmt.where(WarehouseStock.on_hand - WarehouseStock.reserved >= need)
```

**教训**：这条是并发用例抓出来的，单元测试（单笔、库存充足）永远发现不了。

---

### P15. `rollback()` 之后访问 ORM 属性，把真正的异常盖掉

**现象**：秒杀核销失败时，日志里只有 `MissingGreenlet`，看不到真实原因。

**原因**：`except` 分支里写 `logger.exception("...", seckill.id)`，
而此前的 `rollback()` 已经让 `seckill` 过期，读 `.id` 触发隐式 IO。

**处理**：进入方法时先把要用的字段取成普通值（`seckill_id = seckill.id`、
`order_id = order.id`），异常分支只用这些值。

**教训**：异步 Session 里"对象"和"值"必须分清（同 P7/P12），这是第三次踩到。

---

### P16. autogenerate 生成的外键没有名字，downgrade 删不掉

**现象**：

```
CompileError: Can't emit DROP CONSTRAINT for constraint ForeignKeyConstraint(...); it has no name
```

**原因**：`op.create_foreign_key(None, ...)` 让数据库自动起名，
而 `op.drop_constraint(None, ...)` 无法在没有名字的情况下生成 DDL。

**处理**：迁移里给外键显式命名，模型上保持一致：

```python
op.create_foreign_key('fk_seckill_products_sku_id', ...)
op.drop_constraint('fk_seckill_products_sku_id', ..., type_='foreignkey')
```

**教训**：凡是 `downgrade` 里要删的对象，`upgrade` 时必须给它名字。

---

### P17. `create_all` 建出来的库没有默认租户，测试外键全炸

**现象**：第 6 步给老表加 `tenant_id` 后，几乎所有回归测试报
`ForeignKeyViolation: tenant_id=1`。

**原因**：默认租户是**迁移里**插入的数据，而测试用 `Base.metadata.create_all` 建库，
不跑迁移，所以 `tenants` 是空表。

**处理**：conftest 建表后补一行插入默认租户。
同时 `TRUNCATE` 必须包含 `tenants`（否则跨用例租户编码撞唯一键），
清完立刻重建，并用 `setval` 把自增序列推过 1（否则新建租户又拿 id=1 撞主键）。

**教训**：迁移里带数据操作时，测试基建必须同步，否则两套世界会分叉。

---

## 四、待办（尚未解决）

| 编号 | 问题 | 计划 |
|---|---|---|
| T1 | `requirements.txt` 钉的版本组合本机跑不起来（P2、P3） | 收敛成"实测可跑"的版本，并区分运行/测试依赖 |
| T2 | `app/models/__init__.py` 空文件（P8 根因） | 第 2 步一并修掉 |
| T3 | `main.py` 对 prometheus 的 try/except 挡不住 import 失败（P3） | 把 import 也放进 try |
| T4 | `docker-compose.yml` 未验证 | 需要一台有 Docker 的机器 |
| T5 | CI 里没有可跑的测试 | 加 PostgreSQL service 后接入 pytest |
