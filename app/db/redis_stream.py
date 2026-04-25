import json
from redis.asyncio import Redis

async def add_to_stream(redis: Redis, stream_name: str, data: dict):
    """添加消息到 Stream"""
    await redis.xadd(stream_name, {b'data': json.dumps(data)})

async def consume_stream(redis: Redis, stream_name: str, group_name: str, consumer_name: str, count=1):
    """消费者组读取消息（用于后台任务）"""
    try:
        # 创建消费者组（如果不存在）
        await redis.xgroup_create(stream_name, group_name, id='0', mkstream=True)
    except:
        pass
    result = await redis.xreadgroup(group_name, consumer_name, {stream_name: '>'}, count=count, block=0)
    return result