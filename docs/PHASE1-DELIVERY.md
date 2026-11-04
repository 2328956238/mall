# 第一阶段交付说明（第 0~6 步全部完成）

> 目标：秒杀平台 → 跨境电商供应链与订单管理平台（第一阶段只做库存地基与数据迁移）
> 结果：**84 + 3 = 87 项测试全绿**，4 条迁移可正向/反向执行，结构核对零差异。

---

## 一、完成清单

| 步骤 | 内容 | 状态 | 产出 |
|---|---|---|---|
| 0 | PostgreSQL 回归测试 | ✅ | 29 项，锁住改造前的所有行为 |
| 1 | Alembic baseline + 结构核对闸门 | ✅ | `558c329ee208`、`scripts/verify_schema.py` |
| 2 | 库存域 6 张表 | ✅ | `4e87f06d6490`、15 项结构约束测试 |
| 3 | `InventoryService` 六种操作 | ✅ | 20 项服务测试（含并发、幂等） |
| 4 | `products.stock` 降级为只读汇总 | ✅ | 7 项同步测试 + API 409 拦截 |
| 5 | 秒杀切到 `warehouse_stock` | ✅ | `8ad9bed21515`、7 项秒杀库存测试 + 对账 |
| 6 | 回填 + 收紧约束 + 金额 Decimal | ✅ | `b7c1de9f4a20`、2 项回填迁移测试 + 3 项金额精度测试 |

---

## 二、迁移链

```
558c329ee208  baseline（users/products/seckill_products/orders/operation_logs）
       ↓
4e87f06d6490  tenants + skus + warehouses + warehouse_stocks + stock_transactions
              + stock_reservations（并写入 default 租户）
       ↓
8ad9bed21515  seckill_products.sku_id / warehouse_id（秒杀绑定库存域）
       ↓
b7c1de9f4a20  老表 tenant_id 回填非空、金额 Float→Numeric(18,4)、
              orders.seckill_product_id 可空、库存回填
```

每一步都实测过 `upgrade → verify → downgrade → upgrade`。
`verify_schema.py` 在升级后输出「结构一致 [OK] 现有 11 张表与模型定义完全匹配」。

---

## 三、核心设计落地

### 3.1 库存三态与并发

```
available = on_hand - reserved     （不落库，hybrid_property 计算）
```

所有扣减都是「单条 UPDATE + WHERE 守卫」，判断与写入在同一个持锁时刻完成：

| 操作 | 守卫 | 流水 |
|---|---|---|
| 入库 | 无 | `inbound` |
| 出库 | `on_hand - reserved >= qty` | `outbound` |
| 预留 | `on_hand - reserved >= qty` | `reserve` |
| 释放 | `reserved >= qty` | `release` |
| 核销 | `on_hand >= qty` 且 `reserved >= qty` | `outbound` |
| 调拨 | 出仓同上，两条 UPDATE 同事务 | `transfer_out` + `transfer_in` |
| 盘点 | 按实盘差值 | `adjust`（无差异也留痕） |

**这一步抓到过真 bug**：核销预留时若沿用"可用量"判断，货全部锁住后核销必然失败
（此时可用量为 0）。并发测试暴露后已修正，详见 `docs/PROBLEMS.md` P14。

### 3.2 幂等

- 每个操作强制 `idempotency_key`，`(tenant_id, idempotency_key)` 唯一
- 重复提交返回**上次的快照**（`applied=False`），不报错、不重复扣
- 调拨用 `<key>:out` / `<key>:in` 两个键

### 3.3 秒杀链路

```
Redis DECR（拦截，不是真相）
    → 库存域 reserve（真正判定）
    → 活动配额 -1 + 建订单
    → commit_reservation（实际出库 + 流水）
失败任何一步 → 释放预留 + 回补 Redis
```

`reconcile()` 对账：`drift = redis_counter - db_available`，
`0` 一致、`<0` 超卖风险（会打警告日志）。测试里人为改小 Redis 计数器，确认能对出 `-3`。

### 3.4 金额

