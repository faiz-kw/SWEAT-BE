"""
apps/tenant_core/locks.py — Production-Grade Redis Distributed Locking & Concurrency Control.

Provides:
- redis_distributed_lock: Context manager for safe distributed locking with
  UUID token ownership, atomic Lua script release, and background lease auto-renewal.
- redis_concurrency_semaphore: Distributed Redis semaphore for global bounded task concurrency.
- LockAcquisitionError: Raised when a distributed lock cannot be acquired.
- ConcurrencyLimitExceeded: Raised when distributed concurrency limit is reached.
- is_lock_held: Inspects whether a lock key is currently held in Redis.
"""

import sys
import time
import uuid
import logging
import threading
from contextlib import contextmanager
import redis
from django.conf import settings

logger = logging.getLogger(__name__)


class LockAcquisitionError(Exception):
    """Raised when a distributed lock cannot be acquired."""
    pass


class ConcurrencyLimitExceeded(Exception):
    """Raised when a distributed concurrency limit is reached."""
    pass


def get_redis_client() -> redis.Redis:
    """
    Returns an active redis.Redis client configured from settings.REDIS_URL.
    
    Execution semantics:
    - In test runner mode ('test' in sys.argv): tries connecting to real Redis;
      if unreachable, falls back to an in-memory fakeredis instance so tests remain deterministic.
    - Outside test mode (production / staging / local dev): strictly connects to real Redis
      and fails fast with redis.ConnectionError if unavailable. Never silently falls back.
    """
    redis_url = getattr(settings, 'REDIS_URL', 'redis://localhost:6379/0')
    if 'test' in getattr(sys, 'argv', []):
        try:
            client = redis.from_url(redis_url, socket_connect_timeout=0.2)
            client.ping()
            return client
        except Exception:
            if not hasattr(get_redis_client, '_fake_client'):
                import fakeredis
                get_redis_client._fake_client = fakeredis.FakeRedis()
            return get_redis_client._fake_client

    return redis.from_url(redis_url)


@contextmanager
def redis_distributed_lock(
    lock_key: str,
    timeout_seconds: int = 600,
    blocking: bool = False,
    auto_renew: bool = False,
    renew_interval: float = None,
):
    """
    Context manager that acquires a distributed lock in Redis.

    Guarantees:
    1. Safe Ownership: Generates a unique UUID token on acquisition.
    2. Atomic Release: Releases using Lua script ONLY if the token in Redis
       matches the acquired token, preventing a worker from releasing another worker's
       lock if the original lock timed out or was reacquired.
    3. Lease Heartbeat / Auto-Renewal: If auto_renew=True, runs a background daemon thread
       that periodically extends TTL via token-checked reacquire() while the task executes.
       If the worker crashes, the thread dies with it, allowing normal TTL expiry.
    4. Fail-Closed: Raises LockAcquisitionError if lock cannot be acquired.
    """
    client = get_redis_client()
    lock = client.lock(name=lock_key, timeout=timeout_seconds, blocking=blocking)
    acquired = lock.acquire(blocking=blocking)

    if not acquired:
        raise LockAcquisitionError(
            f"Failed to acquire distributed lock for '{lock_key}'. Another worker or process holds this lock."
        )

    logger.debug("Acquired distributed lock for '%s' (timeout=%ds, auto_renew=%s)", lock_key, timeout_seconds, auto_renew)
    
    stop_renew = threading.Event()
    renew_thread = None

    if auto_renew:
        interval = renew_interval if renew_interval is not None else max(1.0, timeout_seconds / 3.0)
        owner_token = getattr(lock.local, 'token', None)

        def _renewal_heartbeat():
            if owner_token is not None:
                lock.local.token = owner_token
            while not stop_renew.wait(interval):
                try:
                    extended = lock.reacquire()
                    if not extended:
                        logger.warning("Failed to extend lease for lock '%s' (ownership lost or expired)", lock_key)
                        break
                    logger.debug("Successfully extended lease for lock '%s'", lock_key)
                except Exception as exc:
                    logger.warning("Heartbeat error extending lease for lock '%s': %s", lock_key, exc)
                    break

        renew_thread = threading.Thread(
            target=_renewal_heartbeat,
            daemon=True,
            name=f"lock-heartbeat-{lock_key}",
        )
        renew_thread.start()

    try:
        yield lock
    finally:
        if renew_thread is not None:
            stop_renew.set()
            renew_thread.join(timeout=1.0)

        try:
            lock.release()
            logger.debug("Released distributed lock for '%s'", lock_key)
        except redis.exceptions.LockNotOwnedError:
            logger.warning(
                "Distributed lock for '%s' was already released, expired, or claimed by another worker; cannot release unowned lock.",
                lock_key
            )
        except Exception as exc:
            logger.warning("Error releasing distributed lock for '%s': %s", lock_key, exc)


