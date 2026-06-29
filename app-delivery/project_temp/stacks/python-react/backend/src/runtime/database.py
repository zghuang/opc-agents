import asyncpg

from .settings import settings


pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(dsn=settings.database_url, min_size=1, max_size=5)
    return pool


async def check_db() -> None:
    connection_pool = await get_pool()
    async with connection_pool.acquire() as connection:
        await connection.fetchval("select 1")


async def close_db() -> None:
    global pool
    if pool is not None:
        await pool.close()
        pool = None