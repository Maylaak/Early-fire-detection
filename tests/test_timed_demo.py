import pytest
from gateway.sim.timed_demo import wait_and_clear, main


class Clock:
    now = 0
    def time(self): return self.now
    def sleep(self, seconds): self.now += seconds


def test_timed_clear_only_at_deadline():
    clock = Clock()
    calls = []
    def status():
        return {'state': 'ALARM' if clock.now >= 3 else 'NORMAL', 'alarm_event_id': 42}
    def clear(event):
        calls.append((clock.now, event))
        return 200, {'ok': True}
    wait_and_clear(60, status, clear, clock=clock.time, sleep=clock.sleep)
    assert calls == [(60, 42)]


@pytest.mark.parametrize('case', ['no_alarm', 'new_alarm', 'unavailable'])
def test_timed_clear_failures(case):
    clock = Clock()
    calls = []
    def status():
        return {'state': 'NORMAL' if case == 'no_alarm' else 'ALARM',
                'alarm_event_id': 43 if case == 'new_alarm' and clock.now > 2 else 42}
    def clear(event):
        calls.append(event)
        return 503, {'detail': 'unavailable'}
    with pytest.raises(RuntimeError):
        wait_and_clear(5, status, clear, clock=clock.time, sleep=clock.sleep)
    assert calls == ([42] if case == 'unavailable' else [])


def test_manual_clear_during_timer_not_repeated():
    clock = Clock()
    def status():
        return {'state': 'ALARM' if clock.now < 2 else 'NORMAL', 'alarm_event_id': 42}
    wait_and_clear(5, status, lambda _: pytest.fail('unexpected clear'),
                   clock=clock.time, sleep=clock.sleep)


@pytest.mark.parametrize('seconds', ['nan', 'inf', '-1', '0', '10'])
def test_invalid_duration_before_any_io(seconds):
    with pytest.raises(SystemExit):
        main(['node-1', '--seconds', seconds])


@pytest.mark.parametrize('authorized', [True, False])
@pytest.mark.parametrize('explicit_token', [True, False])
def test_cli_authorizes_before_injecting(monkeypatch, authorized, explicit_token):
    import gateway.sim.timed_demo as demo
    from gateway.config import load_config
    cfg = load_config(env={'DEMO_SPEEDUP': '60'})
    monkeypatch.setattr(demo, 'load_config', lambda _: cfg)
    monkeypatch.delenv('WILDFIRE_LOCAL_DEMO', raising=False)
    if explicit_token:
        monkeypatch.setenv('WILDFIRE_ADMIN_TOKEN', 'test-token-' + 'x'*32)
    else:
        monkeypatch.delenv('WILDFIRE_ADMIN_TOKEN', raising=False)
    monkeypatch.setattr(demo, 'local_operator_token', lambda: 'automatic-' + 'x'*32)
    class Response:
        def __init__(self, path): self.path = path
        def raise_for_status(self): pass
        def json(self):
            if self.path.endswith('config'):
                return dict(clear_alarm_enabled=True, reporting_mode='event_driven', demo_speedup=60)
            return dict(nodes=[])
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, path): return Response(path)
    monkeypatch.setattr(demo.httpx, 'Client', Client)
    monkeypatch.setattr(demo, 'forward_clear', lambda *args: (400 if authorized else 401, {}))
    injections = []
    monkeypatch.setattr(demo, 'cmd_inject', lambda cfg, args: injections.append(args) or 0)
    waits = []
    monkeypatch.setattr(demo, 'wait_and_clear', lambda seconds, *args: waits.append(seconds))
    assert main(['node-1', '--seconds', '60']) == (0 if authorized else 1)
    assert waits == ([60] if authorized else [])
    assert len(injections) == int(authorized)
    if authorized:
        assert injections[0].hold_minutes == 50
        assert injections[0].ramp_minutes == 5


def test_local_demo_settings_opt_in_and_explicit_override(monkeypatch):
    from gateway.commands import CommandSettings
    import gateway.local_operator as credentials
    monkeypatch.delenv('WILDFIRE_ADMIN_TOKEN', raising=False)
    monkeypatch.delenv('WILDFIRE_LOCAL_DEMO', raising=False)
    calls = []
    monkeypatch.setattr(credentials, 'local_operator_token', lambda: calls.append(1) or 'x'*40)
    assert CommandSettings.from_env().token == ''
    assert not calls
    monkeypatch.setenv('WILDFIRE_LOCAL_DEMO', '1')
    assert CommandSettings.from_env().token == 'x'*40
    monkeypatch.setenv('WILDFIRE_ADMIN_TOKEN', 'y'*40)
    assert CommandSettings.from_env().token == 'y'*40
    assert calls == [1]
