from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    APP_NAME: str = "FlashSalesPlatform"
    DEBUG: bool = False

    DATABASE_URL: str
    REDIS_URL: str

    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    # 缓存配置
    CACHE_PREFIX: str = "flash_sales:"
    CACHE_TTL_SECONDS: int = 300


    # 秒杀配置
    SECKILL_STOCK_PREFIX: str = "seckill:stock:"
    SECKILL_LIMIT_PREFIX: str = "seckill:limit:"
    RATE_LIMIT_PER_USER: int = 1
    SECKILL_QUEUE_NAME: str = "seckill_orders"  # Redis Stream 队列名

    # AI 配置（可选）
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-3.5-turbo"
    
    # 新的AI配置
    SC_NET_API_KEY: str = ""
    SC_NET_API_BASE: str = "https://api.scnet.cn/api/llm/v1"
    SC_NET_MODEL: str = "Qwen3-235B-A22B"

    # Kafka 配置
    KAFKA_BOOTSTRAP_SERVERS: str = ""
    KAFKA_ORDER_TOPIC: str = "orders"

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()