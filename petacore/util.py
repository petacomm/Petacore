"""Small GTK helpers shared across Petacore."""

import threading

from gi.repository import GLib


def run_async(work, on_done):
    """Run `work()` in a background thread; call `on_done(result, error)` on
    the GTK main loop when finished."""
    def _thread():
        result, error = None, None
        try:
            result = work()
        except Exception as e:  # noqa: BLE001 - surfaced to the UI
            error = e
        GLib.idle_add(on_done, result, error)

    threading.Thread(target=_thread, daemon=True).start()


def human_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"
