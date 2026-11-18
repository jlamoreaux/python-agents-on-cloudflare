"""Tools that only make sense in a container.

A Python Worker runs inside a V8 isolate, so it cannot start subprocesses or
install arbitrary native packages. A container is a full Linux VM, so this tool
can run whatever the model writes, using anything in the image.

The container is the sandbox. The limits below only stop a runaway script from
taking the agent server down with it; they are not a security boundary against
a determined attacker. See "Security notes" in the README.
"""

import asyncio
import os
import resource
import signal
import sys
import tempfile

from agent.core import MAX_RESULT_CHARS, Tool

TIMEOUT_S = 10
MAX_CONCURRENT = 2
MEMORY_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024

_slots = asyncio.Semaphore(MAX_CONCURRENT)


def _apply_limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_S + 2, TIMEOUT_S + 2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))


def _kill_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> bytes:
    """Read until EOF or until `cap` bytes, whichever comes first."""
    chunks: list[bytes] = []
    size = 0
    while size < cap:
        chunk = await stream.read(min(65536, cap - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


async def _drain(stream: asyncio.StreamReader) -> None:
    """Read a dead process's pipe to EOF. Without this, wait() can hang on unread output."""
    try:
        while await asyncio.wait_for(stream.read(65536), timeout=1):
            pass
    except asyncio.TimeoutError:
        pass


async def run_python(code: str) -> str:
    async with _slots:
        with tempfile.TemporaryDirectory() as workdir:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-c",
                code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=workdir,
                start_new_session=True,  # own process group, so we can kill grandchildren too
                preexec_fn=_apply_limits,
            )
            try:
                output = await asyncio.wait_for(
                    _read_capped(process.stdout, MAX_RESULT_CHARS), timeout=TIMEOUT_S
                )
            except asyncio.TimeoutError:
                return f"error: timed out after {TIMEOUT_S}s"
            finally:
                # Always runs: on timeout, on hitting the output cap, and if the request is cancelled.
                _kill_group(process)
                await _drain(process.stdout)
                await process.wait()

    text = output.decode(errors="replace") or "(no output)"
    return text if process.returncode in (0, -signal.SIGKILL) else f"exit code {process.returncode}\n{text}"


RUN_PYTHON = Tool(
    name="run_python",
    description=(
        "Run a Python 3 script in a fresh subprocess and return its combined "
        "stdout and stderr. Use print() to produce output. Standard library only. "
        "Limited to 10 seconds and 256 MB of memory."
    ),
    parameters={
        "type": "object",
        "properties": {"code": {"type": "string", "description": "The full script to run."}},
        "required": ["code"],
    },
    fn=run_python,
)
