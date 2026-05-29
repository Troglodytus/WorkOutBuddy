# Workout Buddy v9: async metric recalculation fix

Your WinError 10054 message is typically a Windows/uvicorn/NiceGUI client disconnect while a long synchronous callback is running. It is usually non-fatal: the browser or websocket connection was closed/reset while the server was busy.

This patch changes `Recalculate metrics` from a long blocking synchronous callback into an async handler that runs the heavy recalculation in a worker thread:

```python
await asyncio.to_thread(self._recalculate_all_metrics_worker)
```

This keeps the UI event loop responsive and should prevent/reduce the noisy Proactor connection reset. It also avoids direct NiceGUI UI updates from the worker thread.

## Main changed file

- `web_app.py`

The v8 database fix and previous weather/duplicate files are included in the ZIP as well.
