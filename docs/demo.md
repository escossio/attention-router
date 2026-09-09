# Public architecture demo

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
