# Integration SDK extraction V0

The adapter proof and portable wire conformance are complete prerequisites. This
increment extracts independently installable **contract SDKs** using the existing
V1 schema, without changing the engine, adapters, schema or fixture expectations.

| Package | Location | Included |
| --- | --- | --- |
| `andy-integration-sdk` | [Python](../sdks/python/README.md) | Generated typed dictionaries, validation, JSON parsing/serialization, structured errors, schema resource |
| `@attention-router/integration-sdk` | [TypeScript](../sdks/typescript/README.md) | Generated structural types and declarations, validation, JSON parsing/serialization, structured errors, schema resource |

Both are local prerelease packages; no registry release is part of this extraction.
Python depends on `jsonschema` with format checks; TypeScript depends on Ajv and
`ajv-formats`. Neither imports runtime settings, database code or provider adapters.
The contract's `schema_version: "1"` is separate from package versions.

## One source of truth

`contracts/integration/v1/integration-contract.schema.json` remains canonical.
`scripts/generate_integration_sdks.py` reads it without importing the engine and
generates structural type hints plus byte-identical schema resources. Its `--check`
mode fails on drift. Types aid development; they do not encode every constraint or
prove authority. Validators do not normalize inputs or supply constructor defaults.

The existing 63-case corpus uses `wire_valid` for SDK expectations, including cases
accepted only by native Python normalization. Each installed package exercises
validation, parsing and serialization against that corpus. Package tests also
check isolation, mutation/defaults, non-JSON inputs, error disclosure and schema
copies. Python checks a typed external consumer with mypy; TypeScript checks CJS,
ESM, declarations and a compiled external consumer.
This proves the tested cases, not all possible cross-language JSON inputs. Numbers
and parser duplicate-key behavior follow the receiving language's implementation.

The required `python-tests` job builds a wheel and installs it in a clean virtual
environment without Attention Router, Pydantic or SQLAlchemy. The required
`transport-tests` job builds, packs and installs the TypeScript tarball outside the
repository. Both run synthetic offline examples. Installation uses package
registries; no provider calls are made.

## Authority and next boundary

Explicit tenants, external actor/thread references, idempotency/correlation keys,
receipt identity and result semantics are preserved. Validation establishes only
a valid wire shape. Trusted tenant binding, tenant-scoped identity resolution,
policy/approval and execution authorization remain separate. Artifact storage
references grant no access.

The repository does not yet implement a provider-neutral integration HTTP
endpoint. [ADR 0020](adr/0020-neutral-ingress-and-authenticated-tenant-binding.md)
and the [neutral transport V0 proposal](integration-transport-v0.md) define the
next ingress/egress authority boundary, authenticated tenant binding and admission
semantics before a network client. They are design documents, not shipped HTTP
functionality. Receiver/binding proofs precede client extraction; signing, OAuth,
discovery, artifact upload and live adapters remain future work. No runtime
redesign or production/provider activation is included.
