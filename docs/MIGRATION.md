# 数据库迁移手册（Alembic）

> 适用：本仓库从「`Base.metadata.create_all` 建表」迁移到「Alembic 版本化迁移」。
> 第一阶段只建立 **baseline**，不改变任何业务表结构。

---

## 0. 铁律

1. **迁移只认 `DATABASE_URL`**，不把连接串写进 `alembic.ini`（避免密码进仓库）。
2. **stamp 之前必须先核对结构**，差异未清零不允许 stamp。
3. **每次升级前备份**，回滚优先用 `downgrade`，不行再恢复备份。
4. 生产环境禁止 `create_all`，由迁移负责建表。

---

## 1. 备份（升级前必做）

```bash
# 结构 + 数据
pg_dump "$PG_URL" -Fc -f backup_$(date +%Y%m%d_%H%M%S).dump

# 只备份结构（做结构比对时够用）
pg_dump "$PG_URL" --schema-only -f schema_$(date +%Y%m%d_%H%M%S).sql
```

恢复：

```bash
pg_restore -d "$PG_URL" --clean --if-exists backup_YYYYmmdd_HHMMSS.dump
```

> `--clean --if-exists` 会先删对象再重建，属于**破坏性操作**，只在确认要整库回滚时使用。

---

## 2. 首次接入现有库（最重要的场景）

现有库是 `create_all` 建的，没有 `alembic_version` 表。**不能直接 stamp**，否则结构漂移会被掩盖。

```bash
# ① 核对结构：真实库 vs 代码模型
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db \
  python scripts/verify_schema.py
```

- 输出 `结构一致 [OK]` → 继续
- 输出差异列表并退出码 1 → **停下来**，先补迁移或修模型，不允许 stamp

```bash
# ② 结构确认无误后，标记到 baseline
DATABASE_URL=... alembic stamp head

# ③ 复核
DATABASE_URL=... alembic current     # 应输出 558c329ee208 (head)
DATABASE_URL=... python scripts/verify_schema.py
```

---

## 3. 全新环境

```bash
DATABASE_URL=... alembic upgrade head
DATABASE_URL=... python scripts/verify_schema.py   # 应一致
```

---

## 4. 回滚

```bash
alembic downgrade base      # 回到空库（baseline 的 downgrade 会删表 + 删枚举类型）
alembic downgrade -1        # 只回退一个版本
```

若数据已被破坏，用备份恢复：

```bash
pg_restore -d "$PG_URL" --clean --if-exists backup_YYYYmmdd_HHMMSS.dump
```

> baseline 的 `downgrade` 里显式执行了 `sa.Enum(name='orderstatus').drop(...)`。
> PostgreSQL 的 ENUM 是独立类型，`drop_table` 不会带走它；漏掉这步会导致二次
> `upgrade` 报 `type "orderstatus" already exists`（本次实测踩到并已修复）。

---

## 5. 迁移记录

| 版本号 | 说明 | 依赖 | 状态 |
|---|---|---|---|
| `558c329ee208` | baseline：users / products / seckill_products / orders / operation_logs | — | 已在验证库实测 upgrade → verify → downgrade → upgrade |
| `4e87f06d6490` | 库存域：tenants / skus / warehouses / warehouse_stocks / stock_transactions / stock_reservations，并写入 default 租户 | `558c329ee208` | 已实测 upgrade → verify（11 表一致）→ downgrade -1 → upgrade，`tests/test_migrations.py` 覆盖 |
| `8ad9bed21515` | 秒杀绑定库存域：`seckill_products.sku_id` / `warehouse_id` | `4e87f06d6490` | 已实测；外键显式命名，downgrade 可回退 |
| `b7c1de9f4a20` | 同步收尾：老表 `tenant_id` 回填非空、金额 Float→Numeric(18,4)、`orders.seckill_product_id` 可空、库存回填（SKU+默认仓+流水+秒杀绑定） | `8ad9bed21515` | 已实测 upgrade → verify（11 表一致）→ downgrade → upgrade，`tests/test_phase1_backfill.py` 覆盖 |

> 后续每个阶段的新迁移追加到本表，并注明「是否已在生产执行、执行人、时间」。

---

## 6. 后续阶段的迁移约定

- **加字段**：先加可空列 → 回填 → 再收紧非空（分三条迁移，不合并）。
- **改类型**（如 `Float → Numeric`）：先加新列 → 双写 → 回填 → 切读 → 删旧列。
  直接 `ALTER TYPE` 会锁表且不可逆。
- **加枚举值**：PostgreSQL 12+ 支持 `ALTER TYPE ... ADD VALUE`，但它不能在事务块里执行，
  需要 `op.execute()` + `transaction_per_migration=False` 或拆成独立迁移。
- **建索引**：大表用 `CREATE INDEX CONCURRENTLY`，需 `op.execute()` 且迁移事务关闭。
