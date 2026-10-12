# Executable authority decision demo

This reproducible example **calls the project's real, pure**
`attention_router.core.authority.evaluate_effective_authority` function.
Unlike the illustrated [architecture demo](demo.md), it evaluates actual
decision logic with synthetic inputs.

From a fresh clone, use the [local Python setup](../README.md#provider-free-demonstration-and-tests)
to install the package dependencies. Then run:

```bash
python -m examples.public_authority_demo
python -m pytest -q tests/test_public_authority_demo.py
```

The printed matrix covers seven synthetic situations:

| Scenario | Expected result | Explanation |
| --- | --- | --- |
| Declared capability, not ready | `UNAVAILABLE` | An unprovisioned capability cannot proceed |
| Policy rejects | `DENY` | Model intent cannot override policy |
| Grant missing | `DENY` | Policy permission does not replace an active grant |
| External write | `REQUIRES_APPROVAL` | Side effects trigger a human-approval requirement |
| Read under approval policy | `REQUIRES_APPROVAL` | Approval mode also covers otherwise read-only work |
| Permitted read | `ALLOW` | Available, policy-permitted, granted, non-effectful work |
| Revoked capability | `UNAVAILABLE` | Revoked availability fails closed |

## What this does **not** prove

This is a deterministic decision-function demonstration, **not** an agent
conversation, persisted permission lookup, authenticated approval, transactional
outbox, or a delivered external effect. The example supplies policy and grant
booleans as synthetic test inputs; it does not mint or verify real grants.
The `ALLOW` result must **not** be interpreted as an executable authorization
token or permission to bypass the rest of the runtime.

The example does not call a model, a provider, the network, or production
services. It requires no tenant, account, secrets, database, or runtime changes.

For the full design and boundaries, read
[architecture and trust boundaries](architecture/overview.md) and
[ADR 0012: policy outside model authority](adr/0012-policy-outside-model-authority.md).
