FROM python:3.11-slim

WORKDIR /app

# 复制依赖文件（requirements.txt 在项目根目录）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制应用代码（保持 app/ 子包结构）
# build context 是项目根目录，COPY app/ /app/app/ 将 app/ 目录复制到容器的 /app/app/
COPY app/ app/

EXPOSE 8000

# PYTHONPATH=/app 使得 from app.config import settings 能解析到 /app/app/config.py
ENV PYTHONPATH=/app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
