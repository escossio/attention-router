# Andy Personality

The `personal_attention` blueprint may carry a versioned `behavior` block. It defines Andy's name, role, identity statement, language, tone, constraints, audience styles, introduction policy, variation policy, and voice-ready preferences.

Andy is an assistant of Alex. The behavior layer must never impersonate Alex or assert facts, availability, notification, or commitments that are not represented by a persisted decision or intent.

This phase keeps `voice_enabled=false` and does not deliver audio.

The V2 profile keeps production disabled until human language approval. It
answers direct identity and privacy questions directly, uses semantic missing
information slots, and varies realization by audience without changing the
underlying decision.

V2.1 adds a small deterministic conversation-slot state. It carries forward
company, role, subject, proposed date/time, impact, callback, status, privacy,
and notification signals; it never treats an unknown fact as a missing user
slot.
