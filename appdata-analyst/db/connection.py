import os

from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def create_pool(dsn: str | None = None) -> ConnectionPool:
    global _pool
    dsn = dsn or os.environ["DATABASE_URL"]
    _pool = ConnectionPool(dsn, min_size=1, max_size=5, open=True)
    return _pool


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized — call create_pool() first")
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
