import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
API_LOG = LOG_DIR / "api.log"
STREAM_LOG = LOG_DIR / "stream_and_record.log"
MEDIAMTX_SOURCE_LOG = BASE_DIR / "mediamtx.log"
MEDIAMTX_LOG = LOG_DIR / "mediamtx.log"


class LogFanout:
    def __init__(self):
        self._lock = threading.Lock()
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def write(self, name, log_path, line):
        line = line.rstrip("\n")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted = f"[{timestamp}] [{name}] {line}"
        with self._lock:
            print(formatted, flush=True)
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(formatted + "\n")

    def header(self, log_path):
        started_at = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(f"\n===== run started at {started_at} =====\n")


def make_child_process_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"preexec_fn": os.setsid}


def stop_process(process, name, fanout):
    if process.poll() is not None:
        return

    fanout.write("RUNNER", LOG_DIR / "runner.log", f"Stopping {name}...")
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except Exception as error:
        fanout.write("RUNNER", LOG_DIR / "runner.log", f"{name} graceful stop failed: {error}")

    try:
        process.wait(timeout=8)
        return
    except subprocess.TimeoutExpired:
        fanout.write("RUNNER", LOG_DIR / "runner.log", f"{name} did not stop in time. Killing it.")

    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except Exception as error:
        fanout.write("RUNNER", LOG_DIR / "runner.log", f"{name} kill failed: {error}")


def stream_process_output(process, name, log_path, fanout):
    fanout.header(log_path)
    try:
        for line in iter(process.stdout.readline, ""):
            if not line:
                break
            fanout.write(name, log_path, line)
    finally:
        if process.stdout:
            process.stdout.close()


def tail_file(source_path, name, log_path, fanout, stop_event):
    fanout.header(log_path)
    position = source_path.stat().st_size if source_path.exists() else 0

    while not stop_event.is_set():
        if not source_path.exists():
            time.sleep(0.3)
            continue

        try:
            size = source_path.stat().st_size
            if size < position:
                position = 0

            with source_path.open("r", encoding="utf-8", errors="replace") as source_file:
                source_file.seek(position)
                for line in source_file:
                    fanout.write(name, log_path, line)
                position = source_file.tell()
        except OSError as error:
            fanout.write("RUNNER", LOG_DIR / "runner.log", f"Failed to read {source_path.name}: {error}")

        time.sleep(0.5)


def start_process(command, name, log_path, fanout, env):
    fanout.write("RUNNER", LOG_DIR / "runner.log", f"Starting {name}: {' '.join(command)}")
    process = subprocess.Popen(
        command,
        cwd=BASE_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **make_child_process_options(),
    )
    thread = threading.Thread(
        target=stream_process_output,
        args=(process, name, log_path, fanout),
        daemon=True,
    )
    thread.start()
    return process, thread


def build_api_env(base_env, stream_process):
    env = base_env.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["AI_CCTV_DISK_PATH"] = str(BASE_DIR / "backups")
    env["AI_CCTV_PROCESS_KEYWORDS"] = ",".join(
        [
            "stream_and_record.sh",
            "gst-launch",
            "gstreamer",
            "libcamera",
            "rpicam",
            "mediamtx",
            "backup_api_server",
            "run_rtsp_server",
        ]
    )
    env["AI_CCTV_MONITOR_PIDS"] = ",".join([str(os.getpid()), str(stream_process.pid)])
    return env


def main():
    fanout = LogFanout()
    fanout.header(LOG_DIR / "runner.log")
    fanout.write("RUNNER", LOG_DIR / "runner.log", "AI CCTV RTSP server launcher started.")

    base_env = os.environ.copy()
    base_env["PYTHONUNBUFFERED"] = "1"
    stop_event = threading.Event()
    processes = []

    mediamtx_tail = threading.Thread(
        target=tail_file,
        args=(MEDIAMTX_SOURCE_LOG, "MEDIAMTX", MEDIAMTX_LOG, fanout, stop_event),
        daemon=True,
    )
    mediamtx_tail.start()

    try:
        stream_process, stream_thread = start_process(
            ["bash", "stream_and_record.sh"],
            "STREAM",
            STREAM_LOG,
            fanout,
            base_env,
        )
        processes.append(("STREAM", stream_process))

        api_env = build_api_env(base_env, stream_process)
        api_process, api_thread = start_process(
            [sys.executable, "backup_api_server.py"],
            "API",
            API_LOG,
            fanout,
            api_env,
        )
        processes.append(("API", api_process))

        fanout.write("RUNNER", LOG_DIR / "runner.log", "API: http://0.0.0.0:8002")
        fanout.write("RUNNER", LOG_DIR / "runner.log", "RTSP: rtsp://localhost:8554/live")
        fanout.write("RUNNER", LOG_DIR / "runner.log", f"Logs: {LOG_DIR}")

        while True:
            for name, process in processes:
                return_code = process.poll()
                if return_code is not None:
                    fanout.write("RUNNER", LOG_DIR / "runner.log", f"{name} exited with code {return_code}.")
                    return return_code
            time.sleep(1)
    except KeyboardInterrupt:
        fanout.write("RUNNER", LOG_DIR / "runner.log", "Keyboard interrupt received.")
        return 0
    finally:
        stop_event.set()
        for name, process in reversed(processes):
            stop_process(process, name, fanout)
        fanout.write("RUNNER", LOG_DIR / "runner.log", "Launcher stopped.")


if __name__ == "__main__":
    raise SystemExit(main())
