"""Compatibility entry point for Nodeclaw's distributed heartbeat.

Production scheduling is driven by Celery Beat. This coroutine is retained for
embedded/CLI callers and only asks Celery to run the same atomic Mongo scanner;
it never reads or writes a second task store.
"""

from __future__ import annotations

import asyncio

from memory_module_v3.tasks import scan_scheduled_tasks


async def pacemaker_loop(task_queue: asyncio.Queue | None = None, check_interval: int = 10) -> None:
    del task_queue
    while True:
        await asyncio.sleep(check_interval)
        await asyncio.to_thread(scan_scheduled_tasks.delay)
