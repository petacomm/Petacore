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
    """A size as the person's language writes it: "1.1 KB", "1,1 КБ",
    "1,1 Ko" — the units and the decimal separator both come from the
    translation table."""
    from .i18n import translator as _
    units = (_("unit_b"), _("unit_kb"), _("unit_mb"), _("unit_gb"))
    sep = _("decimal_sep")
    for index, unit in enumerate(units):
        if num < 1024 or index == len(units) - 1:
            if index == 0:
                return f"{num:.0f} {unit}"
            return f"{num:.1f}".replace(".", sep) + f" {unit}"
        num /= 1024
    return f"{num:.1f}".replace(".", sep) + f" {units[-1]}"
