"""Optional asynchronous tool completion for continuously running scenarios."""

from concurrent.futures import ThreadPoolExecutor, wait

from tau2.config import DEFAULT_SCENARIO_DRAIN_SECONDS


class ToolExecutionQueue:
    """Execute each accepted request once while media ticks continue."""

    def __init__(self, environment):
        self.environment = environment
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="tau-tools"
        )
        self.pending = {}
        self.seen = set()
        self.closed = False

    def submit(self, calls):
        """Admit unique calls in their recorded order."""
        if self.closed:
            raise RuntimeError("Tool execution queue is closed")
        for call in calls:
            if call.id in self.seen:
                raise RuntimeError("Duplicate tool call; refusing to execute it twice")
            self.seen.add(call.id)
            future = self.executor.submit(self.environment.get_response, call)
            self.pending[call.id] = (call.requestor, future)

    def drain(self, requestor):
        """Collect completed results once, by participant and call identifier."""
        results = []
        for key, (role, future) in list(self.pending.items()):
            if role == requestor and future.done():
                results.append(future.result())
                del self.pending[key]
        return results

    def close(self):
        """Drain accepted work without abandoning an uncertain state mutation."""
        if self.closed:
            return
        self.closed = True
        futures = [future for _, future in self.pending.values()]
        _, running = (
            wait(futures, timeout=DEFAULT_SCENARIO_DRAIN_SECONDS)
            if futures
            else (set(), set())
        )
        self.executor.shutdown(wait=False, cancel_futures=True)
        if running:
            raise RuntimeError("Tool outcome remained uncertain during trial cleanup")
        for future in futures:
            future.result()
