import logging
import sys
from typing import Optional


def setup_logger(
    name: str = "flash_sales",
    level: int = logging.INFO,
    log_file: Optional[str] = None,
) -> logging.Logger:
    """配置并返回一个结构化日志记录器"""
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(filename)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台输出
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件输出（可选）
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


# 默认应用日志记录器
app_logger = setup_logger()


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """获取指定名称的日志记录器"""
    if name:
        return setup_logger(name)
    return app_logger