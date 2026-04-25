from apscheduler.schedulers.background import BackgroundScheduler
from app.tasks.jobs import release_expired_seckill_stock, generate_daily_report

scheduler = BackgroundScheduler()

def start_scheduler():
    # 每5分钟检查一次秒杀过期库存释放（减少频率）
    scheduler.add_job(release_expired_seckill_stock, 'interval', minutes=5)
    # 每天凌晨1点生成日报
    scheduler.add_job(generate_daily_report, 'cron', hour=1, minute=0)
    scheduler.start()

def shutdown_scheduler():
    scheduler.shutdown()