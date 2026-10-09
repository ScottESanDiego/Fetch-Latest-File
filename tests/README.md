# Tests

Use Linux and Python 3.14.2 or newer. From the repository root, install the
pinned development dependencies and run the checks:

```sh
uv sync --group dev
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

`pyproject.toml` pins Home Assistant 2026.10.0, its compatible custom-component
pytest plugin, and the test and lint tools. Tests load the real Home Assistant
integration, config entries, service registry, sensor platform, and entity registry.
They cover optional responses, translated errors, options, reload, disabled
sensors, concurrency, and unload alongside scanner and upload-waiting behavior.
No Home Assistant module doubles are installed into `sys.modules`.

Linux tests use actual asyncinotify events and open upload handles to verify
writer-close readiness before a long quiet period expires. Other cases cover
mtime heuristics, polling fallback, overflow, watch invalidation, limits, and
candidate revalidation. CI runs the same pytest and Ruff commands on Linux. Separate CI jobs run the official
Home Assistant Hassfest and HACS integration validators. These container-based
validators were not executed locally; inspect their GitHub Actions results.

The session fixture bounds `EpollSelector.select` waits to 10 ms. On the
restricted development host, cross-thread callbacks can fail to wake an idle
selector, including a standalone `asyncio.to_thread` call. The bound permits
executor completion and event-loop shutdown; it does not synthesize filesystem
events, replace Home Assistant, or mark files ready. Timing assertions allow
scheduler overhead while checking completion before the configured quiet period.
