import logging

import redis.asyncio as redis

from app.config import settings

logger = logging.getLogger(__name__)

redis_client = None

# 假 Redis 只在「单进程 + 本地开发」下成立：
# 缓存不共享、限流不跨 worker、重启即丢。所以用显式开关控制，默认关。
_fake_client = None


async def get_redis():
    global redis_client, _fake_client
    if redis_client is not None:
        return redis_client

    if settings.USE_FAKE_REDIS:
        import fakeredis.aioredis

        # 同一个进程内复用一份，保证缓存/限流在不同请求之间还能看见彼此
        if _fake_client is None:
            _fake_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
            logger.warning(
                "USE_FAKE_REDIS=true：Redis 使用进程内假实现，仅限本地开发（多 worker 不共享）"
            )
        redis_client = _fake_client
        return redis_client

    redis_client = await redis.from_url(settings.REDIS_URL, decode_responses=True)
    return redis_client
