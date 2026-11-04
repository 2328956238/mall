# 第三阶段：销售与订单履约

> 从客户下单到签收的完整链路，库存动作全部复用 `InventoryService`。

---

## 一、流程

```
客户 → 销售订单(pending)
        │
        ├─ approve ─→ approved（此时才预留库存，避免未审核订单占货）
        │                │
        │                └─ create_shipment ─→ picking（发货单草稿）
        │                                          │
        │                                          └─ confirm_shipment ─→ shipped（预留核销=出库）
        │                                                                     │
        │                                                                     └─ 物流签收 ─→ completed
        │
        └─ cancel ─→ cancelled（释放预留）
```

| 当前状态 | 允许流转 |
|---|---|
| `pending` | approved、cancelled |
| `approved` | picking、cancelled |
| `picking` | shipped、picking（继续发） |
| `shipped` | completed |
| `completed` / `cancelled` | 终态 |

---

## 二、为什么新开 `sales_orders` 而不是复用 `orders`

老 `orders` 表是秒杀专用，`status` 用 sealed 的 `OrderStatus` 枚举。
往里加 `approved` / `picking` 需要 `ALTER TYPE ... ADD VALUE`（PG 对事务和回滚限制多），
而需求同时要求"保留秒杀业务的特殊处理逻辑"。

所以：

- **秒杀订单**：`orders` 表一行不改
- **销售订单**：`sales_orders` 一族新表，状态机独立
- **对用户的"兼容"**：`GET /api/v1/orders/all` 把两边按时间合并，列表看起来是一份

> `/orders/all` 必须声明在 `/orders/{order_id}` **之前**，否则 "all" 会被当成 UUID 匹配走
> —— 这个坑实测踩过，见下文。

---

## 三、库存动作时点

| 环节 | 库存动作 | 幂等键 |
|---|---|---|
| 审核通过 | `reserve`（可用量 -qty，实际不变） | `so:{id}:reserve:{item_id}` |
| 取消 | `release`（可用量恢复） | `so:{id}:release:{item_id}` |
| 确认发货 | `commit_reservation`（实际 -qty，锁定 -qty，写 outbound 流水） | `so:{id}:ship:{item_id}` |

**为什么审核时才预留**：pending 只是"草稿态"，此时占库存会让未确认的订单把货锁死。

**为什么发货要核销而不是再扣一次**：预留已经是自己的货，核销把 `reserved` 转成实际减少，
一次事务里 `on_hand -= q, reserved -= q`，不会出现"扣两次"。

---

## 四、数据模型（5 张表）

```
customers
  └─< sales_orders >─ sales_order_items
          │
          └─< shipments >─ shipment_items
                  │
                  └─< logistics_tracking
```

| 表 | 关键约束 |
|---|---|
| `customers` | `(tenant_id, code)` 唯一 |
| `sales_orders` | `(tenant_id, order_no)` 唯一、`total_amount >= 0`、`(tenant_id, status)` 索引 |
| `sales_order_items` | `qty > 0`、`shipped_qty <= qty`、`(sales_order_id, sku_id, warehouse_id)` 唯一 |
| `shipments` | `(tenant_id, shipment_no)` 唯一、`(tenant_id, idempotency_key)` 唯一 |
| `shipment_items` | `qty > 0` |
| `logistics_tracking` | `(shipment_id, occurred_at)` 索引 |

---

## 五、API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/sales/customers` | 建客户 |
| GET | `/api/v1/sales/customers` | 客户列表 |
| POST | `/api/v1/sales/orders` | 销售订单（pending） |
| GET | `/api/v1/sales/orders` | 列表 |
| GET | `/api/v1/sales/orders/{id}` | 明细 |
| POST | `/api/v1/sales/orders/{id}/approve` | 审核（预留） |
| POST | `/api/v1/sales/orders/{id}/cancel` | 取消（释放） |
| POST | `/api/v1/sales/orders/{id}/complete` | 手动完成 |
| POST | `/api/v1/sales/orders/{id}/shipments` | 生成发货单 |
| POST | `/api/v1/sales/shipments/{id}/confirm` | 确认发货（核销出库） |
| POST | `/api/v1/sales/shipments/{id}/tracking` | 加物流轨迹 |
| GET | `/api/v1/sales/shipments/{id}/tracking` | 轨迹列表 |
| GET | `/api/v1/orders/all` | **秒杀 + 销售合并视图** |

---

## 六、测试（14 项）

| 用例 | 断言 |
|---|---|
| 全流程 | pending → approved（预留 3）→ picking → shipped（实际 17）→ 签收 → completed |
| 库存不足 | 审核失败，订单仍 pending，`reserved=0` |
| 取消 | approved 后取消，`reserved` 归零、可用量恢复 20 |
| 部分发货 | 发 1 件 → picking；再发剩余 → shipped |
| 状态机 | 未审核发货 → 409；重复审核 → 409；已发货取消 → 409 |
| 超发 | 发 `qty+1` → 409 |
| 幂等 | 重复确认 `applied=False`、`shipped_qty=0`，库存不二次扣减 |
| 隔离 | 跨租户查单 → 404 |
| 编码重复 | 客户编码唯一 |
| API | 全流程走通；`/orders/all` 返回合并后的两种订单；未登录 401 |

---

## 七、迁移

`7a5f6eb7dcb7`：customers / sales_orders / sales_order_items / shipments / shipment_items / logistics_tracking

已验证 `upgrade → verify（22 表一致）→ downgrade → upgrade`，三个枚举在 downgrade 里显式 drop。

---

## 八、这次踩到的两个坑

1. **路由顺序**：`/orders/all` 注册在 `/orders/{order_id}` 之后，被当成 UUID 匹配 →
   `ValueError: badly formed hexadecimal UUID string`。必须把静态路径声明在动态路由之前。

2. **rollback 后读 ORM 属性**（第 4 次）：
   `svc.approve()` 内部 rollback 后，测试里 `order.id` 触发隐式 IO → `MissingGreenlet`。
   修法依旧是"跨可能失败边界先取成普通值"。

---

## 九、遗留

1. **没有支付**：销售订单审核后直接可发货，真实业务应有收款/信用额度校验。
2. **没有退换货**。
3. **发货单只支持单仓库**（同采购）。
4. **物流轨迹只存本地**，未对接快递 100 / 17TRACK 等查询接口。
5. **审核人权限未限制**（同采购，第四阶段接角色）。
