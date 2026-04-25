"""
缓存工具模块 — 提供缓存穿透、缓存击穿、缓存雪崩防护

功能：
1. 缓存穿透防护：查不到DB时缓存空值(NullObject)，设置短TTL
2. 缓存击穿防护：互斥锁(Mutex)机制，防止热点key失效时大量请求穿透到DB
3. 缓存雪崩防护：基础过期时间 + 随机偏移，避免大量key同时过期
4. 统一缓存操作接口：get/set/delete 封装 Redis 操作
"""
import asyncio
import json
import random
from typing import Optional, Any, Callable, Awaitable, TypeVar, Union
from redis.asyncio import Redis
from app.db.redis_client import get_redis

T = TypeVar("T")

# 空值标记 — 用于缓存穿透时表示"DB中不存在"
_NULL_SENTINEL = object()

# 默认空值缓存TTL（秒），防止穿透攻击时长期占用缓存
NULL_CACHE_TTL = 30

# 缓存击穿互斥锁的 Redis key 前缀
MUTEX_LOCK_PREFIX = "cache:mutex:"

# 互斥锁超时时间（秒），防止死锁
MUTEX_LOCK_TIMEOUT = 5

# 基础缓存TTL + 随机偏移最大值（秒），用于防雪崩
CACHE_TTL_JITTER_MAX = 60


def _with_jitter(base_ttl: int, jitter_max: int = CACHE_TTL_JITTER_MAX) -> int:
    """
    防缓存雪崩：在基础TTL上增加随机偏移
    
    避免大量缓存key在同一时间集体过期，导致数据库瞬时压力暴增（雪崩）。
    
    Args:
        base_ttl: 基础过期时间（秒）
        jitter_max: 最大随机偏移（秒），默认60s
        
    Returns:
        int: 最终TTL = base_ttl + random(0, jitter_max)
    """
    return base_ttl + random.randint(0, jitter_max)


async def _get_redis() -> Redis:
    """获取 Redis 客户端实例"""
    return await get_redis()


async def _lock(key: str, redis: Optional[Redis] = None) -> bool:
    """
    获取分布式互斥锁 — 防缓存击穿
    
    使用 Redis SET NX 实现，只有一个请求能获取到锁去重建缓存。
    
    Args:
        key: 锁的key（对应需要重建的缓存key）
        redis: Redis客户端（可选，不传则自动获取）
        
    Returns:
        bool: 是否成功获取锁
    """
    if redis is None:
        redis = await _get_redis()
    lock_key = f"{MUTEX_LOCK_PREFIX}{key}"
    result = await redis.set(lock_key, "1", nx=True, ex=MUTEX_LOCK_TIMEOUT)
    return result is not None and result is True


async def _unlock(key: str, redis: Optional[Redis] = None) -> None:
    """释放分布式互斥锁"""
    if redis is None:
        redis = await _get_redis()
    lock_key = f"{MUTEX_LOCK_PREFIX}{key}"
    await redis.delete(lock_key)


