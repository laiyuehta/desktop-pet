# Task: one variant for 抽卡 (gacha)

You will be called many times with the same prompt; each answer is one candidate. Be bold and different from the obvious solution: vary rhythm, timing accents, which bone leads, antenna personality. Variant seed hint: {{SEED_HINT}}

Target state `{{STATE}}` — {{BRIEF}}
Eyes `{{EYES}}`, duration {{DURATION}} s, {{ROUNDS}} rounds before idle.
{{EXTRA}}

Output a complete patch for this state (animations + states + bake_states + transitions back to idle; if `{{STATE}}` already exists output only `animations.{{STATE}}`). `notes`: one Chinese line naming your idea. JSON only.
