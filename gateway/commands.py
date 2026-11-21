"""Authenticated local operator commands. No database access in the HTTP listener.

The detection loop consumes requests; its Writer owns the transaction. A stale
alarm event ID cannot clear a newer alarm. Disabled unless a strong token is set.
"""
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
import queue
import re
import threading
import time

import httpx


@dataclass(frozen=True)
class CommandSettings:
    token: str = field(default='', repr=False)
    port: int = 8766

    @classmethod
    def from_env(cls):
        token = os.environ.get('WILDFIRE_ADMIN_TOKEN', '')
        if not token and os.environ.get('WILDFIRE_LOCAL_DEMO') == '1':
            from gateway.local_operator import local_operator_token
            token = local_operator_token()
        if token and not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token):
            raise ValueError('WILDFIRE_ADMIN_TOKEN must be 32-256 URL-safe characters')
        port = int(os.environ.get('WILDFIRE_COMMAND_PORT', '8766'))
        if not 1024 <= port <= 65535:
            raise ValueError('WILDFIRE_COMMAND_PORT must be 1024-65535')
        return cls(token, port)

    def authorized(self, header):
        expected = f'Bearer {self.token}'.encode()
        return bool(self.token) and hmac.compare_digest((header or '').encode(), expected)


@dataclass
class ClearRequest:
    dev_eui: str
    alarm_event_id: int
    deadline: float = field(default_factory=lambda: time.monotonic() + 6)
    result: Future = field(default_factory=Future)


def validate_command(body):
    if not isinstance(body, dict) or set(body) != {'dev_eui', 'alarm_event_id'}:
        raise ValueError('Expected node and alarm event ID')
    node, event_id = body['dev_eui'], body['alarm_event_id']
    if not isinstance(node, str) or not re.fullmatch(r'[0-9a-fA-F]{16}', node):
        raise ValueError('Invalid node identifier')
    if type(event_id) is not int or not 0 < event_id < 2**63:
        raise ValueError('Invalid alarm event ID')
    return node.lower(), event_id


def unavailable():
    return 503, {'detail': 'Clear not confirmed. Refresh status before retrying; the command service or database may be unavailable.'}


def start_command_server(settings, inbox):
    if not settings.token:
        return None

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass  # Never log Authorization or request content.

        def reply(self, status, body):
            data = json.dumps(body).encode()
            try:
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except OSError:
                pass  # Client timeout does not undo a committed clear.

        def do_POST(self):
            # Consume the small bounded body before closing a rejected request.
            # Leaving bytes unread can reset the socket on Windows and hide 401.
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1024 or self.headers.get('Transfer-Encoding'):
                    raise ValueError('Invalid request size')
                raw = self.rfile.read(length)
            except (ValueError, OSError):
                self.reply(400, {'detail': 'Invalid request size'})
                return
            if not settings.authorized(self.headers.get('Authorization')):
                self.reply(401, {'detail': 'Operator authorization required'})
                return
            if self.path != '/clear-alarm':
                self.reply(404, {'detail': 'Unknown command'})
                return
            try:
                node, alarm_id = validate_command(json.loads(raw))
            except (ValueError, OSError):
                self.reply(400, {'detail': 'Invalid clear command'})
                return
            command = ClearRequest(node, alarm_id)
            try:
                inbox.put_nowait(command)
                status, body = command.result.result(timeout=8)
            except (queue.Full, TimeoutError):
                status, body = unavailable()
            self.reply(status, body)

    # Never bind this listener to a public interface or publish it in Docker.
    server = ThreadingHTTPServer(('127.0.0.1', settings.port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name='operator-commands', daemon=True).start()
    return server


def forward_clear(settings, node, alarm_id):
    """Web proxy: local HTTP only, no environment proxy, retries or DB writes."""
    try:
        with httpx.Client(timeout=10, trust_env=False) as client:
            response = client.post(f'http://127.0.0.1:{settings.port}/clear-alarm',
                headers={'Authorization': f'Bearer {settings.token}'},
                json={'dev_eui': node, 'alarm_event_id': alarm_id})
        if response.status_code not in (200, 400, 401, 404, 409, 503):
            return unavailable()
        return response.status_code, response.json()
    except (httpx.HTTPError, ValueError):
        return unavailable()