async def safe_get(
    cache_key: str,
    fetch_func: Callable[[], Awaitable[Optional[T]]],
    ttl: int = 300,
    use_mutex: bool = True,
    encode_func: Optional[Callable[[T], str]] = None,
    decode_func: Optional[Callable[[str], T]] = None,
    redis: Optional[Redis] = None,
) -> Optional[T]:
    """
    带三重防护的缓存读取操作
    
    整合了缓存穿透、缓存击穿、缓存雪崩三种防护策略。
    
    Args:
        cache_key: Redis缓存key
        fetch_func: 当缓存未命中时，从数据源获取数据的异步函数（无参数，用闭包捕获参数）
        ttl: 基础缓存过期时间（秒），实际会加上随机偏移防雪崩
        use_mutex: 是否启用互斥锁防缓存击穿（热点key建议开启）
        encode_func: 自定义序列化函数（默认 json.dumps）
        decode_func: 自定义反序列化函数（默认 json.loads）
        redis: Redis客户端（可选）
        
    Returns:
        Optional[T]: 如果数据存在返回数据，否则返回 None
        
    Example:
        ```python
        # 推荐用法 — 闭包捕获参数
        async def _load():
            return await db.execute(...)
        product = await safe_get(
            cache_key=f"product:{product_id}",
            fetch_func=_load,
            ttl=300,
            use_mutex=True,
        )
        ```
    """
    if redis is None:
        redis = await _get_redis()
    
    if encode_func is None:
        encode_func = _default_encode
    if decode_func is None:
        decode_func = _default_decode
    
    # ---------------- 第一步：尝试从缓存读取 ----------------
    cached = await redis.get(cache_key)
    
    if cached is not None:
        # 命中缓存
        decoded = decode_func(cached)
        # 检查是否是空值标记（缓存穿透防护）
        if decoded is None:
            return None
        return decoded
    
    # ---------------- 第二步：缓存未命中 ----------------
    if use_mutex:
        # ========== 防缓存击穿：互斥锁机制 ==========
        locked = await _lock(cache_key, redis)
        if not locked:
            # 其他请求正在重建缓存，短暂等待后重试读取
            await asyncio.sleep(0.05)
            cached = await redis.get(cache_key)
            if cached is not None:
                decoded = decode_func(cached)
                return None if decoded is None else decoded
            # 再等一次
            await asyncio.sleep(0.1)
            cached = await redis.get(cache_key)
            if cached is not None:
                decoded = decode_func(cached)
                return None if decoded is None else decoded
            return None
    
    # ========== 防缓存穿透：查DB ==========
    try:
        data = await fetch_func()
    except Exception:
        # 查询异常时释放锁
        if use_mutex:
            await _unlock(cache_key, redis)
        raise
    
    # ---------------- 第三步：写入缓存 ----------------
    final_ttl = _with_jitter(ttl)
    
    if data is None:
        # ========== 缓存穿透防护：缓存空值（短TTL） ==========
        # 防止恶意请求查询不存在的ID导致大量DB查询
        await redis.setex(cache_key, NULL_CACHE_TTL, encode_func(None))
    else:
        # 正常数据，使用带随机偏移的TTL防雪崩
        await redis.setex(cache_key, final_ttl, encode_func(data))
    
    # 释放互斥锁
    if use_mutex:
        await _unlock(cache_key, redis)
    
    return data


async def safe_get_batch(
    cache_prefix: str,
    ids: list[Any],
    fetch_func: Callable[[list[Any]], Awaitable[list]],
    ttl: int = 300,
    redis: Optional[Redis] = None,
) -> dict[Any, Any]:
    """
    批量缓存读取 — 一次获取多个ID对应的数据
    
    适用于 "获取多个产品" 的场景，减少网络请求次数。
    对未命中的ID批量回源查询，再逐个写入缓存。
    
    Args:
        cache_prefix: 缓存key前缀，例如 "product"
        ids: ID列表
        fetch_func: 批量查询函数，接收 ids(list) 参数
        ttl: 缓存过期时间（秒）
        redis: Redis客户端
        
    Returns:
        dict[Any, Any]: {id: data} 映射
        
    Example:
        ```python
        result = await safe_get_batch(
            "product",
            [1, 2, 3],
            lambda ids: product_service.get_products_by_ids(ids),
        )
        ```
    """
    if redis is None:
        redis = await _get_redis()
    
    # 构建完整的缓存key列表
    cache_keys = {str(id): f"{cache_prefix}:{id}" for id in ids}
    result_map: dict[Any, Any] = {}
    missing_ids: list[Any] = []
    
    # 批量查缓存
    pipeline = redis.pipeline()
    for cache_key in cache_keys.values():
        await pipeline.get(cache_key)
    cached_values = await pipeline.execute()
    
    for id_val, cached in zip(ids, cached_values):
        if cached is not None:
            decoded = _default_decode(cached)
            if decoded is not None:
                result_map[id_val] = decoded
            # 空值缓存也表示为None，不加入 missing_ids
        else:
            missing_ids.append(id_val)
    
    # 回源查询未命中的ID
    if missing_ids:
        fetched_data = await fetch_func(missing_ids)
        
        # 建立 id -> data 映射（假设返回列表每个元素有 id 属性）
        fetched_map: dict[Any, Any] = {}
        for item in fetched_data:
            item_id = getattr(item, "id", None)
            if item_id is not None:
                fetched_map[item_id] = item
        
        # 写入缓存
        pipeline = redis.pipeline()
        for id_val in missing_ids:
            cache_key = cache_keys[str(id_val)]
            if id_val in fetched_map:
                data = fetched_map[id_val]
                await pipeline.setex(cache_key, _with_jitter(ttl), _default_encode(data))
                result_map[id_val] = data
            else:
                # 不存在的ID：缓存空值防穿透
                await pipeline.setex(cache_key, NULL_CACHE_TTL, _default_encode(None))
        await pipeline.execute()
    
    return result_map


