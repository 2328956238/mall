#!/bin/bash
# 初始化数据库（使用 alembic 或直接创建表）sudo 
sudo docker compose exec api python -c "from app.db.session import engine; from app.models.base import Base; import asyncio; asyncio.run(engine.run_sync(Base.metadata.create_all))"
echo "Database initialized"