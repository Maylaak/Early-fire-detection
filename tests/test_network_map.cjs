// Run from project root: node tests/test_network_map.cjs
// Behavioral map tests with a minimal Leaflet/DOM double; no network or database.
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
let reduced=false;
const elements=new Map(),markers=[];
const element=()=>({textContent:'',innerHTML:'',classList:{toggle(){}},hidden:false});
const map={zoom:15,layers:new Set(),handlers:{},setView(p,z){this.zoom=z;this.handlers.zoomend?.();return this},on(k,f){this.handlers[k]=f},addLayer(g){this.layers.add(g);return this},removeLayer(g){this.layers.delete(g)},hasLayer(g){return this.layers.has(g)},getZoom(){return this.zoom},fitBounds(b,o){this.bounds=b;this.zoom=o.maxZoom;this.handlers.zoomend?.()},getBoundsZoom(){return 19},flyTo(p,z){this.flew=true;this.setView(p,z)},invalidateSize(){}};
function group(opts={}){return {options:opts,items:new Set(),addTo(m){m.addLayer(this);return this},addLayers(xs){xs.forEach(x=>this.items.add(x))},addLayer(x){this.items.add(x);return this},clearLayers(){this.items.clear()},removeLayer(x){this.items.delete(x)},refreshClusters(){}}}
function layer(p,options={}){return {pos:p,options,handlers:{},bindTooltip(s,o){this.tip=s;this.tipOptions=o;return this},bindPopup(s){this.popup=s;return this},setTooltipContent(s){this.tip=s},setPopupContent(s){this.popup=s},getPopup(){return this.popup},setIcon(i){this.options.icon=i},setZIndexOffset(){},setLatLng(p){this.pos=p;return this},setStyle(o){this.style=o;return this},setContent(s){this.content=s;return this},on(k,f){this.handlers[k]=f;return this},addTo(g){g.items?.add(this);return this}}}
const L={map:()=>map,point:(x,y)=>[x,y],tileLayer:()=>layer(),layerGroup:group,markerClusterGroup:group,marker:(p,o)=>{const m=layer(p,o);markers.push(m);return m},divIcon:o=>o,circle:layer,polygon:layer,polyline:layer,tooltip:()=>layer(),latLngBounds:p=>({points:p,getCenter:()=>[0,0]}),control:()=>({addTo(){this.onAdd?.()}}),DomUtil:{create:element},DomEvent:{disableClickPropagation(){}}};L.control.layers=()=>({addTo(){}});
const ctx={assert,L,console,clearAlarmButton:()=>"",window:{L,innerWidth:1300,innerHeight:900,matchMedia:()=>({matches:reduced})},document:{title:'Test',getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id)}},ResizeObserver:class{observe(){}},esc:s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),fmtAgo:()=> 'never',badge:()=>'',fmt:()=>'-',gasVs:()=>'-'};
vm.createContext(ctx);
const source=fs.readFileSync('gateway/web/static/console.js','utf8');vm.runInContext(source.slice(0,source.indexOf('// --- status')),ctx);
vm.runInContext(`
CC={map:{centre_lat:0,centre_lon:0,zoom:15,node_detail_zoom:17,cluster_radius_px:50,primary_tiles:{max_zoom:19},fallback_tiles:{max_zoom:19}},sites:{centre:{lat:0,lon:1,label:'<control>',entity_id:'MCU',role:'Main Control Unit'},drone:{lat:1,lon:1,label:'Drone',entity_id:'UAV'},nodes:Array.from({length:9},(_,i)=>({dev_eui:String(i),label:'N'+i,lat:Math.floor(i/3),lon:i%3})),gateways:[]},demo_layout:{square_size_m:100,rows:3,columns:3,boundary:[[0,0],[0,2],[2,2],[2,0]]}};
initMap();
assert.equal(layers.connections.items.size,12);
assert([...layers.connections.items].every(l=>l.options.interactive===false));
const edges = [...layers.connections.items].map(l => JSON.stringify(l.pos));
assert.equal(new Set(edges).size, 12);
assert([...layers.connections.items].every(l => {
  const [a,b] = l.pos;
  return Math.abs(a[0]-b[0]) + Math.abs(a[1]-b[1]) === 1;
}));
assert(map.hasLayer(layers.overview));assert(!map.hasLayer(layers.nodes));
assert.equal(layers.nodes.options.disableClusteringAtZoom,18);
const alarm={...CC.sites.nodes[0],state:'ALARM',missing:true,last_seen:null};
const status={nodes:[alarm],never_seen:CC.sites.nodes.slice(1),unplaced:{count:0,alerting:[]},alarm_zones:[],gateways:[{gateway_id:'GW',label:'<gateway>',lat:0,lon:1,online:false,last_seen:null}]};
drawMap(status);
assert.equal(nodeMarkers.size,9);assert.equal(alarmMarkers.size,1);
assert(networkMarker.options.icon.html.includes('1 FIRE ALARM'));
assert(networkMarker.options.icon.html.includes('8 never seen'));
assert(map.hasLayer(layers.alarms));
const gateway=gatewayMarkers.get('GW'),node=nodeMarkers.get('0'),summary=networkMarker;
for(let i=0;i<30;i++) {map.zoom=i%2?18:15;applyDetail();drawMap(status);}
assert.equal(gatewayMarkers.size,1);assert.equal(gatewayMarkers.get('GW'),gateway);
assert.equal(nodeMarkers.get('0'),node);assert.equal(networkMarker,summary);
assert.equal(layers.connections.items.size,12);assert.equal(layers.gateways.items.size,1);
assert(map.hasLayer(layers.nodes));assert(map.hasLayer(layers.connections));assert(!map.hasLayer(layers.overview));
assert.equal(nodeMapState(alarm),'ALARM');assert.equal(nodeMapState({state:'NORMAL',missing:true}),'MISSING');
map.zoom=15;applyDetail();networkMarker.handlers.click();assert.equal(map.getZoom(),19);
assert(map.hasLayer(layers.nodes));
assert(gateway.popup.includes('&lt;gateway&gt;'));assert(gateway.tip.includes('never heard'));
assert.equal(nodeMapState({state:'WATCH',missing:true}),'WATCH');
status.alarm_zones = [{polygon:[[0,0],[0,1],[1,0]], label:'Sensor coverage <test>'}];
drawMap(status);
assert(map.hasLayer(layers.zones));assert.equal(layers.zones.items.size,1);
assert.equal(layers.coverage.items.size,2);
assert(!map.hasLayer(layers.coverage));
assert.equal(alarmRadiusMeters(),15);
for (const z of layers.zones.items) { assert.equal(z.options.radius,15); assert.equal(z.options.stroke,false); assert.equal(z.options.interactive,false); }
assert([...layers.coverage.items].some(l=>l.content?.includes('&lt;test&gt;')));
map.zoom=15;applyDetail();assert(!map.hasLayer(layers.zones));
assert(map.hasLayer(layers.alarms));assert(networkMarker.options.icon.html.includes('1 FIRE ALARM'));
status.nodes = [{...alarm, state:'NORMAL',missing:false}];status.gateways=[];
drawMap(status);assert.equal(alarmMarkers.size,0);assert.equal(gatewayMarkers.size,0);
assert.equal(layers.gateways.items.size,0);assert(!networkMarker.options.icon.html.includes('FIRE ALARM'));
status.nodes = CC.sites.nodes.map(n => ({...n,state:'ALARM',missing:true}));
for (const zoom of [18,19,18,15,19]) {
  map.zoom=zoom;applyDetail();drawMap(status);
  assert.equal(layers.zones.items.size,9);
  assert.equal(new Set([...layers.zones.items].map(z=>JSON.stringify(z.pos))).size,9);
  assert([...layers.zones.items].every(z=>z.options.radius===15 && z.options.stroke===false));
  assert.equal(alarmMarkers.size,9);
  assert.equal(map.hasLayer(layers.zones),zoom>=18);
}

`,ctx);
assert(markers.find(m=>m.popup?.includes('&lt;control&gt;')).popup.includes('no operational telemetry'));
assert(markers.find(m=>m.popup?.includes('UAV')).tip.includes('planned'));
assert(map.flew);reduced=true;map.flew=false;
vm.runInContext('networkMarker.handlers.click()',ctx);assert(!map.flew);
const css=fs.readFileSync('gateway/web/static/console.css','utf8');assert(css.includes('@media (prefers-reduced-motion: reduce)'));assert(css.includes('pointer-events: none'));
// Legend lists only what this configuration can show.
vm.runInContext(`
CC.reporting_mode='event_driven'; CC.features={drone_simulation:true};
let text=legendRows().join('');
assert(!/watch/i.test(text) && !/warming/i.test(text), 'event mode never shows WATCH or warm-up');
assert(!text.includes('Group of nodes'), 'demo grid never groups nodes');
assert(!text.includes('planned, no nodes') && !text.includes('Reserve'), 'no planned gateway or reserve configured');
assert(text.includes('Drone base (simulated flights)') && text.includes('Fire alarm location'));
CC.reporting_mode='legacy'; CC.sites.gateways=[{gateway_id:'a',planned:false},{gateway_id:'b',planned:true}];
CC.demo_layout.reserve=[[0,0],[0,1],[1,1]]; CC.demo_layout.gateway_range_m=524;
text=legendRows().join('');
assert(text.includes('Node: watch') && text.includes('missing / warming up'));
assert(text.includes('planned, no nodes yet') && text.includes('Reserve boundary') && text.includes('(524 m)'));
`,ctx);
// Map drill-down: all gateways -> gateway -> cluster -> nodes, in step with the panel.
vm.runInContext(`
CC.sites.gateways=[{gateway_id:'GW',label:'GW-01',lat:0,lon:1,planned:false},{gateway_id:'P',label:'GW-02',lat:5,lon:5,planned:true}];
CC.sites.nodes.forEach((n,i)=>{n.gateway_id='GW';n.block='F-0'+(7+Math.floor(i/3));});
CC.demo_layout.sectors=[{gateway_id:'GW',polygon:[[0,0],[0,2],[2,2]]},{gateway_id:'P',polygon:[[4,4],[4,6],[6,6]]}];
initReserve(); initClusterLinks();
var opened=[]; openNetwork=(g,c)=>{opened.push([g,c]);focusMap(g,c)};
assert.equal(clusterGeo.size,3);
map.zoom=15; focusMap(null,null);
assert(map.hasLayer(layers.overview) && !map.hasLayer(layers.clusterTags) && mapCrumbs.hidden);
openNetwork('GW',null);
assert.equal(map.getZoom(),detailZoom()-1,'a gateway stops short of node detail');
assert(!map.hasLayer(layers.overview) && map.hasLayer(layers.clusterTags),'its clusters replace the summary');
assert.equal(layers.clusterTags.items.size,3);
assert.equal(sectorShapes.get('GW').shape.style,SECTOR_ON); assert.equal(sectorShapes.get('P').shape.style,SECTOR_OFF);
assert(!mapCrumbs.hidden && mapCrumbs.innerHTML.includes('GW-01') && mapCrumbs.innerHTML.includes('All gateways'));
const chip=clusterGeo.get('GW|F-08').chip;
chip.handlers.click();
assert.deepEqual(opened.at(-1),['GW','F-08']);
assert(map.getZoom()>=detailZoom() && map.hasLayer(layers.nodes),'a cluster zooms to its nodes');
assert.equal(layers.focus.items.size,1); assert(mapCrumbs.innerHTML.includes('Cluster F-08'));
assert(chip.options.icon.html.includes('selected'));
lastTree=[{gateway_id:'GW',clusters:[{cluster:'F-07',state:'ALARM'}]}]; drawClusterChips();
assert(clusterGeo.get('GW|F-07').chip.options.icon.html.includes('FIRE ALARM'));
openNetwork('P',null);
assert.equal(map.getZoom(),detailZoom()-1); assert.equal(layers.focus.items.size,0);
assert.equal(layers.clusterTags.items.size,0,'a planned gateway has no clusters');
assert(mapCrumbs.innerHTML.includes('GW-02'));
openNetwork(null,null);
assert(mapFocus.gw===null && map.hasLayer(layers.overview) && mapCrumbs.hidden);
assert.equal(map.bounds,siteView.bounds,'All gateways returns to the whole site');
assert.equal(sectorShapes.get('GW').shape.style,SECTOR_OFF);
`,ctx);
console.log('PASS: map drill-down gateway -> cluster -> nodes, sector highlight, breadcrumb, planned gateway, back to all;');
console.log('PASS: legend shows only working items;');
console.log('PASS: grid edges, zoom modes, clustering threshold, alarms, stable markers across 30 transitions, escaped/planned assets, click-to-grid, reduced motion.');