async def safe_set(
    cache_key: str,
    data: Any,
    ttl: int = 300,
    encode_func: Optional[Callable[[Any], str]] = None,
    redis: Optional[Redis] = None,
) -> None:
    """
    写入缓存（带雪崩防护）
    
    触发场景：数据更新时主动刷新缓存，确保缓存与DB一致。
    
    Args:
        cache_key: 缓存key
        data: 要缓存的数据
        ttl: 基础过期时间（秒），会自动加随机偏移
        encode_func: 自定义序列化函数
        redis: Redis客户端
    """
    if redis is None:
        redis = await _get_redis()
    if encode_func is None:
        encode_func = _default_encode
    
    final_ttl = _with_jitter(ttl)
    await redis.setex(cache_key, final_ttl, encode_func(data))


async def safe_delete(
    cache_key: str,
    redis: Optional[Redis] = None,
) -> None:
    """
    删除缓存
    
    触发场景：数据更新/删除时，删除旧缓存保证一致性。
    
    Args:
        cache_key: 缓存key
        redis: Redis客户端
    """
    if redis is None:
        redis = await _get_redis()
    await redis.delete(cache_key)


async def safe_delete_pattern(
    pattern: str,
    redis: Optional[Redis] = None,
) -> int:
    """
    模糊匹配删除缓存
    
    触发场景：批量数据变更时，清除相关前缀的所有缓存。
    例如 safe_delete_pattern("product:*")
    
    Args:
        pattern: 匹配模式，例如 "product:*"
        redis: Redis客户端
        
    Returns:
        int: 删除的key数量
    """
    if redis is None:
        redis = await _get_redis()
    
    cursor = 0
    deleted_count = 0
    
    while True:
        cursor, keys = await redis.scan(cursor=cursor, match=pattern, count=100)
        if keys:
            await redis.delete(*keys)
            deleted_count += len(keys)
        if cursor == 0:
            break
    
    return deleted_count


# ========== 默认序列化/反序列化工具 ==========

def _default_encode(data: Any) -> str:
    """
    默认序列化函数
    
    处理空值标记、ORM对象、Pydantic模型等。
    """
    if data is None:
        return json.dumps(None)
    
    if hasattr(data, "model_dump"):
        # Pydantic v2 模型
        return json.dumps(data.model_dump(), default=str, ensure_ascii=False)
    
    if hasattr(data, "to_dict"):
        return json.dumps(data.to_dict(), default=str, ensure_ascii=False)
    
    # 通用对象：尝试提取 __dict__
    if hasattr(data, "__dict__"):
        return json.dumps(data.__dict__, default=str, ensure_ascii=False)
    
    return json.dumps(data, default=str, ensure_ascii=False)


def _default_decode(data_str: str) -> Any:
    """
    默认反序列化函数
    """
    obj = json.loads(data_str)
    # None -> None（空值标记）
    if obj is None:
        return None
    return obj