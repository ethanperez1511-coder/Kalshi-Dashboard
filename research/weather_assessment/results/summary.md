# Weather candidates: walk-forward results

Generated 2026-10-15 22:35Z. Leads (1, 2, 3); 19 stations; window 90 days; history floor 60.

Scored station-days (common set, all candidates present): 43,473

## Overall (all stations, leads 1-3)

| candidate | ladder Brier | vs incumbent (95% CI) | bucket Brier | vs incumbent (95% CI) | BSS vs climatology | reliability slope | mean log score |
|---|---|---|---|---|---|---|---|
| INC  MEX+bias90 (production) | 0.1230 | +0.0% (+0.0% to +0.0%) | 0.1205 | +0.0% (+0.0% to +0.0%) | 0.502 | 1.01 | -2.640 |
| REC3 20/30/50 | 0.1390 | +13.0% (+11.5% to +14.4%) | 0.1236 | +2.5% (+2.2% to +2.8%) | 0.437 | 0.97 | -2.757 |
| REC5 10/10/15/25/40 | 0.1337 | +8.7% (+7.3% to +10.1%) | 0.1227 | +1.8% (+1.5% to +2.0%) | 0.458 | 0.99 | -2.715 |
| REC5 equal | 0.1319 | +7.2% (+5.8% to +8.6%) | 0.1223 | +1.5% (+1.2% to +1.7%) | 0.466 | 0.99 | -2.700 |
| REC7 5/5/10/10/15/25/30 | 0.1306 | +6.2% (+4.9% to +7.5%) | 0.1221 | +1.3% (+1.0% to +1.5%) | 0.471 | 1.00 | -2.692 |
| REC7 equal | 0.1285 | +4.5% (+3.2% to +5.8%) | 0.1217 | +0.9% (+0.7% to +1.2%) | 0.480 | 1.00 | -2.676 |
| SPREAD NBE xnd sd, MEX mean | 0.1220 | -0.8% (-1.0% to -0.6%) | 0.1201 | -0.4% (-0.5% to -0.3%) | 0.506 | 1.00 | -2.609 |
| SPREAD+SRC2 blend mean, xnd sd | 0.1060 | -13.8% (-15.2% to -12.5%) | 0.1156 | -4.1% (-4.5% to -3.7%) | 0.571 | 1.00 | -2.476 |
| SRC2 MEX+NBE blend | 0.1071 | -12.9% (-14.3% to -11.6%) | 0.1164 | -3.4% (-3.8% to -3.1%) | 0.566 | 1.00 | -2.514 |
| SRC2 NBE alone | 0.1099 | -10.6% (-12.5% to -8.8%) | 0.1172 | -2.8% (-3.2% to -2.3%) | 0.555 | 1.00 | -2.539 |
| TAILS Student-t (df fit) | 0.1227 | -0.2% (-0.4% to -0.1%) | 0.1203 | -0.2% (-0.4% to -0.1%) | 0.503 | 0.98 | -2.612 |

## By lead: ladder Brier change vs incumbent

| candidate | L1 | L2 | L3 |
|---|---|---|---|
| INC  MEX+bias90 (production) | +0.0% | +0.0% | +0.0% |
| REC3 20/30/50 | +11.6% | +12.1% | +15.0% |
| REC5 10/10/15/25/40 | +7.3% | +8.2% | +10.5% |
| REC5 equal | +5.7% | +7.0% | +8.6% |
| REC7 5/5/10/10/15/25/30 | +4.7% | +5.8% | +7.8% |
| REC7 equal | +3.2% | +4.6% | +5.4% |
| SPREAD NBE xnd sd, MEX mean | -0.9% | -0.9% | -0.6% |
| SPREAD+SRC2 blend mean, xnd sd | -14.5% | -14.0% | -13.0% |
| SRC2 MEX+NBE blend | -13.4% | -13.0% | -12.5% |
| SRC2 NBE alone | -10.6% | -10.8% | -10.6% |
| TAILS Student-t (df fit) | -0.3% | -0.2% | -0.2% |

## Cells (station x lead) where the candidate beats the incumbent on ladder Brier

- INC  MEX+bias90 (production): 0 of 57
- REC3 20/30/50: 0 of 57
- REC5 10/10/15/25/40: 0 of 57
- REC5 equal: 1 of 57
- REC7 5/5/10/10/15/25/30: 1 of 57
- REC7 equal: 6 of 57
- SPREAD NBE xnd sd, MEX mean: 54 of 57
- SPREAD+SRC2 blend mean, xnd sd: 57 of 57
- SRC2 MEX+NBE blend: 57 of 57
- SRC2 NBE alone: 55 of 57
- TAILS Student-t (df fit): 44 of 57

## Option 5: shrink toward the market (lead 1, Kalshi's real strikes)

Events with a usable bid/ask mid at 17:00 UTC the day before: 6,717. Kalshi result vs GHCN-implied outcome agree on 6,717 of 6,717.

| model | events | days | model Brier | market Brier | blend Brier | mean weight on model |
|---|---|---|---|---|---|---|
| INC  MEX+bias90 (production) | 6,381 | 71 | 0.1325 | 0.1153 | 0.1153 | 0.08 |
| REC3 20/30/50 | 6,381 | 71 | 0.1412 | 0.1153 | 0.1154 | 0.03 |
| REC5 10/10/15/25/40 | 6,381 | 71 | 0.1382 | 0.1153 | 0.1154 | 0.04 |
| REC5 equal | 6,381 | 71 | 0.1363 | 0.1153 | 0.1154 | 0.04 |
| REC7 5/5/10/10/15/25/30 | 6,381 | 71 | 0.1365 | 0.1153 | 0.1154 | 0.05 |
| REC7 equal | 6,381 | 71 | 0.1350 | 0.1153 | 0.1154 | 0.05 |
| SPREAD NBE xnd sd, MEX mean | 6,381 | 71 | 0.1304 | 0.1153 | 0.1151 | 0.14 |
| SPREAD+SRC2 blend mean, xnd sd | 6,381 | 71 | 0.1244 | 0.1153 | 0.1153 | 0.17 |
| SRC2 MEX+NBE blend | 6,381 | 71 | 0.1264 | 0.1153 | 0.1155 | 0.08 |
| SRC2 NBE alone | 6,381 | 71 | 0.1293 | 0.1153 | 0.1156 | 0.06 |
| TAILS Student-t (df fit) | 6,381 | 71 | 0.1309 | 0.1153 | 0.1152 | 0.12 |

Incumbent blended with market vs incumbent alone: -13.0% Brier (95% CI -14.6% to -11.0%).
