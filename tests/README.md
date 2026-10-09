# Tests

Run from the repository root with Python 3.13 or later:

```sh
python -m pip install watchdog==6.0.0 voluptuous PyYAML
python -m unittest discover -s tests -v
```

These tests import the actual scanner, upload waiting, service and sensor code.
Small Home Assistant boundary doubles replace registration, platform forwarding,
validation helpers and state publication; they do not replace integration logic.
Watchdog is required. On Linux, the writer-close test uses a real inotify observer
and an open upload handle, with a quiet period longer than the test deadline.

The async fixture supplies a dedicated executor and a 10 ms event-loop timer.
On the restricted development host, a delayed cross-thread callback does not wake
an otherwise idle selector loop (even a standalone `asyncio.to_thread` call can
hang). The timer compensates for that host behavior while keeping actual executor
threads and watchdog callbacks active. It does not mark files ready or simulate
filesystem events. Timing assertions allow scheduler overhead and verify completion
well before the configured quiet period.
