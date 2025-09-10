# cache.py
import os
import aioredis
import json
import logging

log = logging.getLogger("agent-commerce")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
redis = None

async def init_cache():
    global redis
    redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    log.info("Connected to Redis at %s", REDIS_URL)

async def close_cache():
    if redis:
        await redis.close()

async def get_cache(key: str):
    if not redis:
        return None
    return await redis.get(key)

async def set_cache(key: str, value, ttl: int = 300):
    if not redis:
        return
    if isinstance(value, (dict, list)):
        value = json.dumps(value)
    await redis.set(key, value, ex=ttl)

async def clear_cache():
    if not redis:
        return
    await redis.flushdb()
    log.info("Cache cleared")
