import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_WORKERS = int(os.getenv("AGENT_WORKER_THREADS", "1"))
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS)
_SEMAPHORE = threading.BoundedSemaphore(_WORKERS)


class AgentQueueFull(Exception):
    pass


def submit_agent_job(fn, *args, **kwargs):
    if not _SEMAPHORE.acquire(blocking=False):
        raise AgentQueueFull()
    future = _EXECUTOR.submit(fn, *args, **kwargs)
    future.add_done_callback(lambda _: _SEMAPHORE.release())
    return future


def shutdown_agent_executor():
    _EXECUTOR.shutdown(wait=False, cancel_futures=False)
