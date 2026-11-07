"""Managed JSONL subprocess pool for optional SAASR GPU synthesis."""

import atexit
import itertools
import json
import queue
import selectors
import subprocess
import tempfile
import threading
import uuid
from concurrent.futures import Future
from pathlib import Path

from tau2.data_model.saasr import SaasrConfig


class SaasrWorkerError(RuntimeError):
    """Synthesis infrastructure failed; never substitute a different renderer."""


class Worker:
    """Serialize inference and prioritize foreground over queued scene preparation."""

    def __init__(self, command, device, directory):
        self.device = device
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log = (self.directory / "worker.log").open("ab")
        command = [
            arg.replace("{worker_directory}", str(self.directory)) for arg in command
        ]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.log,
                bufsize=0,
            )
        except BaseException:
            self.log.close()
            raise
        self.queue = queue.PriorityQueue()
        self.counter = itertools.count()
        self.closed = False
        self.lock = threading.Lock()
        self.reader = bytearray()
        self.thread = threading.Thread(target=self._run, daemon=True, name="tau-saasr")
        self.thread.start()

    def submit(self, payload, timeout, priority=0):
        """Submit exactly once; return a cancellable queue admission."""
        future = Future()
        with self.lock:
            if self.closed:
                raise SaasrWorkerError("SAASR worker is closed")
            self.queue.put((priority, next(self.counter), future, payload, timeout))
        return future

    def _reply(self, timeout):
        import time

        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b"\n" not in self.reader:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    self._terminate()
                    raise SaasrWorkerError("SAASR worker request timed out")
                block = self.process.stdout.read(65536)
                if not block:
                    raise SaasrWorkerError("SAASR worker exited during a request")
                self.reader.extend(block)
                if len(self.reader) > 16 * 1024 * 1024:
                    raise SaasrWorkerError("SAASR worker reply exceeded protocol limit")
        line, _, rest = self.reader.partition(b"\n")
        self.reader = bytearray(rest)
        return json.loads(line)

    def _run(self):
        while True:
            _, _, future, payload, timeout = self.queue.get()
            try:
                if payload is None:
                    return
                if not future.set_running_or_notify_cancel():
                    continue
                identifier = str(uuid.uuid4())
                request = {
                    **payload,
                    "id": identifier,
                    "device": self.device,
                    "worker_directory": str(self.directory),
                }
                data = memoryview((json.dumps(request) + "\n").encode())
                while data:
                    sent = self.process.stdin.write(data)
                    if not sent:
                        raise SaasrWorkerError("SAASR worker input closed")
                    data = data[sent:]
                self.process.stdin.flush()
                reply = self._reply(timeout)
                if reply.get("id") != identifier:
                    raise SaasrWorkerError("Mismatched SAASR worker response ID")
                if "error" in reply:
                    error = reply["error"]
                    raise SaasrWorkerError(f"SAASR {error['type']}: {error['message']}")
                future.set_result(reply["result"])
            except Exception as error:
                if not future.done():
                    future.set_exception(SaasrWorkerError(str(error)))
            finally:
                self.queue.task_done()

    def close(self):
        """Terminate only the owned child and settle all queued futures."""
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self._terminate()
            self.queue.put((-100, next(self.counter), Future(), None, 0))
        self.thread.join(timeout=6)
        while True:
            try:
                _, _, future, _, _ = self.queue.get_nowait()
                future.cancel()
                self.queue.task_done()
            except queue.Empty:
                break
        self.process.stdin.close()
        self.process.stdout.close()
        self.log.close()

    def _terminate(self):
        # Give a container launcher time to stop its owned container before
        # killing the launcher itself; killing only docker's client leaks GPUs.
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


class WorkerPool:
    """Share resident models across trials, keeping scene playback state elsewhere."""

    def __init__(self, config: SaasrConfig):
        self.config = config
        self.directory = tempfile.TemporaryDirectory(prefix="tau-saasr-workers-")
        self.workers = []
        self.sessions = set()
        self.session_lock = threading.Lock()
        try:
            for i, (command, device) in enumerate(
                zip(config.worker_commands, config.devices)
            ):
                self.workers.append(
                    Worker(command, device, Path(self.directory.name) / str(i))
                )
            self.identities = [
                w.submit({"op": "hello"}, config.request_timeout_seconds).result()
                for w in self.workers
            ]
            if any(i.get("protocol") != 1 for i in self.identities):
                raise SaasrWorkerError("Unsupported SAASR worker protocol")
            if any(i != self.identities[0] for i in self.identities):
                raise SaasrWorkerError("SAASR workers must use identical software")
        except BaseException:
            self.close()
            raise

    def submit(self, payload, *, prepare=False):
        """Choose the least queued worker; every request includes immutable scene inputs."""
        available = [worker for worker in self.workers if worker.process.poll() is None]
        if not available:
            raise SaasrWorkerError("No SAASR synthesis worker is running")
        worker = min(available, key=lambda w: w.queue.unfinished_tasks)
        timeout = (
            self.config.prepare_timeout_seconds
            if prepare
            else self.config.request_timeout_seconds
        )
        return worker.submit(payload, timeout, priority=10 if prepare else 0)

    def close(self):
        """Release all model processes owned by this pool."""
        with self.session_lock:
            sessions = list(self.sessions)
        for session in sessions:
            session.close()
        for worker in self.workers:
            worker.close()
        self.directory.cleanup()

    def register_session(self, session):
        """Retain unfinished startup resources until batch cleanup, even after an exception."""
        with self.session_lock:
            self.sessions.add(session)

    def release_session(self, session):
        """Forget a normally closed call without affecting other callers."""
        with self.session_lock:
            self.sessions.discard(session)


_pools = {}
_lock = threading.Lock()


def get_pool(config: SaasrConfig) -> WorkerPool:
    """Get one process-local GPU pool without importing model dependencies into τ."""
    key = config.model_dump_json()
    with _lock:
        if key not in _pools:
            _pools[key] = WorkerPool(config)
        return _pools[key]


def close_pools():
    """Close pools at runner shutdown or interpreter exit."""
    with _lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()


atexit.register(close_pools)
