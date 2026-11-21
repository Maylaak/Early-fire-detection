"""Geographic distances and additive console contract; only temporary SQLite."""
import math

import pytest
import yaml
from fastapi.testclient import TestClient

from gateway.config import DEFAULT_CONFIG_PATH, load_config
from gateway.geometry import grid_boundary, grid_positions
from gateway.web.app import create_app


def distance(a, b):
    # Independent haversine measurement, mean Earth radius in meters.
    lat1, lat2 = math.radians(a['lat']), math.radians(b['lat'])
    dlat, dlon = lat2-lat1, math.radians(b['lon']-a['lon'])
    h = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 6371008.8 * 2 * math.atan2(math.sqrt(h), math.sqrt(1-h))


def local_offsets(origin, point):
    lat1, lat2 = math.radians(origin['lat']), math.radians(point['lat'])
    dl = math.radians(point['lon']-origin['lon'])
    bearing = math.atan2(math.sin(dl)*math.cos(lat2),
                         math.cos(lat1)*math.sin(lat2)-math.sin(lat1)*math.cos(lat2)*math.cos(dl))
    d = distance(origin, point)
    return d*math.sin(bearing), d*math.cos(bearing)


@pytest.mark.parametrize('lat,lon', [(33.553,35.566), (0,0), (70,179.9999), (-45,-100)])
def test_grid_cell_centres_and_extent(lat, lon):
    points = grid_positions(lat, lon)
    assert len({(p['lat'],p['lon']) for p in points}) == 9
    for i, point in enumerate(points):
        east, north = local_offsets(dict(lat=lat,lon=lon), point)
        row, col = divmod(i,3)
        assert east == pytest.approx(-50+(col+0.5)*100/3, abs=0.001)
        assert north == pytest.approx(50-(row+0.5)*100/3, abs=0.001)
    corners = [dict(lat=p[0],lon=p[1]) for p in grid_boundary(lat,lon)]
    for a,b in zip(corners,corners[1:]+corners[:1]):
        assert distance(a,b) == pytest.approx(100,abs=0.001)
    # Extreme sensor centres span 66.67 m; the enclosing square spans 100 m.
    assert distance(points[0],points[2]) == pytest.approx(200/3,abs=0.001)


def test_shipped_layout_identity_and_offsets():
    cfg=load_config(env={})
    origin=cfg.demo_layout.centre.model_dump()
    assert len(cfg.sites.nodes)==len(cfg.simulator.nodes)==9
    assert [n.dev_eui for n in cfg.sites.nodes[:4]] == [f'70b3d57ed000000{i}' for i in range(1,5)]
    assert [n.label for n in cfg.sites.nodes[:4]] == ['F-07-01','F-07-02','F-07-03','F-08-01']
    gateway=cfg.sites.gateways[0]
    assert gateway.gateway_id == cfg.simulator.gateway_id == '0016c001ff100001'
    assert local_offsets(origin,gateway.model_dump()) == pytest.approx((500,0),abs=0.001)
    assert local_offsets(origin,cfg.sites.drone.model_dump()) == pytest.approx((500,60),abs=0.001)
    assert local_offsets(origin,cfg.sites.centre.model_dump()) == pytest.approx((500,-60),abs=0.001)
    assert cfg.sites.centre.label == 'Bkassine Civil Defence Centre'


def test_configurable_layout_and_reordered_registry(tmp_path):
    raw=yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding='utf-8'))
    raw['demo_layout']['centre']={'lat':60,'lon':10}
    raw['demo_layout']['gateway_distance_m']=750
    raw['demo_layout']['gateway_bearing_deg']=180
    raw['sites']['nodes'].reverse()
    path=tmp_path/'config.yaml'; path.write_text(yaml.safe_dump(raw),encoding='utf-8')
    cfg=load_config(path,env={})
    origin=cfg.demo_layout.centre.model_dump()
    assert local_offsets(origin,cfg.sites.gateways[0].model_dump()) == pytest.approx((0,-750),abs=0.001)
    assert local_offsets(origin,cfg.sites.node('70b3d57ed0000001').model_dump()) == pytest.approx((-100/3,100/3),abs=0.001)
    assert cfg.console.map.centre_lat==60


def test_layout_console_contract(engine):
    cfg=load_config(env={})
    with TestClient(create_app(cfg,engine)) as client:
        response=client.get('/api/console/config')
        assert response.status_code==200
        data=response.json()
        assert data['demo_layout']['square_size_m']==100
        assert len(data['sites']['nodes'])==9
        for role in ('drone','centre'):
            entity=data['sites'][role]
            assert entity['status']=='planned' and entity['provisional'] is True
            assert 'battery' not in entity and 'online' not in entity
        status=client.get('/api/console/status').json()
        assert len(status['gateways'])==1
        assert len(status['never_seen'])==9
        assert status['network']['total_nodes']==0
        assert data['sites']['centre']['role']=='Main Control Unit'
        assert status['pill']=='NO_DATA'

@pytest.mark.parametrize('change', ['count', 'identity', 'gateway', 'bearing'])
def test_bad_layout_rejected(tmp_path, change):
    from pydantic import ValidationError
    raw=yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding='utf-8'))
    if change=='count': raw['sites']['nodes'].pop()
    if change=='identity': raw['simulator']['nodes'][0]['dev_eui']='ffffffffffffffff'
    if change=='gateway': raw['simulator']['gateway_id']='ffffffffffffffff'
    if change=='bearing': raw['demo_layout']['gateway_bearing_deg']=360
    path=tmp_path/'config.yaml'; path.write_text(yaml.safe_dump(raw),encoding='utf-8')
    with pytest.raises(ValidationError): load_config(path,env={})


def test_legacy_explicit_positions_still_load(tmp_path):
    cfg=load_config(env={})
    raw=cfg.model_dump(); raw.pop('demo_layout')
    raw['detection'].pop('offline_after_minutes')
    raw['sites'].pop('drone')
    path=tmp_path/'legacy.yaml'; path.write_text(yaml.safe_dump(raw),encoding='utf-8')
    legacy=load_config(path,env={})
    assert legacy.demo_layout is None and legacy.sites.drone is None
    assert legacy.sites.nodes==cfg.sites.nodes
