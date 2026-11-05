"""tenant backfill + money to numeric + inventory backfill

把第一阶段六步收尾的内容合到一个迁移里，顺序是刻意的：

1. 加 `tenant_id`（先可空）
2. 回填默认租户
3. 收紧为非空 + 外键 + 索引
4. 金额 Float → Numeric(18,4)
5. `orders.seckill_product_id` 改可空（为第三阶段普通订单让路）
6. 库存回填：每商品一个 SKU、每租户一个默认仓、`warehouse_stocks` 承接 `products.stock`

第 6 步做完后，`products.stock` 只是派生汇总，真正的库存在 `warehouse_stocks`。

Revision ID: b7c1de9f4a20
Revises: 8ad9bed21515
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7c1de9f4a20"
down_revision: Union[str, None] = "8ad9bed21515"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TENANT_TABLES = ("users", "products", "seckill_products", "orders", "operation_logs")


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1) 加租户列（先可空，避免老数据直接违反非空约束）
    # ------------------------------------------------------------------
    for table in _TENANT_TABLES:
        op.add_column(table, sa.Column("tenant_id", sa.Integer(), nullable=True))

    # ------------------------------------------------------------------
    # 2) 回填默认租户（决策 2：老数据统一挂 default）
    # ------------------------------------------------------------------
    for table in _TENANT_TABLES:
        op.execute(
            f"UPDATE {table} SET tenant_id = (SELECT id FROM tenants WHERE code = 'default') "
            f"WHERE tenant_id IS NULL"
        )

    # ------------------------------------------------------------------
    # 3) 收紧：非空 + 外键 + 索引
    # ------------------------------------------------------------------
    for table in _TENANT_TABLES:
        op.alter_column(table, "tenant_id", nullable=False)
        op.create_foreign_key(
            f"fk_{table}_tenant_id", table, "tenants", ["tenant_id"], ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"], unique=False)

    # ------------------------------------------------------------------
    # 4) 金额 Float → Numeric(18,4)
    # ------------------------------------------------------------------
    op.alter_column(
        "products", "price",
        type_=sa.Numeric(18, 4), existing_type=sa.Float(), existing_nullable=False,
        postgresql_using="price::numeric(18,4)",
    )
    op.alter_column(
        "seckill_products", "seckill_price",
        type_=sa.Numeric(18, 4), existing_type=sa.Float(), existing_nullable=False,
        postgresql_using="seckill_price::numeric(18,4)",
    )
    op.alter_column(
        "orders", "total_amount",
        type_=sa.Numeric(18, 4), existing_type=sa.Float(), existing_nullable=False,
        postgresql_using="total_amount::numeric(18,4)",
    )

    # ------------------------------------------------------------------
    # 5) 普通订单没有秒杀活动
    # ------------------------------------------------------------------
    op.alter_column(
        "orders", "seckill_product_id",
        existing_type=sa.Integer(), nullable=True,
    )

    # ------------------------------------------------------------------
    # 6) 库存回填
    # ------------------------------------------------------------------
    # 6.1 每个租户一个默认仓
    op.execute(
        """
        INSERT INTO warehouses (tenant_id, code, name, country, is_active, created_at, updated_at)
        SELECT t.id, 'DEFAULT', '默认仓', 'CN', true, now(), now()
        FROM tenants t
        WHERE NOT EXISTS (
            SELECT 1 FROM warehouses w WHERE w.tenant_id = t.id AND w.code = 'DEFAULT'
        )
        """
    )

    # 6.2 每个商品一个 SKU（编码用 P{id}，规格为空对象，售价沿用商品价）
    op.execute(
        """
        INSERT INTO skus (tenant_id, product_id, sku_code, spec, sale_price, status, created_at, updated_at)
        SELECT p.tenant_id, p.id, 'P' || p.id, '{}'::json, p.price, 'active'::sku_status, now(), now()
        FROM products p
        WHERE NOT EXISTS (SELECT 1 FROM skus s WHERE s.product_id = p.id)
        """
    )

    # 6.3 products.stock → warehouse_stocks.on_hand
    op.execute(
        """
        INSERT INTO warehouse_stocks
            (tenant_id, sku_id, warehouse_id, on_hand, reserved, version, created_at, updated_at)
        SELECT s.tenant_id, s.id, w.id, COALESCE(p.stock, 0), 0, 0, now(), now()
        FROM skus s
        JOIN products p ON p.id = s.product_id
        JOIN warehouses w ON w.tenant_id = s.tenant_id AND w.code = 'DEFAULT'
        WHERE NOT EXISTS (
            SELECT 1 FROM warehouse_stocks ws
            WHERE ws.sku_id = s.id AND ws.warehouse_id = w.id
        )
        """
    )

    # 6.4 给回填出来的库存补一条初始化流水（有库存才有账）
    op.execute(
        """
        INSERT INTO stock_transactions
            (tenant_id, sku_id, warehouse_id, txn_type, qty_delta, on_hand_after,
             reserved_after, ref_type, ref_id, idempotency_key, remark, created_at, updated_at)
        SELECT ws.tenant_id, ws.sku_id, ws.warehouse_id, 'adjust'::stock_txn_type, 0,
               ws.on_hand, ws.reserved, 'migration', 'backfill',
               'migrate:stock:' || ws.id, '迁移初始化', now(), now()
        FROM warehouse_stocks ws
        WHERE NOT EXISTS (
            SELECT 1 FROM stock_transactions t
            WHERE t.idempotency_key = 'migrate:stock:' || ws.id
        )
        """
    )

    # 6.5 秒杀活动绑定到库存域
    op.execute(
        """
        UPDATE seckill_products sp
        SET sku_id = s.id, warehouse_id = ws.warehouse_id
        FROM skus s
        JOIN warehouse_stocks ws ON ws.sku_id = s.id
        WHERE s.product_id = sp.product_id AND sp.sku_id IS NULL
        """
    )


def downgrade() -> None:
    # 回填出来的库存数据要清掉，否则回退后 products.stock 与库存域会重复计算
    op.execute(
        """
        DELETE FROM stock_transactions
        WHERE idempotency_key IN (
            SELECT 'migrate:stock:' || id FROM warehouse_stocks
        ) AND ref_type = 'migration'
        """
    )
    op.execute("UPDATE seckill_products SET sku_id = NULL, warehouse_id = NULL")
    op.execute("DELETE FROM warehouse_stocks")
    op.execute("DELETE FROM skus WHERE sku_code LIKE 'P%'")
    op.execute("DELETE FROM warehouses WHERE code = 'DEFAULT'")

    op.alter_column("orders", "seckill_product_id", existing_type=sa.Integer(), nullable=False)

    op.alter_column(
        "products", "price",
        type_=sa.Float(), existing_type=sa.Numeric(18, 4), existing_nullable=False,
        postgresql_using="price::double precision",
    )
    op.alter_column(
        "seckill_products", "seckill_price",
        type_=sa.Float(), existing_type=sa.Numeric(18, 4), existing_nullable=False,
        postgresql_using="seckill_price::double precision",
    )
    op.alter_column(
        "orders", "total_amount",
        type_=sa.Float(), existing_type=sa.Numeric(18, 4), existing_nullable=False,
        postgresql_using="total_amount::double precision",
    )

    for table in _TENANT_TABLES:
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)
        op.drop_constraint(f"fk_{table}_tenant_id", table, type_="foreignkey")
        op.drop_column(table, "tenant_id")
