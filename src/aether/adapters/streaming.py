"""Read a subprocess as it runs, instead of buffering it to the end.

`proc.communicate()` returns nothing until the child exits, so killing the
orchestrator mid-execution throws away everything the agent had already
produced. Reading incrementally lets each chunk be mirrored into the task's
`ExecutionLog` the moment it arrives, which is what makes a killed run
recoverable.

Feeding stdin runs concurrently with the readers on purpose: writing a large
prompt while nothing drains stdout deadlocks once the pipe buffer fills
(~64 KB on most platforms), and context packages are comfortably that big.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from aether.execution_log import ExecutionLog

CHUNK = 8192


async def _feed_stdin(proc: asyncio.subprocess.Process, data: Optional[bytes]) -> None:
    if data is None or proc.stdin is None:
        return
    try:
        proc.stdin.write(data)
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        # the agent exited without reading its prompt; its output still counts
        pass
    finally:
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass


async def _pump(
    stream: Optional[asyncio.StreamReader],
    limit: int,
    log: Optional[ExecutionLog],
) -> bytes:
    """Drain a pipe, mirroring into the log, stopping the copy at `limit`."""
    if stream is None:
        return b""
    collected = bytearray()
    while True:
        chunk = await stream.read(CHUNK)
        if not chunk:
            break
        if log is not None:
            log.append(chunk.decode("utf-8", errors="replace"))
        if len(collected) < limit:
            collected.extend(chunk[: limit - len(collected)])
        # keep draining past the limit so the child is never blocked on a full pipe
    return bytes(collected)


async def stream_process(
    proc: asyncio.subprocess.Process,
    stdin_data: Optional[bytes],
    max_output_bytes: int,
    log: Optional[ExecutionLog] = None,
) -> tuple[bytes, bytes]:
    """communicate() replacement that surfaces output as it arrives.

    Only stdout is mirrored to the log — stderr is usually progress noise and
    mixing the two would corrupt a salvaged handoff.
    """
    stdout, stderr, _ = await asyncio.gather(
        _pump(proc.stdout, max_output_bytes, log),
        _pump(proc.stderr, max_output_bytes, None),
        _feed_stdin(proc, stdin_data),
    )
    await proc.wait()
    return stdout, stderr
