# Persistent binding and transactional admission proof V0

This increment extends the offline binding proof with an **internal PostgreSQL
service**. It exposes no HTTP route or client, invokes no provider or engine,
and dispatches no events. Pending rows are not proof of execution or delivery.
The transport proposal remains a proposal for the eventual HTTP boundary.

## Storage and identity

Migration `0037_integration_admission_v0` adds three isolated tables:

- `integration_bindings`: tenant, deployment audience and exact source identity;
  one stable namespace per audience/tenant/kind/name/instance/account tuple.
- `integration_credentials`: only SHA-256 digests, activation/expiration,
  revocation and scopes; multiple credentials may rotate over one binding.
- `integration_inbox`: the receipt and recoverable pending work in one row,
  including exact raw bytes, SHA-256, original correlation, server admission time
  and credential ID for provenance. No raw credential is stored.

The internal `account_key` encodes accountless as an empty string; nonempty
accounts retain their exact value. The validated domain record prohibits empty
account identifiers. This avoids SQL NULL uniqueness gaps without changing V1.
Composite foreign keys prevent inbox tenant/binding/credential mismatches.
PostgreSQL triggers prevent identity retargeting, deletion, credential
resurrection and modification of the pending inbox. They deliberately allow
binding activation and scope changes. No retention or consumer API exists yet.
Downgrade refuses any data in these tables; empty tables can be removed.
These guards protect ordinary DML, not a schema owner bypassing constraints.

## Transaction boundary

`attention_router.integrations.admission.admit_inbound` owns a fresh transaction
and returns a receipt only after its commit. Trusted deployment code supplies
session factory and audience; neither comes from the payload. Administrative
provision/revoke/disable helpers are internal trusted operations, not public APIs.

Discovery reads find server-owned identities. The transaction then locks and
rereads **tenant → binding → credential**, preserving this order in administrative
helpers. Locks remain held through the inbox commit. Tenant-wide serialization
is a deliberate conservative V0 throughput limit. Direct tenant status updates
also conflict with the tenant row lock. Unknown identities, corruption, lock
or commit failures produce no receipt. PostgreSQL is required; SQLite is refused.

Activation and expiration use PostgreSQL `clock_timestamp()` **after acquiring
locks**. `now()` would incorrectly reuse the start-of-transaction time following
a wait. Lock timeout is two seconds and statement timeout five seconds. There
is no automatic retry loop; a caller can retry an unavailable admission.

Every request, including a duplicate, reauthenticates current state. Revocation
that commits first blocks admission; admission that commits first remains a
durable pending receipt. Revocation does not undo accepted work. A future
consumer must recheck tenant/binding status before dispatch, without treating
transport credentials as actor authority or approval.

## Validation and deduplication

The private parser enforces 64 KiB, UTF-8, unique JSON members, finite numbers
and maximum nesting 64. Full canonical V1 JSON Schema and formats are validated
without native-model coercion. Parsing errors are only returned after credential
checks; unknown credentials do not disclose payload validation details.
The pinned `jsonschema[format-nongpl]` dependency moves from dev to runtime.

Both idempotency key and external event ID are unique within the bound
`(tenant, binding, inbound_event)` namespace. Exact-byte retry returns the
original receipt, timestamp and correlation; a collision with changed bytes
returns conflict. Rotation does not reset this namespace. Inbox and work cannot
commit separately because they are the same row. Connection loss after commit
is resolved by retrying the same bytes and recovering the original receipt.

## Evidence and limits

Offline parser tests and the existing binding matrix complement PostgreSQL
integration tests in `tests/integration/test_postgres_integration_admission.py`.
The latter use committed state and separate real connections, including observed
PostgreSQL lock waits for revocation ordering. They cover concurrent duplicate
and conflicting submissions, expiry after waiting, disabled tenants/bindings,
rotation, rollback before commit, retry after a lost response, timeout, namespace
isolation, constraints and immutable identities. They do not substitute SQLite
for PostgreSQL.

Run the documented public CI or disposable PostgreSQL harness. The local authoring
environment has no PostgreSQL/Docker; passing PostgreSQL CI on the exact commit
is required before claiming this proof complete. HTTP handling, rate limits,
credential issuance/delivery, production enrollment, dispatch and HTTP SDKs remain
outside this increment. No production admission is activated by these changes.
