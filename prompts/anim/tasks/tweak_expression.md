# Task: tweak an existing expression

Rewrite the animation `{{STATE}}`. Keep its name, duration ({{DURATION}} s unless told otherwise) and overall meaning.
Change request: {{CHANGE}}

Current animation JSON:
```json
{{CURRENT_JSON}}
```
Output a patch with ONLY `animations.{{STATE}}` (full replacement, all tracks) and `notes` (Chinese, what you changed). Do not emit states/bake_states unless the duration changed (then also update the timer `after` and `cut_every`). JSON only.
