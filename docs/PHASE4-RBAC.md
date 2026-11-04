# 第四阶段：SaaS 权限体系

> 租户隔离（第一阶段已铺）+ 部门 / 角色 / 权限 + 数据范围 + 操作审计。

---

## 一、模型

```
tenants（第一阶段）
  ├─< departments （自关联树，path = "1/4/9"）
  ├─< roles >─< role_permissions >─ permissions（全局字典）
  └─< user_roles >─ users（新增 department_id）
```

| 表 | 关键约束 |
|---|---|
| `departments` | `(tenant_id, code)` 唯一；`path` 物化路径，一次 LIKE 查子树 |
| `roles` | `(tenant_id, code)` 唯一；`data_scope ∈ {all, department, self}` |
| `permissions` | `code` **全局唯一**（平台统一维护字典） |
| `user_roles` | `(user_id, role_id)` 唯一 |
| `role_permissions` | `(role_id, permission_id)` 唯一 |

**为什么权限全局、角色租户内**：权限是平台定义的"能做什么"（`purchase.approve`），
角色是每个租户自己的"谁来干"，租户不能自定义权限码，但能自由组合角色。

---

## 二、默认角色

| 角色 | 数据范围 | 权限 |
|---|---|---|
| `admin` | all | 全部 |
| `manager` | department | 采购/销售全流程 + 审计查看，**不给**系统角色管理 |
| `operator` | self | 建单、收货、发货，**不给**审核 |

权限字典 15 项，覆盖商品 / 库存 / 采购 / 销售 / 系统 / 数据六类。

---

## 三、迁移期兼容（重要）

存量用户一个角色都没有。如果一上来就强制校验，全部 403。

所以 `settings.ENFORCE_PERMISSIONS` 默认 **False**：

```
ENFORCE_PERMISSIONS=False → 有角色就按角色判，没角色放行（不阻塞在跑的业务）
ENFORCE_PERMISSIONS=True  → 严格：没角色 403 (NO_ROLE)，缺权限 403 (PERMISSION_DENIED)
```

**上线步骤**：

1. 部署，保持 `False`
2. 调 `PermissionService.init_tenant_bootstrap(tenant_id, first_user_id)` 建角色
3. 给存量用户 `assign_role`
4. 打开开关

---

## 四、数据权限

`Role.data_scope` 决定列表能看多少。多角色取**最宽**。

| scope | 过滤条件 |
|---|---|
| `all` | 不加限制（仍在租户内） |
| `department` | `sales_orders.user_id IN (本部门及子部门用户)` |
| `self` | `sales_orders.user_id = 当前用户` |

用户没分配部门时，`department` 退化为 `self`（防止越权看到全租户）。

部门树用**物化路径**而不是递归 CTE：

```
HQ   path = "1"
 └ SZ  path = "1/2"
    └ SZ-1 path = "1/2/3"
```

查子树：`path = '1/2' OR path LIKE '1/2/%'` —— 一次索引扫描，不用递归。

---

## 五、操作审计

复用第一阶段的 `operation_logs` 表，改造 `LogMiddleware`：

| 改造点 | 说明 |
|---|---|
| 只记写操作 | POST/PUT/PATCH/DELETE；读请求量大且无审计价值 |
| 补 `tenant_id` / `user_id` | 从 JWT 解析，审计能查出"谁在哪个租户干的" |
| 记录耗时 | `execution_time_ms` |
| 失败不影响业务 | 审计异常只记 logger，不改变响应 |

> 原来 `LogMiddleware` 是**所有**请求都写库，且没有 tenant_id。这次一并修正。

---

## 六、接口权限

```python
@router.post("/orders", dependencies=[Depends(require_permission("purchase.create"))])
```

已挂载的接口：

| 接口 | 权限码 |
|---|---|
| `POST /purchase/suppliers` | `purchase.create` |
| `POST /purchase/orders` | `purchase.create` |
| `POST /purchase/orders/{id}/approve` | `purchase.approve` |
| `POST /purchase/receipts/{id}/confirm` | `purchase.receive` |
| `POST /sales/orders` | `sales.create` |
| `POST /sales/orders/{id}/approve` | `sales.approve` |
| `POST /sales/shipments/{id}/confirm` | `sales.ship` |

---

## 七、测试（14 项）

| 用例 | 断言 |
|---|---|
| 权限字典幂等 | 第一次插入全部，第二次 0 新增 |
| 默认角色 | 三个角色存在，admin 拿到全部权限 |
| 角色分配 | operator 有 `sales.create`、没有 `purchase.approve` |
| 数据范围取最宽 | operator + manager → `department` |
| 未知角色 | 抛 `RoleError` |
| 租户初始化 | 第一个用户自动是 admin |
| 无角色 403 | `code=NO_ROLE` |
| admin 放行 | 201 |
| 角色差异 | operator 不能审核，manager 能 |
| **数据范围过滤** | 两个用户各下单，`self` 范围各看 1 条 |
| 部门树 | path 拼接正确、子树查询不串层级 |
| 未知上级部门 | 抛错 |
| 审计落库 | POST 写入 `operation_logs`，带 tenant_id |
| 审计不记读 | GET 不产生记录 |

---

## 八、迁移

| 版本 | 说明 |
|---|---|
| `39f23516f210` | departments / roles / permissions / user_roles / role_permissions |
| `076554ed75ab` | users.department_id |

都已验证 `upgrade → verify（27 表一致）→ downgrade → upgrade`。

**两个坑（都在 downgrade）**：

1. `users.department_id` 的外键 autogenerate 又生成了无名约束，downgrade 报
   `Can't emit DROP CONSTRAINT ... it has no name`（P16 复发）。
2. 枚举 drop 插在了 `drop_table('departments')` 之后、`drop_table('permissions')` 之前，
   结果 `permission_type` 还被 `permissions` 依赖 → `DependentObjectsStillExist`。
   **枚举必须在所有引用它的表删完之后再删。**

---

## 九、遗留

1. **没有登录态内的角色缓存**：每次权限校验都查库（3 张表 join），高频接口应加 Redis 缓存。
2. **没有角色继承**。
3. **审计只记了"谁调了哪个接口"**，没有记业务对象变更前后的值（做不了"谁把价格改了"）。
4. **没有 SSO / 密码策略 / 登录审计**（只记了写操作，登录失败没记）。
5. **菜单权限只有定义**，前端还没接（`type=menu` 的权限码已备好）。
