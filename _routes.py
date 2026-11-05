import os, sys
sys.path.insert(0, ".")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x/y")
os.environ.setdefault("REDIS_URL", "redis://x/0")
os.environ.setdefault("SECRET_KEY", "t")
from app.main import app

for tag in ("purchase", "sales"):
    rows = []
    for r in app.routes:
        if getattr(r, "tags", None) and tag in r.tags:
            methods = ",".join(sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS")))
            rows.append((methods, r.path))
    print(f"\n=== {tag} 接口 {len(rows)} 个 ===")
    for m, p in sorted(rows, key=lambda x: x[1]):
        print(f"  {m:6} {p}")
