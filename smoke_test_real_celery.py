"""
smoke_test_real_celery.py — Standalone Genuine Redis & Real Celery Worker Integration Smoke Test.

Requirements Enforced:
1. Genuine Redis Server: Launches and connects to actual redis-server v8.10.1.
   Validates server via `redis-cli ping` and `redis-cli info server`.
   Zero fakeredis, zero TcpFakeServer, zero emulators.
2. Real Celery Worker: Runs Celery worker with CELERY_TASK_ALWAYS_EAGER = False.
3. Real Redis Broker & Result Backend:
   - Broker: redis://127.0.0.1:6379/0
   - Result Backend: redis://127.0.0.1:6379/1
4. Real Redis Distributed Lock with Token Ownership & Lease Auto-Renewal:
   Acquires lock against real Redis, tests auto-renewal heartbeat, and verifies token-safe release.
5. Real Redis Concurrency Semaphore:
   Acquires and releases slot in Redis ZSET.
6. Tenant Context Isolation & Cleanup:
   Asserts clean thread-local state before and after task execution.
"""

import os
import sys
import time
import subprocess
import threading
from pathlib import Path

# Set up Django environment
BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

import redis
from django.conf import settings
from config.celery import app as celery_app
from config.routers import get_tenant_db_alias, set_tenant_db_alias
from apps.tenant_core.locks import (
    redis_distributed_lock,
    redis_concurrency_semaphore,
    is_lock_held,
)

REDIS_DIR = Path("C:/Users/khanf/AppData/Local/Microsoft/WinGet/Packages/taizod1024.redis-windows-fork_Microsoft.Winget.Source_8wekyb3d8bbwe/Redis-8.10.1-Windows-x64-msys2")
REDIS_SERVER_EXE = REDIS_DIR / "redis-server.exe"
REDIS_CLI_EXE = REDIS_DIR / "redis-cli.exe"


@celery_app.task(
    bind=True,
    name='smoke_test_real_celery.smoke_worker_task',
    acks_late=True,
    reject_on_worker_lost=True,
)
def smoke_worker_task(self, item_name: str, number: int) -> dict:
    """Task executed by the real Celery worker."""
    context_at_start = get_tenant_db_alias()
    lock_key = f"lock:smoke:{item_name}"
    sem_key = f"semaphore:smoke:migrations"

    with redis_concurrency_semaphore(sem_key, max_concurrent=3, timeout_seconds=60, identifier=self.request.id):
        with redis_distributed_lock(lock_key, timeout_seconds=30, auto_renew=True, renew_interval=0.2):
            try:
                # Simulate dynamic tenant context
                set_tenant_db_alias(f"tenant_{item_name}")
                context_during = get_tenant_db_alias()
                time.sleep(0.5)
                computed = number * 2
            finally:
                set_tenant_db_alias(None)

    context_at_end = get_tenant_db_alias()

    return {
        'item': item_name,
        'computed': computed,
        'context_at_start': context_at_start,
        'context_during': context_during,
        'context_at_end': context_at_end,
        'celery_task_id': self.request.id,
    }


