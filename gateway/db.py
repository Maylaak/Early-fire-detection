"""Database schema and engine setup (SQLAlchemy Core).

Works with PostgreSQL (main path) and SQLite (no-Docker fallback).
Only the detection service creates tables; the web app connects with a
read-only role (Postgres) or a read-only file handle (SQLite).

All timestamps are stored and returned as timezone-aware UTC.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Aware UTC datetimes in and out, on every backend (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC")
        value = value.astimezone(timezone.utc)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


# BIGINT on Postgres, but SQLite only auto-increments a plain INTEGER primary key.
BigId = BigInteger().with_variant(Integer, "sqlite")

metadata = MetaData()

nodes = Table(
    "nodes",
    metadata,
    Column("dev_eui", String(16), primary_key=True),
    Column("name", String(64), nullable=False),
    Column("application_name", String(128)),
    Column("first_seen", UTCDateTime, nullable=False),
    Column("last_seen", UTCDateTime, nullable=False),
    Column("last_f_cnt", Integer),
    Column("last_rssi", Integer),
    Column("last_snr", Float),
    Column("battery_v", Float),
    # Maintained by the detection engine.
    Column("state", String(16), nullable=False, default="WARMUP"),
    Column("state_since", UTCDateTime),
    Column("online", Boolean, nullable=False, default=True),
)

readings = Table(
    "readings",
    metadata,
    Column("id", BigId, primary_key=True, autoincrement=True),
    Column("dev_eui", String(16), ForeignKey("nodes.dev_eui"), nullable=False),
    Column("time", UTCDateTime, nullable=False),  # from the uplink envelope
    Column("received_at", UTCDateTime, nullable=False),  # when this gateway got it
    # ChirpStack deduplicationId, or a synthetic key. Unique: MQTT QoS 1 can redeliver.
    Column("uplink_key", String(128), nullable=False, unique=True),
    Column("f_cnt", Integer, nullable=False),
    # Which mode the node sent this in (lorawan.* ports in config.yaml).
    # NULL for readings stored before the column existed.
    Column("f_port", Integer),
    Column("temperature_c", Float, nullable=False),
    Column("humidity_pct", Float, nullable=False),
    Column("pressure_hpa", Float, nullable=False),
    Column("gas_resistance_ohm", Float, nullable=False),
    Column("gas_saturated", Boolean, nullable=False, default=False),
    Column("battery_v", Float, nullable=False),
    Column("rssi", Integer),
    Column("snr", Float),
    Column("gateway_count", Integer, nullable=False, default=0),
    # Filled in by the detection engine for charting and post-mortems.
    Column("baseline_temperature_c", Float),
    Column("baseline_humidity_pct", Float),
    Column("baseline_gas_ohm", Float),
    Column("state", String(16)),
    Index("ix_readings_dev_eui_time", "dev_eui", "time"),
)

events = Table(
    "events",
    metadata,
    Column("id", BigId, primary_key=True, autoincrement=True),
    Column("dev_eui", String(16), ForeignKey("nodes.dev_eui"), nullable=False),
    Column("time", UTCDateTime, nullable=False),
    Column("kind", String(16), nullable=False),  # STATE, OFFLINE, ONLINE
    Column("from_state", String(16)),
    Column("to_state", String(16)),
    Column("message", Text, nullable=False),
    Column("details", JSON),
    Column("notified", Boolean, nullable=False, default=False),
    Index("ix_events_time", "time"),
)

# LoRaWAN gateways, as seen in uplinks' rxInfo. "Online" is decided by the
# reader from last_seen (a gateway that hears no node looks the same as a
# dead one; ChirpStack's gateway stats events would tell them apart).
gateways = Table(
    "gateways",
    metadata,
    Column("gateway_id", String(16), primary_key=True),
    Column("first_seen", UTCDateTime, nullable=False),
    Column("last_seen", UTCDateTime, nullable=False),
    Column("last_rssi", Integer),
    Column("last_snr", Float),
    Column("uplinks", BigInteger, nullable=False, default=0),
)

# Operator console event log: what happened, in words, oldest first.
console_log = Table(
    "console_log",
    metadata,
    Column("id", BigId, primary_key=True, autoincrement=True),
    Column("time", UTCDateTime, nullable=False),
    Column("level", String(8), nullable=False),  # info, warning, alarm
    Column("category", String(16), nullable=False),  # system, node, block, notify
    Column("message", Text, nullable=False),
    Column("dev_eui", String(16)),  # no foreign key: system entries have none
    Column("block", String(32)),
    Index("ix_console_log_time", "time"),
)


def make_engine(url: str, read_only: bool = False) -> Engine:
    """Create an engine. For SQLite, also create the data folder and enable WAL
    so the web app can read while the detection service writes."""
    u = make_url(url)
    if u.get_backend_name() != "sqlite":
        return create_engine(url, pool_pre_ping=True)

    on_disk = bool(u.database) and u.database != ":memory:"
    if read_only and on_disk:
        # Open the file with SQLite's own read-only mode: writes fail, and a
        # missing file is an error instead of silently creating an empty one.
        file_uri = Path(u.database).resolve().as_uri() + "?mode=ro"
        engine = create_engine(
            "sqlite://",
            creator=lambda: sqlite3.connect(file_uri, uri=True, check_same_thread=False, timeout=10),
        )
    else:
        if on_disk:
            Path(u.database).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 10})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        if not read_only:
            cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()

    return engine


def init_schema(engine: Engine) -> None:
    """Create any missing tables, and add new nullable columns to tables made
    by an older version (so an upgrade keeps its data). Detection service only."""
    metadata.create_all(engine)
    _add_missing_columns(engine)


def _add_missing_columns(engine: Engine) -> None:
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                if not col.nullable:
                    raise RuntimeError(f"table {table.name} lacks required column {col.name}: "
                                       "recreate the database (docker compose down -v)")
                ddl = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}'))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