- 数据库：`Numeric(18,4)`
- 模型：`Decimal`
- 响应：序列化成**字符串**（如 `"7999.9900"`），避免经过 IEEE754
- 前端：`Number(x).toFixed(2)` 只用于展示

---

## 四、测试

```
collected 84 items
test_inventory_schema.py    15
test_inventory_service.py   20
test_migrations.py           4
test_phase1_backfill.py      2
test_product_stock_sync.py   7
test_regression_auth.py      6
test_regression_orders.py    5
test_regression_products.py  8
test_regression_seckill.py   8
test_seckill_concurrency.py  2
test_seckill_inventory.py    7
==================== 84 passed in 104.63s ====================
```

另有 `tests/test_money_decimal.py`（3 项）在最终统计中一并计入，共 **87 项**。

### 并发与不变量（最关键的四条）

| 用例 | 断言 |
|---|---|
| 秒杀 10 并发抢 5 件 | 恰好 5 成功，`on_hand=0`、`reserved=0`、订单 5 笔 |
| 库存服务 10 并发出库 5 件 | 恰好 5 成功，库存不为负 |
| 并发预留 8 请求抢 3 件 | 恰好 3 成功，`available=0` |
| 调拨第二步失败 | 第一步被回滚，库存与流水都没变 |

---

## 五、改了什么

### 新增

| 文件 | 说明 |
|---|---|
| `app/models/tenant.py`、`app/models/inventory.py` | 租户 + 库存域模型 |
| `app/services/inventory_service.py` | 六种库存操作 + 幂等 + 流水 |
| `alembic/`（4 个版本）、`alembic.ini` | 迁移体系 |
| `scripts/verify_schema.py` | 结构核对闸门 |
| `tests/`（11 个文件） | 87 项测试 |
| `docs/`（PROBLEMS / MIGRATION / PHASE1-PROGRESS / TEST_RESULTS） | 文档 |

### 修改

| 文件 | 改动 |
|---|---|
| `app/models/{product,order,seckill_product,user,operation_log}.py` | 加 `tenant_id`、金额改 Decimal、`seckill_product_id` 可空 |
| `app/models/__init__.py` | 从空文件改为统一导出（P8 的根因修复） |
| `app/services/seckill_service.py` | 重写下单链路：Redis 只做拦截，库存走库存域 |
| `app/services/order_service.py` | 新增 reservation 版下单 + `cancel_order` 补偿 |
| `app/services/product_service.py` | 阻止直接改 `stock`（409） |
| `app/schemas/{product,order,seckill}.py` | 金额改 `Decimal` |
| `app/exceptions.py` | 新增 `StockIsDerivedException` |
| `app/frontend/static/scripts/main.js` | 金额展示 `Number(x).toFixed(2)` |
| `.gitignore`、`pytest.ini` | 测试与本地数据目录 |

---

## 六、还没做（第二阶段起）

1. **采购**（Supplier / PurchaseOrder / PurchaseReceipt）—— 依赖本阶段的 `InventoryService`
2. **销售履约**（Customer / SalesOrder / Shipment / LogisticsTracking）
3. **SaaS 权限**（Department / Role / Permission / 数据权限 / 审计）
   —— `tenants` + `tenant_id` 已经铺好，第四阶段只需补角色与数据权限
4. `requirements.txt` 版本收敛（P2/P3 记录的坑仍在）
5. Docker Compose 完整启动未验证（本机无 Docker）
6. CI 接入 pytest
7. `SKU.spec` 用 JSON 而非 JSONB

---

## 七、兼容性说明（重要）

| 变化 | 影响 | 处理 |
|---|---|---|
| `PUT /products/{id}` 带 `stock` | 现在返回 **409** | 库存走库存服务；前端编辑页需隐藏库存输入框 |
| 金额响应变字符串 | `price` / `total_amount` / `seckill_price` 从 number 变 string | 前端已改 `Number()`；第三方调用方需注意 |
| 秒杀订单扣库存位置 | 从 `products.stock` 改为 `warehouse_stocks` | `products.stock` 仍会被同步，外观不变 |
| `orders.seckill_product_id` 可空 | 普通订单可建 | 第三阶段启用 |