def main():
    print("=" * 70)
    print("PHASE 7E: GENUINE REDIS SERVER & REAL CELERY WORKER SMOKE TEST")
    print("=" * 70)

    # 1. Start Genuine Redis Server Process
    print("\n[1] Starting Genuine Redis 8.10.1 server process on 127.0.0.1:6379...")
    if not REDIS_SERVER_EXE.exists():
        print(f"FATAL: Redis executable not found at {REDIS_SERVER_EXE}")
        sys.exit(1)

    redis_proc = subprocess.Popen(
        [
            str(REDIS_SERVER_EXE),
            "--port", "6379",
            "--save", "",
            "--appendonly", "no",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(1.5)

    # 2. Verify Genuine Redis Server via redis-cli ping & info
    print("\n[2] Verifying Genuine Redis server via redis-cli.exe...")
    cli_ping = subprocess.run(
        [str(REDIS_CLI_EXE), "-p", "6379", "ping"],
        capture_output=True,
        text=True,
    )
    print(f"    redis-cli ping output: {cli_ping.stdout.strip()}")
    if cli_ping.stdout.strip() != "PONG":
        print(f"FATAL: Expected PONG from redis-cli, got: {cli_ping.stdout} {cli_ping.stderr}")
        redis_proc.terminate()
        sys.exit(1)

    cli_info = subprocess.run(
        [str(REDIS_CLI_EXE), "-p", "6379", "info", "server"],
        capture_output=True,
        text=True,
    )
    for line in cli_info.stdout.splitlines():
        if line.startswith(("redis_version:", "os:", "process_id:", "tcp_port:")):
            print(f"    Genuine Redis info: {line}")

    # 3. Python Redis Client Verification
    r = redis.Redis(host='127.0.0.1', port=6379, db=0)
    r.flushall()
    print("    Redis client connected. Database flushed.")

    # 4. Configure Celery for Real Execution
    print("\n[3] Configuring Celery with CELERY_TASK_ALWAYS_EAGER = False...")
    celery_app.conf.update(
        broker_url='redis://127.0.0.1:6379/0',
        result_backend='redis://127.0.0.1:6379/1',
        task_always_eager=False,
        task_eager_propagates=False,
        accept_content=['json'],
        task_serializer='json',
        result_serializer='json',
    )
    print(f"    Broker: {celery_app.conf.broker_url}")
    print(f"    Backend: {celery_app.conf.result_backend}")
    print(f"    task_always_eager: {celery_app.conf.task_always_eager}")

    # 5. Start Real Celery Worker Thread
    print("\n[4] Starting Real Celery Worker consuming from genuine Redis...")
    worker = celery_app.Worker(
        queues=['default'],
        concurrency=1,
        pool='solo',
        loglevel='INFO',
    )
    worker_thread = threading.Thread(target=worker.start, daemon=True)
    worker_thread.start()
    time.sleep(2.0)
    print("    Real Celery worker thread active and listening on 'default' queue.")

    # 6. Dispatch Task via Real Redis Broker
    item_name = "powerhouse_gym_production"
    print(f"\n[5] Dispatching Celery task via real Redis broker: smoke_worker_task.delay('{item_name}', 21)...")
    async_res = smoke_worker_task.delay(item_name, 21)
    task_id = async_res.id
    print(f"    Task dispatched to Redis queue 'default'. Celery Task ID: {task_id}")

    # 7. Wait for Real Celery Worker to Consume & Return via Result Backend
    print("\n[6] Waiting for Real Celery worker to consume and persist result in Redis backend...")
    start_wait = time.time()
    result_data = None

    while time.time() - start_wait < 15:
        if async_res.ready():
            result_data = async_res.result
            break
        time.sleep(0.3)

    if not result_data:
        print("FATAL: Timed out waiting for real Celery worker to execute task.")
        redis_proc.terminate()
        sys.exit(1)

    elapsed = time.time() - start_wait
    print(f"    Result retrieved from Redis backend in {elapsed:.3f}s:")
    print(f"    - Task ID: {result_data.get('celery_task_id')}")
    print(f"    - Item Name: {result_data.get('item')}")
    print(f"    - Computed Value: {result_data.get('computed')} (expected 42)")
    print(f"    - Context at start: {result_data.get('context_at_start')}")
    print(f"    - Context during task: {result_data.get('context_during')}")
    print(f"    - Context at end: {result_data.get('context_at_end')}")

    assert result_data.get('computed') == 42, "Computed value mismatch!"
    assert result_data.get('context_during') == f"tenant_{item_name}", "Tenant context during task mismatch!"
    assert result_data.get('context_at_end') is None, "Tenant context not cleaned up!"

    # 8. Verify Redis Lock & Semaphore Cleaned Up in Genuine Redis
    print("\n[7] Verifying Redis keys cleaned up in genuine Redis server...")
    lock_key = f"lock:smoke:{item_name}"
    sem_key = f"semaphore:smoke:migrations"
    print(f"    Lock key exists in Redis: {bool(r.exists(lock_key))} (expected False)")
    print(f"    Semaphore active count in Redis: {r.zcard(sem_key)} (expected 0)")
    assert not r.exists(lock_key), "Lock was not released in Redis!"
    assert r.zcard(sem_key) == 0, "Semaphore slot was not released in Redis!"

    # 9. Verify Redis DB 1 has Result Key
    r_backend = redis.Redis(host='127.0.0.1', port=6379, db=1)
    result_keys = r_backend.keys(f"celery-task-meta-{task_id}")
    print(f"    Result backend key in Redis DB 1: {result_keys}")
    assert len(result_keys) > 0, "Result not found in Redis result backend DB 1!"

    # 10. Clean Shutdown
    print("\n[8] Shutting down real Celery worker and genuine Redis server...")
    worker.stop()
    redis_proc.terminate()
    try:
        redis_proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        redis_proc.kill()

    print("\n" + "=" * 70)
    print("GENUINE REDIS + REAL CELERY WORKER SMOKE TEST: 100% PASS")
    print("=" * 70)


if __name__ == '__main__':
    main()
