# Data model (extracted from frontend/)

Three JSON files define every expression. The bake script (`frontend/tools/bake.py` → `pet/baker.py`) renders them to 800×480 JPEG clips; the Pi only plays the pack.

## 1. Skeleton animation — `frontend/assets/skeletons/seed.json` → `animations.<name>`
```
{"duration": 3.2, "loop": true, "fade_in": 0.25?, "fade_out": 0.25?,
 "tracks": {"<bone>": {"<channel>": [[t, value, "ease"], ..., [duration, value]]}}}
```
- Sampling: `sample_track` interpolates from key i to i+1 using key i's ease (`linear` default, `ease` = smooth in-out, `step` = hold). Before first/after last key: clamp.
- Channel meaning (relative to bone rest): `x`,`y` pixels added to pivot (y down is +); `rot` degrees added to rest rot (clockwise +); `sx`,`sy` multiply rest scale. Missing channel = neutral (0 or 1).
- `fade_in`/`fade_out`: crossfade seconds when switching into/out of this animation (default 0.25). The baker auto-generates visual transition clips between all state pairs from these; you don't hand-draw transitions.

Bones (canvas 800×480, parent → child):
| bone | parent | pivot | notes |
|---|---|---|---|
| root | – | (400,440) floor contact point | moves/rotates whole pet; shadow follows root x |
| body | root | (0,−158) ball center | ball image r≈156 |
| spin | body | (0,0) | seams layer; rotate = roll/tilt head (face+antennae follow) |
| face | spin | (0,0) | eyes at (±66,10), mouth (0,72), diamond (0,−60) — drawn by code |
| antenna_l | spin | (−34,−136), rest rot −14 | z behind body |
| antenna_r | spin | (34,−136), rest rot +14 | |

## 2. State — `frontend/config/states.json` → `states.<name>`
```
{"label": "垂眼", "animation": "sad_1", "eyes": "sad",
 "timers": [{"after": 6.3, "event": "sad_1_done", "reset_on_input": false}]}
```
`transitions`: list of `{"from": state|"*", "event": name, "to": state}`; specific `from` beats `"*"`. Emotion states return via `<name>_done → idle`. Timer `after = duration × rounds − 0.1` so it fires just before frame 0.

Eye presets (`pet/eyes.py` EXPRESSIONS; shape/open/glow/size/look/mouth):
normal (round, open) · happy (^ ^, mouth open) · bored (half open, look down, flat) · sleep (closed ︶) · squint (open .35) · surprised (big 1.15, bright, mouth open) · sleepy (open .25) · sad (open .7, dim, look down, flat).

## 3. Bake variants — `frontend/config/bake.json` → `states.<name>`
```
{"cut_every": 64, "variants": [
  {"name": "base", "weight": 1},
  {"name": "blink", "weight": 2, "blinks": [1.4]},
  {"name": "look_l", "weight": 1, "looks": [[0.4, -0.7, 0.2], [2.2, 0.0, 0.0]]}]}
```
fps 20. `cut_every` = frames between allowed cut points; emotion states use one full round (duration×20) so the motion always finishes. Variants share the pose, only eyes differ; player picks by weight. `looks` = [time, x, y], return to [t, 0, 0].

## Real example A — `sad_1`「垂眼」(Lv1: slight sink, slow shallow breath, antennae droop)
```json
{"duration":3.2,"loop":true,"tracks":{
 "root":{"sy":[[0.0,0.975,"ease"],[1.6,0.965,"ease"],[3.2,0.975]],"sx":[[0.0,1.012,"ease"],[1.6,1.018,"ease"],[3.2,1.012]],"rot":[[0.0,-1.2,"ease"],[1.6,1.2,"ease"],[3.2,-1.2]]},
 "body":{"y":[[0.0,5,"ease"],[1.6,8,"ease"],[3.2,5]]},
 "antenna_l":{"rot":[[0.0,-22,"ease"],[1.6,-28,"ease"],[3.2,-22]]},
 "antenna_r":{"rot":[[0.0,26,"ease"],[1.6,21,"ease"],[3.2,26]]}}}
```
## Real example B — `happy_2`「開心」(Lv2: alternating side hops 0.6 s each, 30 px, squash on land)
```json
{"duration":1.2,"loop":true,"tracks":{"root":{
 "y":[[0.0,0,"ease"],[0.25,-30,"ease"],[0.5,0,"linear"],[0.6,0,"ease"],[0.85,-30,"ease"],[1.1,0,"linear"],[1.2,0]],
 "x":[[0.0,16,"ease"],[0.5,-16,"linear"],[0.6,-16,"ease"],[1.1,16,"linear"],[1.2,16]],
 "rot":[[0.0,0,"ease"],[0.25,-7,"ease"],[0.5,0,"linear"],[0.6,0,"ease"],[0.85,7,"ease"],[1.1,0,"linear"],[1.2,0]],
 "sy":[[0.0,0.92,"ease"],[0.1,1.05,"ease"],[0.3,1.0,"linear"],[0.5,1.0,"ease"],[0.55,0.9,"ease"],[0.7,1.05,"ease"],[0.9,1.0,"linear"],[1.1,1.0,"ease"],[1.15,0.9,"ease"],[1.2,0.92]],
 "sx":[[0.0,1.07,"ease"],[0.1,0.97,"ease"],[0.3,1.0,"linear"],[0.5,1.0,"ease"],[0.55,1.09,"ease"],[0.7,0.97,"ease"],[0.9,1.0,"linear"],[1.1,1.0,"ease"],[1.15,1.09,"ease"],[1.2,1.07]]},
 "antenna_l":{"rot":[[0.0,-14,"ease"],[0.3,14,"ease"],[0.6,-14,"ease"],[0.9,14,"ease"],[1.2,-14]]},
 "antenna_r":{"rot":[[0.0,14,"ease"],[0.3,-14,"ease"],[0.6,14,"ease"],[0.9,-14,"ease"],[1.2,14]]}}}
```
State: `{"label":"開心","animation":"happy_2","eyes":"happy","timers":[{"after":3.5,"event":"happy_2_done","reset_on_input":false}]}` (1.2×3−0.1). Bake: `cut_every` 24, variants base(2)/look_l(1)/look_r(1).

## Existing states (don't reuse names unless tweaking)
idle, happy, bored, sleep, happy_1..3, surprised_1..3, sad_1..3, shy, annoyed_1..2. Emotion caps (`states.json` emotions.levels): happy 3, surprised 3, sad 3, shy 1, annoyed 2.

## Full patch example (new state)
```json
{"animations":{"sleepy_1":{"duration":3.2,"loop":true,"tracks":{"body":{"y":[[0,4,"ease"],[1.6,9,"ease"],[3.2,4]]},"spin":{"rot":[[0,0,"ease"],[1.6,4,"ease"],[3.2,0]]}}}},
 "states":{"sleepy_1":{"label":"想睡","animation":"sleepy_1","eyes":"sleepy","timers":[{"after":6.3,"event":"sleepy_1_done","reset_on_input":false}]}},
 "bake_states":{"sleepy_1":{"cut_every":64,"variants":[{"name":"base","weight":1},{"name":"blink","weight":2,"blinks":[1.5]}]}},
 "transitions":[{"from":"sleepy_1","event":"sleepy_1_done","to":"idle"}],
 "notes":"慢慢點頭打瞌睡"}
```