@contextmanager
def redis_concurrency_semaphore(
    semaphore_key: str,
    max_concurrent: int,
    timeout_seconds: int = 300,
    identifier: str = None,
):
    """
    Distributed Redis semaphore to enforce global bounded concurrency across all workers.

    Mechanism:
    - Uses a Redis sorted set (ZSET) where score = expiry timestamp (now + timeout).
    - Atomically clears expired slots.
    - If active slot count < max_concurrent, registers the slot and yields.
    - If at or above max_concurrent, raises ConcurrencyLimitExceeded.
    - Releases slot unconditionally in finally block.
    """
    client = get_redis_client()
    slot_id = identifier or str(uuid.uuid4())
    now = time.time()
    expires_at = now + timeout_seconds

    # Atomic Lua script for acquiring a semaphore slot
    acquire_lua = """
    local sem_key = KEYS[1]
    local now_ts = tonumber(ARGV[1])
    local exp_ts = tonumber(ARGV[2])
    local limit = tonumber(ARGV[3])
    local token = ARGV[4]
    local key_ttl = tonumber(ARGV[5])

    -- Remove expired slots
    redis.call('ZREMRANGEBYSCORE', sem_key, '-inf', now_ts)

    -- Check capacity
    local current = redis.call('ZCARD', sem_key)
    if current < limit then
        redis.call('ZADD', sem_key, exp_ts, token)
        redis.call('EXPIRE', sem_key, key_ttl)
        return 1
    else
        return 0
    end
    """
    try:
        acquired = client.eval(acquire_lua, 1, semaphore_key, now, expires_at, max_concurrent, slot_id, timeout_seconds + 60)
    except Exception as eval_err:
        # Fallback for mock/plain clients without eval
        logger.debug("Evaluating fallback semaphore logic for '%s': %s", semaphore_key, eval_err)
        client.zremrangebyscore(semaphore_key, '-inf', now)
        current = client.zcard(semaphore_key)
        if current < max_concurrent:
            client.zadd(semaphore_key, {slot_id: expires_at})
            acquired = 1
        else:
            acquired = 0

    if not acquired:
        raise ConcurrencyLimitExceeded(
            f"Distributed concurrency limit of {max_concurrent} reached for '{semaphore_key}'."
        )

    logger.debug("Acquired concurrency slot in '%s' (slot_id=%s, limit=%d)", semaphore_key, slot_id, max_concurrent)
    try:
        yield slot_id
    finally:
        try:
            client.zrem(semaphore_key, slot_id)
            logger.debug("Released concurrency slot in '%s' (slot_id=%s)", semaphore_key, slot_id)
        except Exception as exc:
            logger.warning("Error releasing concurrency slot '%s' in '%s': %s", slot_id, semaphore_key, exc)


def is_lock_held(lock_key: str) -> bool:
    """Check if a given lock key currently exists in Redis."""
    client = get_redis_client()
    try:
        return bool(client.exists(lock_key))
    except Exception as exc:
        logger.warning("Error inspecting lock '%s': %s", lock_key, exc)
        return False
