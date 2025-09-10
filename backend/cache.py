# cache.py
import os
import redis.asyncio as aioredis
import json
import logging

log = logging.getLogger("agent-commerce")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
redis: aioredis.Redis | None = None

async def init_cache():
    global redis
    redis = aioredis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await redis.ping()
        log.info("Connected to Redis at %s", REDIS_URL)
    except Exception as e:
        log.error("Failed to connect to Redis: %s", e)
        redis = None

async def close_cache():
    global redis
    if redis:
        await redis.close()
        redis = None

async def get_cache(key: str):
    if not redis:
        return None
    value = await redis.get(key)
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value

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
