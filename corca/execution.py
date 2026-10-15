"""Bound expensive rankers without relying on signals in third-party code."""
import multiprocessing as mp
import time
import traceback

from .circa import InsufficientEvidence


def _worker(send, fn, args):
    try:
        send.send((True, fn(*args)))
    except InsufficientEvidence as exc:
        send.send((False, exc))
    except BaseException:
        send.send((False, traceback.format_exc()))
    finally:
        send.close()


def call_with_timeout(fn, seconds, args=()):
    """Call a picklable function in a spawned process; terminate it on expiry.

    With seconds=None, call directly. The timeout includes worker startup.
    Spawning works in threads and does not inherit third-party signal handlers.
    """
    if seconds is None:
        return fn(*args)
    if seconds <= 0:
        raise ValueError("timeout must be positive or None")
    ctx = mp.get_context("spawn")
    receive, send = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_worker, args=(send, fn, args))
    deadline = time.monotonic() + seconds
    try:
        process.start()
        send.close()
        if not receive.poll(max(0, deadline - time.monotonic())):
            raise TimeoutError(f"ranker exceeded {seconds:g} seconds")
        try:
            ok, value = receive.recv()
        except EOFError as exc:
            raise RuntimeError("ranker worker exited without returning a result") from exc
        if not ok:
            if isinstance(value, InsufficientEvidence):
                raise value
            raise RuntimeError(value)
        return value
    finally:
        receive.close()
        send.close()
        if process.pid is not None:
            process.join(timeout=0.1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join()
            process.close()
