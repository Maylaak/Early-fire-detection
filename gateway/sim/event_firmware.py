"""Event-driven simulated firmware. Local sensing is independent of radio traffic.

SQLite checkpoints and an outbox reserve transmissions atomically. Restarted
publishes retain the same counter and timestamp, allowing gateway deduplication.
Acknowledgement means broker acceptance, not proof of physical radio delivery.
"""
from dataclasses import replace, asdict
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3

from gateway.chirpstack import duty_cycle_gap_seconds
from gateway.detect import NodeDetector
from gateway.codec import SensorReading
from gateway.sim.model import Sample
from gateway.sim.firmware import NodeFirmware, Transmission


def _encode(value):
    if isinstance(value, datetime):
        return {"__time__": value.isoformat()}
    raise TypeError(type(value).__name__)


def _decode(value):
    return datetime.fromisoformat(value['__time__']) if set(value)=={'__time__'} else value


class EventNodeFirmware(NodeFirmware):
    def __init__(self, model, cfg, state_path=None):
        super().__init__(model, cfg.simulator.firmware,
                         cfg.detection.explicit_heartbeat_minutes, cfg.timing.demo_speedup)
        self.detector = NodeDetector(model.node.dev_eui, model.node.name, cfg.detection)
        self.min_gap = timedelta(seconds=duty_cycle_gap_seconds(
            model.spreading_factor, 2, self.fw.duty_cycle) / cfg.timing.demo_speedup)
        self.state_path = state_path
        self.state = None
        if state_path:
            Path(state_path).parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(state_path) as db:
                db.execute('CREATE TABLE IF NOT EXISTS simulator_nodes (dev_eui TEXT PRIMARY KEY, state TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS simulator_outbox (dev_eui TEXT, frame INTEGER, payload TEXT NOT NULL, PRIMARY KEY(dev_eui, frame))')

    def configured_rates(self):
        return {'heartbeat': self.heartbeat}

    def step(self, t):
        if t.tzinfo is None:
            raise ValueError('simulator requires an aware UTC clock')
        if not self.state_path:
            tx = self._step(t)
            return tx
        # Serialize checkpoint changes even if a second simulator is accidentally opened.
        with sqlite3.connect(self.state_path, timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM simulator_nodes WHERE dev_eui=?',
                             (self.model.node.dev_eui,)).fetchone()
            self.state = json.loads(row[0], object_hook=_decode) if row else None
            tx = self._step(t)
            db.execute('INSERT OR REPLACE INTO simulator_nodes VALUES (?,?)',
                       (self.model.node.dev_eui, json.dumps(self.state, default=_encode)))
            if tx is not None:
                db.execute('INSERT INTO simulator_outbox VALUES (?,?,?)',
                           (self.model.node.dev_eui, tx.sample.f_cnt, json.dumps(asdict(tx), default=_encode)))
            return tx

    def pending_transmissions(self):
        if not self.state_path:
            return []
        with sqlite3.connect(self.state_path) as db:
            rows = db.execute('SELECT payload FROM simulator_outbox WHERE dev_eui=? ORDER BY frame',
                              (self.model.node.dev_eui,)).fetchall()
        result = []
        for row in rows:
            data = json.loads(row[0], object_hook=_decode)
            sample = data['sample']
            sample['reading'] = SensorReading(**sample['reading'])
            result.append(Transmission(Sample(**sample), data['reason']))
        return result

    def acknowledge(self, tx):
        if self.state_path:
            with sqlite3.connect(self.state_path) as db:
                db.execute('DELETE FROM simulator_outbox WHERE dev_eui=? AND frame=?',
                           (self.model.node.dev_eui, tx.sample.f_cnt))

    def _step(self, t):
        if self.state is None:
            self.state = dict(next_heartbeat=t, last_sample=None, last_tx=None,
                              frame=0, pending=False, episode=False, detector={},
                              heartbeat_seconds=self.heartbeat.total_seconds())
        st = self.state
        if st.get('heartbeat_seconds') != self.heartbeat.total_seconds():
            # A new clock rate starts a future interval; never send an extra boot heartbeat.
            st['next_heartbeat'] = t + self.heartbeat
            st['heartbeat_seconds'] = self.heartbeat.total_seconds()
        if st['last_sample'] is not None and t < st['last_sample'] + self.interval:
            return None
        self.detector.__dict__.update(st['detector'])
        sample = self.model.sample(t)
        self.detector.process(t, sample.reading)
        # One alert per local alarm episode; only full NORMAL rearms it.
        if self.detector.state == 'ALARM' and not st['episode']:
            st['pending'] = True
            st['episode'] = True
        elif self.detector.state == 'NORMAL':
            st['episode'] = False
        st['last_sample'] = t
        st['detector'] = {k:v for k,v in self.detector.__dict__.items() if k!='cfg'}
        if not st['pending'] and t < st['next_heartbeat']:
            return None
        if st['last_tx'] is not None and t-st['last_tx'] < self.min_gap:
            self.held_by_duty_cycle += 1
            return None
        reason = 'FIRE_ALERT' if st['pending'] else 'HEARTBEAT'
        if reason == 'FIRE_ALERT':
            st['pending'] = False
        else:
            # Advance from the deadline, not the last sample. No catch-up burst after sleep.
            intervals = (t-st['next_heartbeat']) // self.heartbeat + 1
            st['next_heartbeat'] += intervals*self.heartbeat
        tx = Transmission(replace(sample, f_cnt=st['frame']), reason)
        st['frame'] += 1
        st['last_tx'] = t
        return tx
