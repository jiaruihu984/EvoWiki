from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="evowiki-llm")


class DeadlineExceeded(TimeoutError):
    pass


def call_with_deadline[T](fn: Callable[[], T], seconds: float) -> T:
    """Run fn on a worker thread; give up waiting after `seconds`. The call itself keeps
    running, so a late LLM result is still logged by the Gateway and cached for next time."""
    fut = _EXECUTOR.submit(fn)
    try:
        return fut.result(timeout=seconds)
    except FuturesTimeout as exc:
        raise DeadlineExceeded(f"no result within {seconds:.2f}s") from exc
