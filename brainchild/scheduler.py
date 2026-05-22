"""Monotonic-clock scheduler with sleep-resilient catch-up.

Each job has:
- name, interval-or-wallclock spec, last_fired_at (persisted)
- On every tick: compute most-recent should-have-fired timestamp; if
  last_fired_at < that, fire once and update.

No backlog queueing — slept-through misses collapse to a single fire.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from brainchild.config import PATHS

log = logging.getLogger("brainchild.sched")


@dataclass
class Job:
    name: str
    fn: Callable[[], None]
    interval_sec: int | None = None      # for cron-like every-N-seconds
    daily_time: str | None = None        # "HH:MM" 24h local — fire once per day after this
    catch_up: bool = True                # if False, never fire after the canonical moment


class Scheduler:
    def __init__(self, state_path: Path | None = None) -> None:
        self.state_path = state_path or PATHS.jobs_state
        self.jobs: list[Job] = []
        self.state: dict[str, float] = self._load()

    def add(self, job: Job) -> None:
        self.jobs.append(job)
        self.state.setdefault(job.name, 0.0)

    def _load(self) -> dict[str, float]:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text())
        except Exception:
            return {}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        os.replace(tmp, self.state_path)

    def tick(self, now: float | None = None) -> int:
        """Run all jobs whose target time has passed since last fire. Returns count."""
        now = now or time.time()
        fired = 0
        for job in self.jobs:
            target = self._target(job, now)
            if target is None:
                continue
            if self.state.get(job.name, 0.0) < target:
                try:
                    job.fn()
                    self.state[job.name] = now
                    self._save()
                    fired += 1
                    log.info("job=%s status=fired target=%.0f", job.name, target)
                except Exception:
                    log.exception("job=%s status=error", job.name)
        return fired

    @staticmethod
    def _target(job: Job, now: float) -> float | None:
        """Most recent should-have-fired timestamp ≤ now, or None if not due."""
        if job.interval_sec:
            # Floor to interval boundary
            return now - (now % job.interval_sec)
        if job.daily_time:
            try:
                h, m = job.daily_time.split(":")
                today = datetime.now().replace(
                    hour=int(h), minute=int(m), second=0, microsecond=0,
                )
            except ValueError:
                return None
            if today.timestamp() <= now:
                return today.timestamp()
            return (today - timedelta(days=1)).timestamp()
        return None
