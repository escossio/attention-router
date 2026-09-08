# Response Variation

`ResponseCandidate` separates semantic family from linguistic realization. A family is selected from the existing decision; variants only change wording.

Each candidate has a deterministic `variant_id`, `text`, `spoken_text`, introduction marker, and promise check. Recent variant IDs are supplied by conversation state, so the renderer can avoid the latest formulations without changing the decision, policy, action, escalation, or facts.

The V2 offline profile has a compositional capacity of 2000 variants for
`REQUEST_CONTEXT` and produced 50 distinct normalized structures across 54
scenarios. Variation is secondary to semantic correctness and audience fit;
recent variants are avoided only when an equally valid alternative exists. No
audio is generated in this phase.
