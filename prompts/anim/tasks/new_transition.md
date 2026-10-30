# Task: new state transition / entry path

Goal: {{GOAL}}
Wire it as state-machine transitions `{from, event, to}` (from may be "*"). Existing events: pat, poke, press, person_near, idle_timeout, night, morning, <state>_done, emotion_* (handled in code).
If the switch looks too abrupt, you may add `fade_in` / `fade_out` (seconds, 0–2) to the involved animation — then re-emit that animation in full (current JSON below), otherwise omit `animations`.

Involved animation(s) current JSON (may be empty):
```json
{{CURRENT_JSON}}
```
Output a patch with `transitions` (+ optional `animations`) and `notes`. JSON only.
