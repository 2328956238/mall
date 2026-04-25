import time
from redis.asyncio import Redis

async def token_bucket(redis: Redis, key: str, rate: float = 1, capacity: int = 1) -> bool:
    """令牌桶限流，返回 True 表示允许"""
    now = time.time()
    script = """
    local key = KEYS[1]
    local rate = tonumber(ARGV[1])
    local capacity = tonumber(ARGV[2])
    local now = tonumber(ARGV[3])

    local data = redis.call('hmget', key, 'last_time', 'tokens')
    local last_time = data[1] or now
    local tokens = tonumber(data[2]) or capacity

    local delta = math.max(0, now - last_time)
    local filled_tokens = math.min(capacity, tokens + delta * rate)

    if filled_tokens >= 1 then
        local new_tokens = filled_tokens - 1
        redis.call('hmset', key, 'last_time', now, 'tokens', new_tokens)
        redis.call('expire', key, 60)
        return 1
    else
        return 0
    end
    """
    res = await redis.eval(script, 1, key, rate, capacity, now)
    return res == 1