# Behavior Certification

Run the offline harness with:

```bash
python scripts/andy_behavior_certification.py
```

The V2 harness covers 54 scenarios across family, recruiters, business clients,
unknown contacts, friends, relationship, urgency, fallback, privacy,
escalation, and follow-up behavior. It prints 24 review samples and 10
consecutive same-intent variants with input, audience, family, variant,
introduction state, promise backing, text, and spoken text.

Certification assertions include:

- no impersonation of Alex;
- introduction on first contact and no redundant reintroduction;
- no unsupported promises;
- decision and policy semantics preserved;
- non-empty text and voice-ready output when a response is required;
- recent-variant anti-repetition;
- no external delivery.
- semantic missing-information questions;
- no internal-language leakage;
- structural variation and generic-request concentration.

Human review is required before activating the profile in the production
blueprint or creating a behavior canary. `production_enabled=false` remains
explicit in the V2 profile.

V2.1 adds six four-turn conversations, known-slot non-repetition checks,
explicit-intent-first checks, callback/future-notification handling, and
capability-aware language checks.
