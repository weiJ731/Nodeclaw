from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from nodeclaw.core.heartbeat import pacemaker_loop
from nodeclaw.core.task_store import LOCAL_TZ, _next_occurrence


def test_repeat_occurrences_cover_supported_frequencies():
    current = datetime(2026, 1, 31, 9, 0, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    assert _next_occurrence(current, "hourly") > current
    assert _next_occurrence(current, "daily") > current
    assert _next_occurrence(current, "weekly") > current
    monthly = _next_occurrence(current, "monthly").astimezone(LOCAL_TZ)
    assert (monthly.year, monthly.month, monthly.day) == (2026, 2, 28)


def test_pacemaker_dispatches_the_celery_scanner():
    async def run_once():
        with patch("nodeclaw.core.heartbeat.scan_scheduled_tasks.delay") as delay:
            task = asyncio.create_task(pacemaker_loop(check_interval=0))
            for _ in range(20):
                await asyncio.sleep(0)
                if delay.called:
                    break
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert delay.called

    asyncio.run(run_once())
