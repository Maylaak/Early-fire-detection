"""Database writes for the detection service, off the alarm path.

The service detects and alerts from memory first, then hands every database
write to this queue. One background thread writes the jobs in the order they
were submitted. While the database is unreachable it retries the job at the
head of the queue and keeps the rest waiting, so detection and alerts carry on
through an outage and nothing is written out of order when it comes back.

The queue is capped (database.max_queued_writes). Past the cap the OLDEST
READING is dropped to make room. Events (state changes, OFFLINE), "notified"
marks and console-log lines are never dropped.

What is still lost: if the service process is killed during an outage, the
jobs waiting in this queue (in memory) are lost. Alerts already sent are not
affected.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import DBAPIError, OperationalError

from gateway.chirpstack import Uplink
from gateway.codec import SensorReading
from gateway.console_log import LogEntry
from gateway.detect import Evaluation, Event
from gateway.store import NodeState, Store

log = logging.getLogger(__name__)

RETRY_SECONDS = 2.0
DROP_LOG_EVERY = 1000


class EventRef:
    """Stands for an event's database id before the event has been written
    (the dispatcher may deliver an alert while the database is down)."""

    __slots__ = ("id",)

    def __init__(self) -> None:
        self.id: int | None = None

    def __str__(self) -> str:
        return str(self.id) if self.id is not None else "(not stored yet)"


class Job:
    droppable = False

    def run(self, store: Store) -> None:
        raise NotImplementedError


@dataclass(eq=False)
class ReadingJob(Job):
    """One uplink and its detection result. The only kind of job the cap may drop."""

    uplink: Uplink
    reading: SensorReading
    received_at: datetime
    evaluation: Evaluation
    node: NodeState
    droppable = True

    def run(self, store: Store) -> None:
        if store.save_reading(self.uplink, self.reading, self.received_at,
                              self.evaluation, self.node) is None:
            log.info("uplink from %s fCnt=%d was already stored", self.uplink.device_name,
                     self.uplink.f_cnt)


@dataclass(eq=False)
class EventsJob(Job):
    events: Sequence[Event]
    refs: Sequence[EventRef]
    states: Mapping[str, NodeState]

    def run(self, store: Store) -> None:
        for ref, event_id in zip(self.refs, store.save_events(self.events, self.states)):
            ref.id = event_id


@dataclass(eq=False)
class NotifiedJob(Job):
    ref: EventRef

    def run(self, store: Store) -> None:
        if self.ref.id is None:  # its event could not be written at all (logged then)
            log.warning("cannot mark an unstored event as notified")
            return
        store.mark_notified([self.ref.id])


@dataclass(eq=False)
class LogJob(Job):
    entries: Sequence[LogEntry]

    def run(self, store: Store) -> None:
        store.append_log(self.entries)


class Writer:
    def __init__(self, store: Store, max_jobs: int, retry_seconds: float = RETRY_SECONDS):
        self.store = store
        self.max_jobs = max_jobs
        self.retry_seconds = retry_seconds
        self.dropped = 0  # readings dropped because the queue was full
        self.written = 0
        self._queue: deque[Job] = deque()
        self._busy = False  # a job is taken off the queue and being written
        self._stopping = False
        self._outage_since: float | None = None
        self._cond = threading.Condition()
        self._thread = threading.Thread(target=self._run, name="db-writer", daemon=True)

    def start(self) -> Writer:
        self._thread.start()
        return self

    @property
    def pending(self) -> int:
        with self._cond:
            return len(self._queue) + self._busy

    @property
    def database_down(self) -> bool:
        return self._outage_since is not None

    def submit(self, job: Job) -> None:
        with self._cond:
            if len(self._queue) >= self.max_jobs:
                self._make_room()
            self._queue.append(job)
            self._cond.notify_all()

    def _make_room(self) -> None:
        oldest = next((j for j in self._queue if j.droppable), None)
        if oldest is None:
            return  # only events and log lines waiting: never dropped, let it grow
        self._queue.remove(oldest)
        self.dropped += 1
        if self.dropped == 1 or self.dropped % DROP_LOG_EVERY == 0:
            log.error("write queue full (%d jobs): dropped the oldest stored-reading write "
                      "(%d dropped so far). Events and alerts are kept.", self.max_jobs, self.dropped)

    def flush(self, timeout: float) -> bool:
        """Wait until everything submitted so far is written. False on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._queue or self._busy:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
        return True

    def stop(self, timeout: float = 10.0) -> int:
        """Write what is queued (up to `timeout`), then stop. Returns the
        number of jobs left unwritten (lost)."""
        self.flush(timeout)
        with self._cond:
            self._stopping = True
            left = len(self._queue) + self._busy
            self._cond.notify_all()
        self._thread.join(1.0)
        if left:
            log.error("stopping with %d database writes not done (database unavailable): "
                      "they are lost", left)
        return left

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
                job = self._queue.popleft()
                self._busy = True
            self._write(job)
            with self._cond:
                self._busy = False
                self._cond.notify_all()

    def _write(self, job: Job) -> None:
        """Write one job, retrying while the database is unreachable."""
        while True:
            try:
                job.run(self.store)
            except OperationalError as e:
                self._down(e)
            except DBAPIError as e:
                if not e.connection_invalidated:
                    log.exception("database rejected a %s; skipping it", type(job).__name__)
                    return
                self._down(e)
            else:
                self.written += 1
                if self._outage_since is not None:
                    log.warning("database is back after %.0f s; writing %d queued jobs",
                                time.monotonic() - self._outage_since, self.pending)
                    self._outage_since = None
                return
            # Wait out the retry delay. New jobs wake the condition too; they
            # must not turn into a retry per incoming uplink.
            deadline = time.monotonic() + self.retry_seconds
            with self._cond:
                while not self._stopping and (left := deadline - time.monotonic()) > 0:
                    self._cond.wait(left)
                if self._stopping:
                    return

    def _down(self, e: DBAPIError) -> None:
        if self._outage_since is None:
            self._outage_since = time.monotonic()
            msg = str(e.orig).strip().splitlines()[0] if str(e.orig).strip() else type(e).__name__
            log.error("database unavailable (%s): writes are queued (%d waiting); detection "
                      "and alerts continue. Retrying every %gs.", msg, self.pending, self.retry_seconds)
