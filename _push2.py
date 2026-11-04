# -*- coding: utf-8 -*-
"""推送第二阶段：只上传变更文件，其余复用已有 blob（减少请求、降低断线概率）。"""
import base64, hashlib, json, os, time, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(r"C:\Users\23289\Documents\deepseek-harness\default-workspace\mall")
TOKEN = (Path(os.environ["TEMP"]) / "gh_tok5.txt").read_text().strip()
OWNER, REPO = "2328956238", "mall"
BASE_BRANCH = "feature/inventory-core"
BRANCH = "feature/purchase"
API = "https://api.github.com"
EXCLUDE = ("\\.venv\\", "__pycache__", "\\.pgdata", "\\.pgtmp", "\\.git\\", ".pyc", "pytest_cache", ".pg_uri")


def call(method, path, body=None, retries=5):
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            API + path, method=method, data=data,
            headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                     "User-Agent": "dsh", "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1)); last = f"{e.code} {detail}"; continue
            raise SystemExit(f"{method} {path} -> {e.code} {detail}")
        except Exception as e:
            last = repr(e)
            time.sleep(min(2 ** attempt, 15))
    raise SystemExit(f"{method} {path} 多次失败: {last}")


def git_blob_sha(data: bytes) -> str:
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


ref = call("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/{BASE_BRANCH}")
base_sha = ref["object"]["sha"]
base_commit = call("GET", f"/repos/{OWNER}/{REPO}/git/commits/{base_sha}")
remote_tree_sha = base_commit["tree"]["sha"]
remote = call("GET", f"/repos/{OWNER}/{REPO}/git/trees/{remote_tree_sha}?recursive=1")
remote_map = {e["path"]: e["sha"] for e in remote["tree"] if e["type"] == "blob"}
remote_paths = set(remote_map)
print(f"基线 {BASE_BRANCH} = {base_sha[:8]}，远端 {len(remote_map)} 个文件")

local = {}
for p in sorted(ROOT.rglob("*")):
    if not p.is_file():
        continue
    rel = p.relative_to(ROOT).as_posix()
    if any(tok.strip("\\") in rel for tok in EXCLUDE):
        continue
    local[rel] = p.read_bytes()

changed = [r for r, d in local.items() if remote_map.get(r) != git_blob_sha(d)]
deleted = sorted(remote_paths - set(local))
print(f"本地 {len(local)} 个，需要上传 {len(changed)} 个，删除 {len(deleted)} 个")

entries = []
for rel in changed:
    data = local[rel]
    blob = call("POST", f"/repos/{OWNER}/{REPO}/git/blobs",
                {"content": base64.b64encode(data).decode(), "encoding": "base64"})
    entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    print(f"  + {rel}")
for rel in deleted:
    entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": None})
    print(f"  - {rel}")

tree = call("POST", f"/repos/{OWNER}/{REPO}/git/trees",
            {"base_tree": remote_tree_sha, "tree": entries})

msg = """feat(phase2): 采购管理（供应商 / 采购单 / 收货入库）

在库存域之上做采购链路，不重复实现库存逻辑。

模型（5 张表）
- suppliers / purchase_orders / purchase_order_items
- purchase_receipts / purchase_receipt_items
- 金额 Numeric(18,4)，枚举存 value
- (tenant_id, order_no) / (tenant_id, receipt_no) / (tenant_id, idempotency_key) 唯一
- purchase_order_items 带 CHECK：qty>0、received_qty<=qty

状态机
draft → submitted → approved → receiving → completed
未收货前可 cancelled；submitted 可 rejected
非法流转抛 InvalidTransition（HTTP 409），集中在 ALLOWED 一处定义

收货入库（复用第一阶段）
- confirm_receipt 逐条调用 InventoryService.inbound
- 幂等键 receipt:{receipt_id}:item:{po_item_id}，重复确认不会二次入库
- 支持部分收货：收一部分 → receiving，收完 → completed
- 收货后自动同步 products.stock 并写 inbound 流水

API
- /api/v1/purchase/suppliers
- /api/v1/purchase/orders（+ submit/approve/reject/cancel）
- /api/v1/purchase/orders/{id}/receipts（生成入库任务）
- /api/v1/purchase/receipts/{id}/confirm
- 业务异常统一转 {detail, code}

迁移 f8a3a6adda45：已验证 upgrade → verify（16 表一致）→ downgrade → upgrade
测试：新增 17 项，全量 104 项全绿（147s）
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
