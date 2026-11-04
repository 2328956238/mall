# -*- coding: utf-8 -*-
"""通过 GitHub Git Data API 推送 mall 分支（git push 在本机被重置）。"""
import base64, json, os, sys, time, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(r"C:\Users\23289\Documents\deepseek-harness\default-workspace\mall")
TOKEN = (Path(os.environ["TEMP"]) / "gh_tok5.txt").read_text().strip()
OWNER, REPO = "2328956238", "mall"
BRANCH = "feature/inventory-core"
API = "https://api.github.com"

EXCLUDE = ("\\.venv\\", "__pycache__", "\\.pgdata", "\\.pgtmp", "\\.git\\", ".pyc", "pytest_cache", ".pg_uri")


def call(method, path, body=None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                 "User-Agent": "dsh", "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:400]
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1)); continue
            raise SystemExit(f"{method} {path} -> {e.code} {detail}")
        except Exception as e:
            if attempt < 2:
                time.sleep(2); continue
            raise SystemExit(f"{method} {path} -> {e}")


files = []
for p in sorted(ROOT.rglob("*")):
    if not p.is_file():
        continue
    rel = p.relative_to(ROOT).as_posix()
    if any(tok.strip("\\") in rel for tok in EXCLUDE):
        continue
    files.append((rel, p.read_bytes()))
print(f"待上传 {len(files)} 个文件，共 {sum(len(d) for _, d in files)/1024:.1f} KB")

ref = call("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/main")
base_sha = ref["object"]["sha"]
base_commit = call("GET", f"/repos/{OWNER}/{REPO}/git/commits/{base_sha}")
print(f"main = {base_sha[:8]}")

entries = []
for rel, data in files:
    blob = call("POST", f"/repos/{OWNER}/{REPO}/git/blobs",
                {"content": base64.b64encode(data).decode(), "encoding": "base64"})
    entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
print("blob 上传完成")

tree = call("POST", f"/repos/{OWNER}/{REPO}/git/trees",
            {"base_tree": base_commit["tree"]["sha"], "tree": entries})

msg = """feat(phase1): 库存域 + 租户隔离 + 金额 Decimal（第 0~6 步）

在原秒杀平台上增量改造，不改架构、不重写、不删功能。

第 0 步 回归测试
- 真的跑在 PostgreSQL 16 上（嵌入式 pgserver），不是 SQLite
- 29 项锁住商品/订单/秒杀/鉴权/越权的现有行为
- 并发用例：10 人抢 5 件必须恰好 5 成功

第 1 步 Alembic baseline
- 引入 alembic，baseline 558c329ee208
- scripts/verify_schema.py：stamp 前先核对真实库与模型，有差异直接退出码 1
- 迁移手册 docs/MIGRATION.md（备份/回滚/downgrade 约定）

第 2 步 库存域 6 张表
- tenants / skus / warehouses / warehouse_stocks / stock_transactions / stock_reservations
- 可用量不落库（on_hand - reserved），DB 用 CHECK 兜底
- 6 个枚举类型在 downgrade 里显式 drop

第 3 步 InventoryService
- 入库/出库/预留/释放/核销/调拨/盘点
- 单条 UPDATE + WHERE 守卫，判断与写入同一持锁时刻
- 库存与流水同事务；幂等键重复提交返回上次快照

第 4 步 products.stock 降级为只读汇总
- InventoryService 从 warehouse_stocks 重算
- PUT /products 带 stock 返回 409

第 5 步 秒杀切库存域
- Redis DECR 只做前置拦截，真相在 warehouse_stocks
- 预留 → 占配额建单 → 核销，失败全链路补偿
- reconcile() 对账，drift 不为 0 打警告

第 6 步 回填与收紧
- 老表补 tenant_id 并回填默认租户，再收紧非空
- 金额 Float → Numeric(18,4)，API 序列化为字符串保精度，前端改 Number()
- 每商品生成 SKU + 默认仓库存 + 初始化流水，秒杀活动绑定库存域
- orders.seckill_product_id 改可空，为第三阶段普通订单让路

测试：87 项全绿（107s），含 4 条并发/不变量用例和 6 项真跑 alembic 的迁移测试。
迁移链：558c329ee208 → 4e87f06d6490 → 8ad9bed21515 → b7c1de9f4a20，均验证过
upgrade → verify → downgrade → upgrade。

文档：docs/PHASE1-DELIVERY.md、docs/PROBLEMS.md（17 条踩坑记录）、docs/MIGRATION.md
"""
commit = call("POST", f"/repos/{OWNER}/{REPO}/git/commits",
              {"message": msg, "tree": tree["sha"], "parents": [base_sha]})
print(f"commit = {commit['sha'][:8]}")

try:
    call("POST", f"/repos/{OWNER}/{REPO}/git/refs",
         {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
except SystemExit as e:
    if "422" in str(e):
        call("PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}",
             {"sha": commit["sha"], "force": True})
    else:
        raise
print(f"分支已推送: {BRANCH}")
print(f"https://github.com/{OWNER}/{REPO}/tree/{BRANCH}")
