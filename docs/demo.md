# Public architecture demo

Portfolio assets: [MP4 demo](assets/demo/attention-router-demo.mp4) ·
[poster](assets/demo/attention-router-demo-poster.png).

The repository includes a `DETERMINISTIC OFFLINE DEMO` that illustrates the
Attention Router flow: synthetic inbound event, context assembly, deterministic
proposal, policy decision, human approval boundary, outbox execution, and
synthetic delivery evidence.

It uses no WhatsApp account, browser profile, provider, network credential,
real name, phone number, or real message. Run it from a fresh clone with:

```bash
python examples/public_architecture_demo.py
```

This demonstrates architecture contracts and human control; it is not a live
conversation or an LLM/provider claim.

The visual sequence makes the authority boundary explicit: an agent proposal
does not execute by itself; policy requires human approval, execution is queued
in an outbox, and delivery evidence is recorded as a separate state.
