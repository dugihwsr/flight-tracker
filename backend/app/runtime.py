"""User-adjustable runtime settings (refresh mode/interval), persisted next to the tracked flights.

modes: "live"   - refresh at the chosen interval no matter what (still stops when a plan is exhausted)
       "saver"  - stretch the interval automatically so limited plans last the whole day/month
       "manual" - never refresh in the background; only when the user presses Refresh
"""
import json
import logging
import os

from .budget import budget
from .config import settings

log = logging.getLogger(__name__)
MODES = ("live", "saver", "manual")
MIN_POLL = 5


class Runtime:
    def __init__(self):
        self.mode = "saver"
        self.poll_interval = max(MIN_POLL, settings.poll_interval)
        self._path = os.path.join(settings.data_dir, "runtime.json")

    def load(self):
        try:
            with open(self._path) as f:
                d = json.load(f)
            self.update(d.get("mode"), d.get("poll_interval"), save=False)
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("could not load runtime settings: %s", e)
        budget.mode = self.mode

    def update(self, mode=None, poll_interval=None, save=True):
        if mode in MODES:
            self.mode = mode
        if isinstance(poll_interval, (int, float)) and poll_interval >= MIN_POLL:
            self.poll_interval = min(int(poll_interval), 3600)
        budget.mode = self.mode
        if save:
            try:
                os.makedirs(settings.data_dir, exist_ok=True)
                with open(self._path, "w") as f:
                    json.dump({"mode": self.mode, "poll_interval": self.poll_interval}, f)
            except Exception as e:  # noqa: BLE001
                log.warning("could not save runtime settings: %s", e)


runtime = Runtime()
