"""Operator console event log: entries in plain words, built from what the
detection service sees. Pure logic; the service stores them (console_log table)
and the web app's event log shows them.

Operators see site labels ("F-07-03") and blocks ("F-07"), never devEUIs;
the devEUI is kept on the entry for the node detail view.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from gateway.config import SitesConfig
from gateway.detect import ALARM, KIND_OFFLINE, WATCH, Event, NodeDetector

INFO, WARNING, ALARM_LEVEL = "info", "warning", "alarm"
SYSTEM, NODE, BLOCK, NOTIFY = "system", "node", "block", "notify"

_CLEAR = "CLEAR"


@dataclass(frozen=True)
class LogEntry:
    time: datetime
    level: str
    category: str
    message: str
    dev_eui: str | None = None
    block: str | None = None


def _block_of(sites: SitesConfig, dev_eui: str) -> str | None:
    site = sites.node(dev_eui)
    return site.block if site else None


def node_entries(evs: Iterable[Event], sites: SitesConfig) -> list[LogEntry]:
    """One entry per detector event (state change, offline, back online)."""
    out = []
    for e in evs:
        if e.to_state == ALARM:
            level = ALARM_LEVEL
        elif e.to_state == WATCH or e.kind == KIND_OFFLINE:
            level = WARNING
        else:
            level = INFO
        out.append(LogEntry(e.time, level, NODE, e.message, e.dev_eui, _block_of(sites, e.dev_eui)))
    return out


class BlockTracker:
    """Rolls node states up to blocks and logs when a block's picture changes:
    first WATCH, first ALARM, more nodes joining the alarm, all clear.

    A block is as bad as its worst node. Only nodes in the site registry
    belong to a block."""

    def __init__(self, sites: SitesConfig):
        self.sites = sites
        self._size: dict[str, int] = {}
        for n in sites.nodes:
            self._size[n.block] = self._size.get(n.block, 0) + 1
        self._last: dict[str, tuple[str, frozenset[str]]] = {}

    def prime(self, detectors: Mapping[str, NodeDetector]) -> None:
        """Take the current picture as known (after a restart replay): no entries."""
        self._last = self._picture(detectors)

    def update(self, detectors: Mapping[str, NodeDetector], t: datetime) -> list[LogEntry]:
        now = self._picture(detectors)
        out = []
        for block in sorted(self._size):
            level, alarmed = now.get(block, (_CLEAR, frozenset()))
            before, alarmed_before = self._last.get(block, (_CLEAR, frozenset()))
            if (level, alarmed) == (before, alarmed_before):
                continue
            size = self._size[block]
            names = ", ".join(sorted(alarmed))
            if level == ALARM and before != ALARM:
                out.append(LogEntry(t, ALARM_LEVEL, BLOCK,
                                    f"BLOCK {block} FIRE ALARM: {names} ({len(alarmed)} of {size} nodes)",
                                    block=block))
            elif level == ALARM and len(alarmed) > len(alarmed_before):
                out.append(LogEntry(t, ALARM_LEVEL, BLOCK,
                                    f"Block {block} alarm spreading: {names} ({len(alarmed)} of {size} nodes)",
                                    block=block))
            elif level == WATCH and before == _CLEAR:
                watching = self._in_state(detectors, block, WATCH)
                out.append(LogEntry(t, WARNING, BLOCK,
                                    f"Block {block} on WATCH: {', '.join(watching)} "
                                    f"({len(watching)} of {size} nodes)", block=block))
            elif level == _CLEAR and before != _CLEAR:
                out.append(LogEntry(t, INFO, BLOCK, f"Block {block} all clear", block=block))
        self._last = now
        return out

    def _in_state(self, detectors: Mapping[str, NodeDetector], block: str, state: str) -> list[str]:
        return sorted(d.name for d in detectors.values()
                      if d.state == state and _block_of(self.sites, d.dev_eui) == block)

    def _picture(self, detectors: Mapping[str, NodeDetector]) -> dict[str, tuple[str, frozenset[str]]]:
        out = {}
        for block in self._size:
            alarmed = frozenset(self._in_state(detectors, block, ALARM))
            if alarmed:
                out[block] = (ALARM, alarmed)
            elif self._in_state(detectors, block, WATCH):
                out[block] = (WATCH, frozenset())
            else:
                out[block] = (_CLEAR, frozenset())
        return out


def service_started(t: datetime, n_nodes: int, notifier_names: Iterable[str], speedup: float) -> LogEntry:
    channels = ", ".join(notifier_names) or "none"
    demo = f" DEMO SPEED-UP x{speedup:g}." if speedup != 1 else ""
    return LogEntry(t, INFO, SYSTEM,
                    f"Detection service started: {n_nodes} nodes known, alert channels: {channels}.{demo}")


def service_stopped(t: datetime) -> LogEntry:
    return LogEntry(t, WARNING, SYSTEM, "Detection service stopped")


def system_check(t: datetime, detectors: Mapping[str, NodeDetector],
                 gateways_online: int, gateways_total: int) -> LogEntry:
    ds = list(detectors.values())
    reporting = sum(d.online for d in ds)
    alarm = sum(d.state == ALARM for d in ds)
    watch = sum(d.state == WATCH for d in ds)
    healthy = reporting == len(ds) and gateways_online == gateways_total and not (alarm or watch)
    return LogEntry(
        t, INFO if healthy else WARNING, SYSTEM,
        f"System check: {reporting}/{len(ds)} nodes reporting, "
        f"{gateways_online}/{gateways_total} gateways online, {alarm} in alarm, {watch} on watch")


def notification(t: datetime, headline: str, node_label: str, channel: str, ok: bool,
                 detail: str = "", dev_eui: str | None = None) -> LogEntry:
    if ok:
        return LogEntry(t, INFO, NOTIFY, f"{channel}: sent \"{headline}\" for {node_label}", dev_eui)
    return LogEntry(t, WARNING, NOTIFY,
                    f"{channel}: FAILED to send \"{headline}\" for {node_label}"
                    + (f" ({detail})" if detail else ""), dev_eui)
