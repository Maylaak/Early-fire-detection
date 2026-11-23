# Gateway coverage study (theoretical)

How many gateways the reserve needs, and where they go on the console map. All numbers are **theory**. They plan the survey; they don't replace it.

## 1. The reserve

The grid in the team's "Pilot scale project location" figure, traced onto satellite imagery:

- about 2.16 × 1.58 km, **2.61 km²**;
- accurate to roughly ±50 m (traced by eye from the figure);
- the outline is `demo_layout.reserve` in `config.yaml`, drawn in yellow on the map.

## 2. Link budget (node → gateway)

| Item | Value |
|---|---|
| Node transmit power | 14 dBm (EU868 limit) |
| Node antenna | 2 dBi |
| Gateway antenna | 3 dBi |
| Cable and connector loss | 2 dB |
| Fade margin | 10 dB |
| Free-space loss at 1 m, 868 MHz | 31.2 dB |
| Path-loss exponent, forest | n = 4 (12 dB per doubling of distance; open air is 6 dB) |

Allowed path loss = 14 + 2 + 3 − 2 − 10 − sensitivity. Range = 1 m × 10^((allowed − 31.2) / 40).

| Spreading factor | SX126x sensitivity | Range in forest |
|---|---|---|
| SF7 | −124 dBm | 312 m |
| SF8 | −127 dBm | 371 m |
| SF9 | −130 dBm | 441 m |
| **SF10** | **−133 dBm** | **524 m** |
| SF11 | −135.5 dBm | 605 m |
| SF12 | −137 dBm | 660 m |

In open air (n = 2) the same budget reaches tens to hundreds of km, which is why LoRa range claims look so large. Under tree cover, range is set by the vegetation, not the radio.

**SF10 is the design point**: 524 m. It leaves SF11 and SF12 for nodes in bad spots, and keeps airtime acceptable for the event-driven messages.

## 3. How many gateways

- **Rough estimate.** Each gateway covers a hexagon inscribed in its 524 m circle: about 0.71 km². 2.61 / 0.71 ≈ 3.7, so **at least 4**. The reserve's irregular shape (arms to the north-east and south-west) adds one.
- **Placement.** k-means plus a minimax step on a 20 m grid of the reserve, 30 random restarts. It minimises the worst distance from any point to its nearest gateway.

With 5 gateways:

- worst-case distance to the nearest gateway: about 580 m;
- mean distance to the nearest gateway: about 290 m;
- **99.3 %** of the reserve within SF10 range (524 m), **100 %** within SF11 (605 m).

## 4. Positions

| Gateway | Lat | Lon | Sector |
|---|---|---|---|
| GW-01 north-centre | 33.55893 | 35.57111 | 0.54 km² |
| GW-02 north-west | 33.55921 | 35.56378 | 0.50 km² |
| GW-03 north-east | 33.55930 | 35.57967 | 0.50 km² |
| GW-04 south-west | 33.55192 | 35.56597 | 0.55 km² |
| GW-05 south-east | 33.55290 | 35.57659 | 0.47 km² |

- A **sector** is the part of the reserve closer to that gateway than to any other. The map draws sectors as dotted yellow lines and the 524 m range as dashed circles; both can be switched off under **Layers**.
- The 9-node pilot grid sits in GW-01's sector, **376 m** from it (inside SF10 range). GW-01 is the gateway the simulator reports through.
- **GW-01 was moved 71 m north-west of the optimiser's point.** At the optimum the grid was almost exactly as far from GW-01 (446 m) as from GW-02 (455 m), so the sector line ran through the grid. Now every node is at least 21 m closer to GW-01 than to any other gateway. The worst case and the SF10 coverage are unchanged (to within 5 m and 0.1 %).
- GW-02 to GW-05 have no nodes yet, so the console shows them as **planned**: not counted as offline, and not expected to be heard.

## 5. Caveats

- **Terrain is not modelled.** The reserve is hilly; a gateway behind a ridge can lose 20 dB or more. Real sites belong on high ground with line of sight down the slopes.
- **Gateway height is not modelled.** A gateway on a mast above the canopy does much better than n = 4 predicts.
- **The exponent is an assumption.** Measured values in forest range from about 3 to 5. A walk test with one node at several distances fixes it.
- **The outline is traced by eye** (±50 m).
- **Sites need power and access**, which the optimiser doesn't know about. Expect each position to move by up to ~100 m during the survey.
- **The simulator's signal values don't follow this model.** Its RSSI and SNR don't depend on distance, so they don't show what a node 376 m away would see (about −117 dBm).
