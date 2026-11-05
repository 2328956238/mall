@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
echo.
echo   mall 本地启动（嵌入式 PostgreSQL + 假 Redis）
echo   不需要 Docker / 不需要预装 PostgreSQL / Redis
echo.
".venv\Scripts\python.exe" dev_start.py --port 8000
echo.
echo   服务已退出。若报错，看上面的输出；或者试 python dev_start.py --reset
pause
