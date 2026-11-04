# 第二阶段：采购管理

> 目标：完整的采购到入库链路，且**复用第一阶段的库存域**，不另起一套库存。

---

## 一、流程与状态机

```
draft ──submit──> submitted ──approve──> approved ──generate receipt──> receiving ──全部收完──> completed
  │                   │                     │                              │
  └──cancel──> cancelled └──reject──> rejected └──cancel──> cancelled        └──继续收货──> receiving
```

合法流转集中在一处（`purchase_service.ALLOWED`），非法跳转直接抛 `InvalidTransition`（HTTP 409）：

| 当前状态 | 允许流转到 |
|---|---|
| `draft` | submitted、cancelled |
| `submitted` | approved、rejected、cancelled |
| `approved` | receiving、cancelled |
| `receiving` | receiving、completed |
| `completed` / `rejected` / `cancelled` | 无（终态） |

---

## 二、数据模型（5 张表）

```
suppliers
  └─< purchase_orders >─ purchase_order_items
            │
            └─< purchase_receipts >─ purchase_receipt_items
```

| 表 | 关键约束 |
|---|---|
| `suppliers` | `(tenant_id, code)` 唯一 |
| `purchase_orders` | `(tenant_id, order_no)` 唯一；`total_amount >= 0`；`(tenant_id, status)` 索引 |
| `purchase_order_items` | `qty > 0`、`unit_price >= 0`、`received_qty <= qty`、`(po_id, sku_id, warehouse_id)` 唯一 |
| `purchase_receipts` | `(tenant_id, receipt_no)` 唯一、`(tenant_id, idempotency_key)` 唯一 |
| `purchase_receipt_items` | `qty > 0` |

金额全部 `Numeric(18,4)`，枚举存 value（`draft` / `confirmed`）不存 name。

---

## 三、收货如何入库

**不重复实现库存逻辑**，直接调第一阶段的 `InventoryService.inbound`：

```
confirm_receipt(receipt_id)
  ├─ 校验：qty <= 未收数量（Service 校验 + DB CHECK 兜底）
  ├─ 逐条 InventoryService.inbound(
  │       idempotency_key = f"receipt:{receipt_id}:item:{po_item_id}"
  │       ref_type = "purchase_receipt", ref_id = receipt_id)
  │       → warehouse_stocks.on_hand += qty
  │       → stock_transactions 写一条 inbound 流水
  │       → products.stock 自动重算
  ├─ received_qty += qty
  └─ 全部收完 → completed，否则 → receiving
```

**幂等有两层**：

1. 收货单级：`status == confirmed` 直接返回 `applied=False`；
2. 流水级：`idempotency_key` 唯一，就算中途失败重试也不会重复入库。

---

## 四、API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/purchase/suppliers` | 建供应商 |
| GET | `/api/v1/purchase/suppliers` | 供应商列表 |
| POST | `/api/v1/purchase/orders` | 建采购单（草稿） |
| GET | `/api/v1/purchase/orders` | 采购单列表 |
| GET | `/api/v1/purchase/orders/{id}` | 明细 |
| POST | `/api/v1/purchase/orders/{id}/submit` | 提交审核 |
| POST | `/api/v1/purchase/orders/{id}/approve` | 审核通过 |
| POST | `/api/v1/purchase/orders/{id}/reject` | 驳回 |
| POST | `/api/v1/purchase/orders/{id}/cancel` | 取消 |
| POST | `/api/v1/purchase/orders/{id}/receipts` | 生成入库任务 |
| POST | `/api/v1/purchase/receipts/{id}/confirm` | 确认收货（可部分收） |

业务异常统一由 `app/main.py` 的处理器转成 `{detail, code}`，状态码取自异常自带的 `http_status`。

---

## 五、测试（17 项）

| 用例 | 断言 |
|---|---|
| 全流程 | draft → submitted → approved → receiving → completed，库存真的 +14 |
| 部分收货 | 先收 6 件 → receiving；再收剩余 → completed |
| 状态机 | 草稿直接审核 → 409；重复提交 → 409；完成后取消 → 409 |
| 收货前置 | 未审核就生成入库任务 → 409 |
| 驳回 | 驳回后不能再审核 |
| 超收 | 收 11 件（单上 10）→ 409 |
| 幂等 | 重复确认不二次入库，`inbound_qty=0` |
| 流水 | 一次收货产生 2 条 `inbound` 流水，`ref_type=purchase_receipt` |
| 校验 | 空明细、重复 SKU+仓库、供应商编码重复 |
| 隔离 | 跨租户查采购单 → 404 |
| API | 全流程走通；非法流转返回 409 且带 `code`；未登录 401 |

---

## 六、迁移

| 版本 | 说明 |
|---|---|
| `f8a3a6adda45` | purchase: suppliers / purchase_orders / purchase_order_items / purchase_receipts / purchase_receipt_items |

已验证 `upgrade → verify（16 表一致）→ downgrade → upgrade`，`po_status` / `receipt_status` 两个枚举在 downgrade 里显式 drop。

---

## 七、遗留

1. **没有审批人权限校验**：现在任何登录用户都能审核自己的单子。第四阶段接入角色后要限制"审核人 ≠ 创建人"。
2. **收货单只支持单仓库**：明细来自采购单，但 `purchase_receipts.warehouse_id` 取了第一条明细的仓库；多仓混收要拆单。
3. **没有采购退货**：属于后续阶段。
4. **没有附件/对账单**：实际业务会有发票、装箱单。
