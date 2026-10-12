"""Budgets for an acquisition, and the bounded way a child process is run under one.

Disk monitoring samples at most every 50 ms and after child exit. A writing child
can overshoot a disk budget during that interval; this is not a kernel disk quota.
No result is accepted after any observed budget exhaustion.
"""
from dataclasses import dataclass, fields
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
import threading
from collections.abc import Callable


class BudgetExceeded(RuntimeError):
    """Acquisition is incomplete and must not be treated as an inventory."""

    def __init__(self, message: str):
        super().__init__(message)
        if "deadline" in message:
            self.code = "upstream_timeout"
        elif "concurrency" in message or "in progress" in message:
            self.code = "source_busy"
        elif message in {"acquisition cancelled", "unsafe cache entry"}:
            self.code = "acquisition_refused"
        else:
            self.code = "source_limit_exceeded"


@dataclass(frozen=True)
class AcquisitionLimits:
    operation_seconds: float = 300
    command_seconds: float = 60
    package_bytes: int = 100 * 1024 * 1024
    file_bytes: int = 10 * 1024 * 1024
    files: int = 2000
    refs: int = 10000
    tree_entries: int = 100000
    path_bytes: int = 4096
    nesting: int = 64
    acquired_bytes: int = 128 * 1024 * 1024
    expanded_bytes: int = 256 * 1024 * 1024
    cache_bytes: int = 512 * 1024 * 1024
    history_commits: int = 4096
    per_tenant: int = 4
    per_process: int = 8
    stdout_bytes: int = 32 * 1024 * 1024
    stderr_bytes: int = 64 * 1024

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError('source budgets must be finite and positive')
            if not field.name.endswith('_seconds') and not isinstance(value, int):
                raise ValueError('source count and byte budgets must be integers')


def disk_bytes(root: Path) -> int:
    total = 0
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(parent) / name
            if path.is_symlink():
                raise BudgetExceeded('unsafe cache entry')
        for name in files:
            try:
                total += (Path(parent) / name).stat().st_size
            except FileNotFoundError:
                continue
    return total


class Budget:
    def __init__(self, limits: AcquisitionLimits, *, cancelled: Callable[[], bool] = lambda: False):
        self.limits = limits
        self.deadline = time.monotonic() + limits.operation_seconds
        self.cancelled = cancelled
        self.expanded = 0
        self.acquired = 0
        self.cache: Path | None = None
        self.last_size = 0

    def watch_cache(self, cache: Path) -> None:
        self.cache = cache
        self.last_size = disk_bytes(cache)
        self.check()

    def check(self) -> None:
        if self.cancelled():
            raise BudgetExceeded('acquisition cancelled')
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded('operation deadline exceeded')
        if self.cache is not None:
            size = disk_bytes(self.cache)
            self.acquired += max(0, size - self.last_size)
            self.last_size = size
            if size > self.limits.cache_bytes:
                raise BudgetExceeded('cache disk budget exceeded')
            if self.acquired > self.limits.acquired_bytes:
                raise BudgetExceeded('acquired data budget exceeded')

    def expand(self, count: int) -> None:
        self.expanded += count
        if self.expanded > self.limits.expanded_bytes:
            raise BudgetExceeded('expanded object budget exceeded')
        self.check()


def run_bounded(argv: list[str], env: dict[str, str], cwd: Path, budget: Budget,
                *, stdin: bytes | None = None, stdout_cap: int | None = None) -> tuple[int, bytes]:
    budget.check()
    deadline = min(budget.deadline, time.monotonic() + budget.limits.command_seconds)
    cap = min(stdout_cap or budget.limits.stdout_bytes, budget.limits.stdout_bytes)
    # Git input is a finite list of validated object IDs, never interactive input.
    with subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as child:
        output = bytearray()
        stderr_size = 0
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ, 'stdout')
                selector.register(child.stderr, selectors.EVENT_READ, 'stderr')
                pending = memoryview(stdin or b'')
                if child.stdin is not None:
                    os.set_blocking(child.stdin.fileno(), False)
                    selector.register(child.stdin, selectors.EVENT_WRITE, 'stdin')
                while selector.get_map() or child.poll() is None:
                    budget.check()
                    if time.monotonic() >= deadline:
                        raise BudgetExceeded('command deadline exceeded')
                    for key, _ in selector.select(min(0.05, max(0, deadline - time.monotonic()))):
                        if key.data == 'stdin':
                            if pending:
                                try:
                                    pending = pending[os.write(key.fileobj.fileno(), pending[:65536]):]
                                except BrokenPipeError:
                                    pending = memoryview(b'')
                            if not pending:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                            continue
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        elif key.data == 'stdout':
                            if len(output) + len(chunk) > cap:
                                raise BudgetExceeded('stdout budget exceeded')
                            output.extend(chunk)
                        else:
                            stderr_size += len(chunk)
                            if stderr_size > budget.limits.stderr_bytes:
                                raise BudgetExceeded('stderr budget exceeded')
                code = child.wait()
            budget.check()
            return code, bytes(output)
        finally:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()


class AcquisitionLease:
    """Non-waiting cache and tenant leases shared across local worker processes."""
    def __init__(self, root: Path, tenant_key: str, cache_key: str, limits: AcquisitionLimits):
        self.root, self.tenant_key, self.cache_key, self.limits = root, tenant_key, cache_key, limits
        self.handles = []

    def __enter__(self):
        import fcntl
        # A fixed process ceiling applies even to separately composed readers.
        if not _enter_process(self.limits.per_process):
            raise BudgetExceeded('process acquisition concurrency exceeded')
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            for names in ([f'cache-{self.cache_key}.lock'],
                          [f'tenant-{self.tenant_key}-{i}.lock' for i in range(self.limits.per_tenant)]):
                acquired = False
                for name in names:
                    fd = os.open(self.root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                    handle = os.fdopen(fd, 'rb')
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        handle.close()
                        continue
                    self.handles.append(handle)
                    acquired = True
                    break
                if not acquired:
                    raise BudgetExceeded('source acquisition already in progress')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc):
        for handle in reversed(self.handles):
            handle.close()
        self.handles.clear()
        _leave_process()


_PROCESS_LOCK = threading.Lock()
_PROCESS_ACTIVE = 0


def _enter_process(limit: int) -> bool:
    global _PROCESS_ACTIVE
    with _PROCESS_LOCK:
        if _PROCESS_ACTIVE >= min(limit, 8):
            return False
        _PROCESS_ACTIVE += 1
        return True


def _leave_process() -> None:
    global _PROCESS_ACTIVE
    with _PROCESS_LOCK:
        _PROCESS_ACTIVE -= 1
