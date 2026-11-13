"""In-process entry-point lock, including manual runs outside systemd/flock."""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import fcntl
import os

LOCK_PATH = Path.home() / ".local/state/tinvest-sandbox-trader/engine.lock"


@contextmanager
def engine_lock(path=None):
    path = Path(path or LOCK_PATH)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Sandbox engine or upgrade is running") from None
        yield
    finally:
        os.close(fd)


def single_run(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with engine_lock():
            return function(*args, **kwargs)
    return wrapped
