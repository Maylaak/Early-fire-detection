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
    # Five gateways from the coverage study; GW-01 serves the pilot grid and is
    # the simulator's gateway, about 375 m from the grid centre.
    gws=cfg.sites.gateways
    assert [g.gateway_id for g in gws] == [f'0016c001ff10000{i}' for i in range(1,6)]
    gateway=gws[0]
    assert gateway.gateway_id == cfg.simulator.gateway_id
    assert distance(origin, gateway.model_dump()) == pytest.approx(376, abs=5)
    # The whole grid is in GW-01's sector: every node clearly nearest to it.
    for n in cfg.sites.nodes:
        own = distance(n.model_dump(), gateway.model_dump())
        assert all(distance(n.model_dump(), g.model_dump()) - own > 15 for g in gws[1:])
    # GW-01 is the pilot grid's nearest gateway.
    assert min(gws, key=lambda g: distance(origin, g.model_dump())) == gateway
    # Every node belongs to the pilot gateway, and keeps its planned cell.
    assert {n.gateway_id for n in cfg.sites.nodes} == {gateway.gateway_id}
    assert not any(n.surveyed for n in cfg.sites.nodes)
    # Main Control Unit in the village to the west, drone base mid-reserve.
    east, north = local_offsets(origin, cfg.sites.centre.model_dump())
    assert east < -500
    assert cfg.sites.centre.label == 'Main Control Unit - Bkassine municipality'
    reserve=cfg.demo_layout.reserve
    lats=[p[0] for p in reserve]; lons=[p[1] for p in reserve]
    assert min(lats) < cfg.sites.drone.lat < max(lats) and min(lons) < cfg.sites.drone.lon < max(lons)
    # The pilot grid lies inside the reserve.
    assert min(lats) < origin['lat'] < max(lats) and min(lons) < origin['lon'] < max(lons)


def test_configurable_layout_and_reordered_registry(tmp_path):
    raw=yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding='utf-8'))
    # A single gateway with no position is placed by distance and bearing.
    first=raw['sites']['gateways'][0]
    raw['sites']['gateways']=[{'gateway_id':first['gateway_id'],'label':first['label']}]
    raw['demo_layout'].pop('reserve')
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
        assert len(status['gateways'])==5
        assert [g['planned'] for g in status['gateways']]==[False,True,True,True,True]
        assert status['network']['gateways_planned']==4
        assert [g['planned'] for g in data['sites']['gateways']]==[False,True,True,True,True]
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


def test_surveyed_gps_position_is_kept(tmp_path):
    """A node with lat/lon in config.yaml stays where it was installed."""
    raw=yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding='utf-8'))
    raw['sites']['nodes'][2].update(lat=33.55312, lon=35.56587)
    path=tmp_path/'config.yaml'; path.write_text(yaml.safe_dump(raw),encoding='utf-8')
    cfg=load_config(path,env={})
    node=cfg.sites.node('70b3d57ed0000003')
    assert (node.lat, node.lon, node.surveyed) == (33.55312, 35.56587, True)
    others=[n for n in cfg.sites.nodes if n.dev_eui != node.dev_eui]
    assert not any(n.surveyed for n in others)


def test_half_a_gps_position_is_rejected(tmp_path):
    raw=yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding='utf-8'))
    raw['sites']['nodes'][0]['lat']=33.5
    path=tmp_path/'config.yaml'; path.write_text(yaml.safe_dump(raw),encoding='utf-8')
    with pytest.raises(ValueError, match='both lat and lon'): load_config(path,env={})


def test_node_gateway_must_exist_and_is_required_with_several():
    from pydantic import ValidationError
    from gateway.config import SitesConfig
    cfg=load_config(env={})
    base=cfg.sites.model_dump()
    bad={**base,'nodes':[{**base['nodes'][0],'gateway_id':'ffffffffffffffff'}]}
    with pytest.raises(ValidationError, match='not a site gateway'): SitesConfig.model_validate(bad)
    two={**base,'gateways':base['gateways'][:1]+[{**base['gateways'][0],'gateway_id':'0016c001ff100002','label':'GW-02'}],
         'nodes':[{**base['nodes'][0],'gateway_id':None}]}
    with pytest.raises(ValidationError, match='set gateway_id'): SitesConfig.model_validate(two)
    two['nodes'][0]['gateway_id']='0016C001FF100002'
    assert SitesConfig.model_validate(two).nodes[0].gateway_id=='0016c001ff100002'


def test_network_tree_gateway_clusters_nodes(engine):
    cfg=load_config(env={})
    with TestClient(create_app(cfg,engine)) as client:
        status=client.get('/api/console/status').json()
    tree=status['network_tree']
    assert [g['label'] for g in tree]==['GW-01 north-centre','GW-02 north-west','GW-03 north-east',
                                        'GW-04 south-west','GW-05 south-east']
    assert [g['planned'] for g in tree]==[False,True,True,True,True]
    assert all(g['nodes']==0 and g['clusters']==[] for g in tree[1:])
    gw=tree[0]
    assert gw['nodes']==9 and gw['state']=='NEVER'
    assert [c['cluster'] for c in gw['clusters']]==['F-07','F-08','F-09']
    assert all(len(c['nodes'])==3 and c['state']=='NEVER' for c in gw['clusters'])
    for n in status['never_seen']:
        assert n['gateway_label']=='GW-01 north-centre' and n['surveyed'] is False
        assert n['days_missing'] is None  # nothing logged yet: monitoring has not begun


def test_reserve_sectors_cover_it_once_and_follow_the_coverage_study(engine):
    """The five sectors split the reserve without gaps or overlaps, each point
    to its nearest gateway, and the worst case is within the SF11 range."""
    from gateway.geometry import polygon_area_m2
    cfg=load_config(env={})
    with TestClient(create_app(cfg,engine)) as client:
        d=client.get('/api/console/config').json()['demo_layout']
    assert d['gateway_range_m']==524
    assert len(d['sectors'])==5 and all(len(s['polygon'])>=3 for s in d['sectors'])
    total=sum(polygon_area_m2(s['polygon']) for s in d['sectors'])
    # Areas are projected around each ring's own first corner: tiny scale differences.
    assert total == pytest.approx(polygon_area_m2(d['reserve']), rel=1e-3)
    assert 2.4 < d['reserve_area_km2'] < 2.8
    gws={g.gateway_id: g.model_dump() for g in cfg.sites.gateways}
    for s in d['sectors']:
        g=gws[s['gateway_id']]
        for lat, lon in s['polygon']:
            p={'lat':lat,'lon':lon}
            # Every corner is nearest to its own gateway (ties on borders allowed)...
            assert distance(p,g) <= min(distance(p,h) for h in gws.values()) + 0.5
            # ...and no further than the SF11 range (605 m) from it.
            assert distance(p,g) < 605


def test_half_a_reserve_or_gateway_position_is_rejected(tmp_path):
    raw=yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding='utf-8'))
    raw['sites']['gateways'][2].pop('lon')
    path=tmp_path/'config.yaml'; path.write_text(yaml.safe_dump(raw),encoding='utf-8')
    with pytest.raises(ValueError, match='give its lat and lon'): load_config(path,env={})
    raw=yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding='utf-8'))
    raw['demo_layout']['reserve']=raw['demo_layout']['reserve'][:2]
    path.write_text(yaml.safe_dump(raw),encoding='utf-8')
    with pytest.raises(ValueError, match='at least three points'): load_config(path,env={})
