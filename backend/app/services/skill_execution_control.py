"""Cooperative cancellation for a single project Skill execution."""

import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime


class SkillExecutionStopped(RuntimeError):
    pass


_current = ContextVar("skill_execution_control", default=None)
_controls = {}
_lock = threading.Lock()


class Control:
    def __init__(self):
        self.stopped = threading.Event()
        self.reason = "用户暂停了本轮处理"
        self.progress_events = []
        self.progress_lock = threading.Lock()

    def progress(self, label):
        with self.progress_lock:
            self.progress_events.append({"at": datetime.now(UTC).isoformat(), "label": label})
            self.progress_events = self.progress_events[-200:]

    def take_progress(self):
        with self.progress_lock:
            events, self.progress_events = self.progress_events, []
            return events

    def stop(self, reason="用户暂停了本轮处理"):
        self.reason = reason
        self.stopped.set()

    def check(self):
        if self.stopped.is_set():
            raise SkillExecutionStopped(self.reason)


@contextmanager
def execution_scope(key, control):
    token = _current.set(control)
    with _lock:
        _controls[key] = control
    try:
        yield
    finally:
        with _lock:
            _controls.pop(key, None)
        _current.reset(token)


def request_stop(key):
    with _lock:
        control = _controls.get(key)
    if control:
        control.stop()


def checkpoint():
    control = _current.get()
    if control:
        control.check()


def report_progress(label):
    control = _current.get()
    if control:
        control.check()
        control.progress(label)


def run_process(command, **kwargs):
    control = _current.get()
    if control is None:
        return subprocess.run(command, **kwargs)
    control.check()
    timeout = kwargs.pop("timeout")
    data = kwargs.pop("input")
    kwargs.pop("check", None)
    kwargs.pop("capture_output", None)
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        **kwargs,
    )
    try:
        first = True
        while True:
            control.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(
                    input=data if first else None, timeout=min(0.2, remaining)
                )
                control.check()
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                first = False
    finally:
        if process.poll() is None:
            # This process group was created exclusively for this invocation.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
