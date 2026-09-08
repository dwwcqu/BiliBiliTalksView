"""Bounded capture of one child process; no shell and no environment persistence."""

import os
import signal
import subprocess
import threading
import time
from tempfile import TemporaryFile


def _terminate(process):
    verified = True
    try:
        if os.name == "nt":
            # An exited PID cannot reliably locate its former descendants.
            if process.poll() is not None:
                verified = False
            else:
                completed = subprocess.run(
                    [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                  "System32", "taskkill.exe"),
                     "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW, check=False,
                )
                verified = completed.returncode == 0
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    except (OSError, subprocess.TimeoutExpired):
        verified = False
    finally:
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            verified = False
    return verified


def run_process(argv, *, cwd, env, stdin, timeout_seconds, max_output_bytes):
    """Return bounded raw bytes; callers decide transport and business acceptance.

    Dedicated readers drain both pipes, sharing a single capture cap. Stdin is
    a private temporary file to avoid a blocked writer hiding a timeout.
    """
    output = {"stdout": bytearray(), "stderr": bytearray()}
    guard = threading.Lock()
    overflow = threading.Event()
    failed_read = threading.Event()
    used = 0

    def consume(pipe, name):
        nonlocal used
        try:
            with pipe:
                while chunk := os.read(pipe.fileno(), 4096):
                    with guard:
                        keep = min(len(chunk), max_output_bytes - used)
                        output[name].extend(chunk[:keep])
                        used += keep
                        if keep < len(chunk):
                            overflow.set()
                            return
        except OSError:
            failed_read.set()

    with TemporaryFile() as source:
        source.write(stdin)
        source.seek(0)
        try:
            process = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=source, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, shell=False,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError:
            return {"returncode": None, "stdout": b"", "stderr": b"", "stop_reason": "spawn_failed"}
        readers = [threading.Thread(target=consume, args=(getattr(process, name), name),
                                    daemon=True) for name in output]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + timeout_seconds
        stop_reason = None
        try:
            while True:
                if overflow.is_set():
                    stop_reason = "output_limit"
                    break
                if failed_read.is_set():
                    stop_reason = "output_read_failed"
                    break
                if process.poll() is not None and not any(t.is_alive() for t in readers):
                    break
                if time.monotonic() >= deadline:
                    stop_reason = "timeout"
                    break
                time.sleep(0.01)
        finally:
            trigger_reason = stop_reason
            if (stop_reason is not None or process.poll() is None) and not _terminate(process):
                stop_reason = "process_cleanup_unverified"
            for reader in readers:
                reader.join(timeout=1)
        if any(t.is_alive() for t in readers):
            stop_reason = "process_cleanup_unverified"
        with guard:
            return {"returncode": process.returncode,
                        "stdout": bytes(output["stdout"]), "stderr": bytes(output["stderr"]),
                        "stop_reason": stop_reason, "trigger_reason": trigger_reason}
