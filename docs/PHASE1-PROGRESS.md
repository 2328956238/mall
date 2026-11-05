# 第一阶段进度看板

> 目标：秒杀平台 → 跨境电商供应链 SaaS
> 当前进度：**第 2 步完成**（共 7 步：0~6）

---

## 进度总览

| 步骤 | 内容 | 状态 | 关键产出 |
|---|---|---|---|
| **0** | PostgreSQL 回归测试（锁定现有功能） | ✅ 完成 | 29 项测试 |
| **1** | Alembic baseline + 结构核对闸门 | ✅ 完成 | `558c329ee208` + `verify_schema.py` |
| **2** | 库存域建表（6 张表） | ✅ 完成 | `4e87f06d6490` + 19 项测试 |
| **3** | `InventoryService` 六种操作 | ⬜ 待做 | 入库/出库/预留/释放/调拨/盘点 |
| **4** | 兼容层：`products.stock` 只读汇总 | ⬜ 待做 | 双向同步 + 回归保持绿 |
| **5** | 秒杀切到 `warehouse_stock` | ⬜ 待做 | Redis 只做拦截 + 公平补偿 + 对账 |
| **6** | 回填数据 + 收紧约束 | ⬜ 待做 | `tenant_id` 非空、`seckill_product_id` 可空 |

测试数量：**48 项全绿**（29 回归 + 15 结构约束 + 4 迁移），耗时 56 秒。

---

## 第 2 步做了什么

### 2.1 模型（6 张表）

```
tenants
  └─< skus >─ products
        │
        └─< warehouse_stocks >─ warehouses
                │
                ├─< stock_transactions   （只追加流水）
                └─< stock_reservations   （可过期预留）
```

| 表 | 关键设计 |
|---|---|
| `tenants` | 最小实现（code / name / status）。部门角色留到第四阶段 |
| `skus` | 商品与 SKU 分离；`tenant_id` + `sku_code` 唯一；规格存 JSON |
| `warehouses` | `tenant_id` + `code` 唯一；带国家字段（跨境必需） |
| `warehouse_stocks` | `(sku_id, warehouse_id)` 唯一；`on_hand` / `reserved` / `version` |
| `stock_transactions` | `BigInteger` 主键；`(tenant_id, idempotency_key)` 唯一；记录变更后快照 |
| `stock_reservations` | 状态机 active/released/committed/expired；同幂等键唯一 |

### 2.2 三个关键决策（已落地）

**① 可用库存不落库**

```python
@hybrid_property
def available(self) -> int:
    return self.on_hand - self.reserved
```

落库就是第三份真相。数据库层用 `CHECK (reserved <= on_hand)` 兜底。

**② 金额直接用 Decimal**

新表 `Numeric(18, 4)`，不是 Float。测试断言了 `information_schema` 里确实是
`numeric(18,4)`，且 `19.99` 存取回来是 `Decimal("19.9900")`。

**③ 枚举存 value 不存 name**

老代码 `Enum(OrderStatus)` 存的是 `PENDING`（大写 name）。
新表统一 `values_callable` 存 `inbound`，测试里直接查 `txn_type::text` 断言等于 `"inbound"`。

### 2.3 迁移里带了数据操作

```python
op.execute(
    "INSERT INTO tenants (code, name, status, created_at, updated_at) "
    "VALUES ('default', '默认租户', 'active', now(), now()) "
    "ON CONFLICT (code) DO NOTHING"
)
```

`ON CONFLICT DO NOTHING` 保证重复执行不炸。

### 2.4 downgrade 里删了枚举

```python
sa.Enum(name='stock_txn_type').drop(op.get_bind(), checkfirst=True)
sa.Enum(name='reservation_status').drop(op.get_bind(), checkfirst=True)
sa.Enum(name='sku_status').drop(op.get_bind(), checkfirst=True)
```

这是第 1 步踩过的坑（P9），这次提前处理，并在 `test_migrations.py` 里用
`upgrade → downgrade base → upgrade` 锁死。

---

## 第 2 步测试明细

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `test_inventory_schema.py` | 15 | 6 张表存在 / 租户唯一 / SKU 编码租户内唯一且跨租户可复用 / 同 SKU 多仓库 / `(sku,warehouse)` 唯一 / on_hand 非负 / reserved 不得超过 on_hand / available 计算 / SKU 外键 / **Decimal 精度与 numeric(18,4)** / **枚举存 value** / 幂等键租户内唯一跨租户可重 / 预留默认状态 / 预留数量必须为正 |
| `test_migrations.py` | 4 | 空库 upgrade → downgrade base → upgrade（**验证枚举清理**）/ 迁移写入 default 租户 / 迁移建表集合与模型完全一致 / 版本号可追踪 |

---

## 还没解决的（更新自上一版）

| 编号 | 问题 | 状态 |
|---|---|---|
| T1 | `requirements.txt` 版本组合跑不起来 | 未动（改动风险大，建议单独一轮） |
| T2 | `app/models/__init__.py` 空文件 | ✅ **已修**（统一导出，`env.py` 简化） |
| T3 | `main.py` 对 prometheus 的 try/except 挡不住 import | 未动 |
| T4 | `docker-compose.yml` 未验证 | 未动（本机无 Docker） |
| T5 | CI 没有可跑的测试 | 未动 |

新增遗留：

| 编号 | 问题 |
|---|---|
| T6 | 老表（`products` / `orders` / `users` / `seckill_products` / `operation_logs`）**还没有 `tenant_id`**，第 6 步统一补 |
| T7 | `SKU.spec` 用的是 `JSON` 不是 `JSONB`，查询性能弱一些；暂不影响功能 |
| T8 | 目前只有模型层，**没有任何 API**，前端还用不到库存 |

---

## 下一步（第 3 步）预告

`app/services/inventory_service.py`，六种操作：

| 操作 | SQL 核心 | 流水类型 |
|---|---|---|
| 入库 | `on_hand += q` | `inbound` |
| 出库 | `on_hand -= q WHERE on_hand - reserved >= q` | `outbound` |
| 预留 | `reserved += q WHERE on_hand - reserved >= q` | `reserve` |
| 释放 | `reserved -= q WHERE reserved >= q` | `release` |
| 调拨 | 两条 UPDATE 同事务 | `transfer_out` + `transfer_in` |
| 盘点 | 按实盘数折算差值 | `adjust` |

共同要求：
- 单条 `UPDATE ... WHERE 条件` 完成判断与写入（靠行锁，不用悲观锁）
- 同事务写流水，流水带 `on_hand_after` 快照
- `idempotency_key` 冲突时返回**上次结果**而不是报错
- 全部操作带 `tenant_id`，越权直接拒绝

**等你确认后开始，仍不会改动 `products` / `orders` 等老表。**
