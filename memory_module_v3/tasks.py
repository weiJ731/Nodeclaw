from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from celery import Celery, Task
from pymongo import ReturnDocument
from redis import Redis

from .config import get_memory_config
from .models import utc_now
from .service import compact_session, process_exchange, reindex_user, sync_outbox_event
from .storage import get_database

config = get_memory_config()
celery_app = Celery("nodeclaw", broker=config.celery_broker_url, backend=config.celery_result_backend)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": 3600},
    task_routes={
        "memory_module_v3.tasks.process_exchange_task": {"queue": "memory"},
        "memory_module_v3.tasks.compact_session_task": {"queue": "summary"},
        "memory_module_v3.tasks.sync_outbox_task": {"queue": "index"},
        "memory_module_v3.tasks.scan_outbox_task": {"queue": "index"},
        "memory_module_v3.tasks.scan_scheduled_tasks": {"queue": "scheduler"},
        "memory_module_v3.tasks.deliver_scheduled_task": {"queue": "scheduler"},
        "memory_module_v3.tasks.reindex_user_task": {"queue": "index"},
    },
    beat_schedule={
        "scan-memory-outbox": {"task": "memory_module_v3.tasks.scan_outbox_task", "schedule": 5.0},
        "scan-scheduled-tasks": {"task": "memory_module_v3.tasks.scan_scheduled_tasks", "schedule": 5.0},
    },
)


class DeadLetterTask(Task):
    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        get_database().dead_letters.insert_one({
            "queue": self.name,
            "task_id": task_id,
            "args": list(args),
            "kwargs": kwargs,
            "error": str(exc)[:2000],
            "created_at": utc_now(),
        })
        if self.name == "memory_module_v3.tasks.sync_outbox_task" and args:
            get_database().outbox_events.update_one(
                {"event_id": args[0]},
                {"$set": {"status": "failed", "updated_at": utc_now(), "last_error": str(exc)[:1000]}},
            )
        super().on_failure(exc, task_id, args, kwargs, einfo)


@contextmanager
def user_lock(user_id: str, timeout: int = 180) -> Iterator[None]:
    redis = Redis.from_url(config.redis_url)
    lock = redis.lock(f"nodeclaw:memory:user:{user_id}", timeout=timeout, blocking_timeout=30)
    if not lock.acquire(blocking=True):
        raise RuntimeError("could not acquire user memory lock")
    try:
        yield
    finally:
        try:
            lock.release()
        except Exception:
            pass


@celery_app.task(bind=True, base=DeadLetterTask, autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=3)
def process_exchange_task(self, user_id: str, session_id: str, exchange_id: str):
    with user_lock(user_id):
        return process_exchange(user_id, session_id, exchange_id)


@celery_app.task(bind=True, base=DeadLetterTask, autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=3)
def compact_session_task(self, user_id: str, session_id: str):
    with user_lock(user_id):
        return compact_session(user_id, session_id)


@celery_app.task(bind=True, base=DeadLetterTask, autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=5)
def sync_outbox_task(self, event_id: str):
    return sync_outbox_event(event_id)


@celery_app.task
def scan_outbox_task():
    db = get_database()
    queued = 0
    for _ in range(100):
        row = db.outbox_events.find_one_and_update(
            {"status": "pending"},
            {"$set": {"status": "queued", "updated_at": utc_now()}},
            sort=[("created_at", 1)],
            return_document=ReturnDocument.AFTER,
        )
        if not row:
            break
        try:
            sync_outbox_task.delay(row["event_id"])
            queued += 1
        except Exception:
            db.outbox_events.update_one(
                {"_id": row["_id"], "status": "queued"},
                {"$set": {"status": "pending", "updated_at": utc_now()}},
            )
            raise
    return {"queued": queued}


@celery_app.task
def scan_scheduled_tasks():
    from nodeclaw.core.task_store import claim_due_tasks

    rows = claim_due_tasks()
    for row in rows:
        deliver_scheduled_task.delay(str(row["_id"]))
    return {"queued": len(rows)}


@celery_app.task(bind=True, base=DeadLetterTask, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def deliver_scheduled_task(self, object_id: str):
    from bson import ObjectId
    from nodeclaw.core.notifications import create_notification
    from nodeclaw.core.task_store import finish_task

    row = get_database().scheduled_tasks.find_one({"_id": ObjectId(object_id), "status": "processing"})
    if not row:
        return {"status": "skipped"}
    create_notification(
        row["user_id"],
        "reminder",
        row["description"],
        event_key=f"task:{row['task_id']}:{row['claim_id']}",
        task_id=row["task_id"],
        session_id=row.get("session_id"),
    )
    finish_task(row)
    return {"status": "delivered", "task_id": row["task_id"]}


@celery_app.task(bind=True, base=DeadLetterTask, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def reindex_user_task(self, user_id: str):
    return reindex_user(user_id)


def enqueue_exchange(user_id: str, session_id: str, exchange_id: str, needs_summary: bool) -> None:
    process_exchange_task.delay(user_id, session_id, exchange_id)
    if needs_summary:
        compact_session_task.delay(user_id, session_id)
