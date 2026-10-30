# Task: new emotion state

Create ONE new state `{{STATE}}` (Traditional Chinese label: 「{{LABEL}}」).
- Emotion / intensity: {{EMOTION_DESC}}
- Eyes preset: `{{EYES}}`
- Motion idea: {{MOTION}}
- Loop duration: {{DURATION}} s; plays {{ROUNDS}} rounds then returns to idle.

Output one patch with `animations.{{STATE}}`, `states.{{STATE}}`, `bake_states.{{STATE}}` (cut_every = duration×20, ≥2 variants incl. base), and `transitions` `[{"from":"{{STATE}}","event":"{{STATE}}_done","to":"idle"}]`. Timer after = {{DURATION}}×{{ROUNDS}}−0.1.
Put a one-line Chinese description of the motion in `notes`. JSON only.
