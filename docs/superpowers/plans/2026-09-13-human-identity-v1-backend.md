# Human Identity V1 Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the backend Human Identity V1 boundary for Google-backed human authentication using a short-lived single-use challenge, server-side Google ID-token verification, and an opaque Attention Router Human Identity keyed by `google:sub`, without creating tenant membership, device enrollment, application sessions, or integration authority.

**Architecture:** The Client API exposes exactly two pre-session operations: issue a Google human-auth challenge and verify one Google ID token against that challenge. The backend stores only a digest of the nonce, validates the provider assertion behind an adapter boundary, resolves `(provider, subject)` to one opaque Human Identity, and consumes the challenge atomically. PostgreSQL is the authority for challenge state and provider binding; Google credentials never become durable Attention Router authority.

**Tech Stack:** Python 3.11+, FastAPI 0.112.2, Pydantic, SQLAlchemy 2.0.32, Alembic 1.13.2, PostgreSQL 16 test harness, OpenAPI 3.1 JSON, pytest 8.3.2, Ruff 0.6.3, `google-auth==2.58.0`.

**Spec:** `escossio/andy-android@6d1e34638377eb6a304d7efba27f2a6b4571b341:docs/superpowers/specs/2026-09-12-human-identity-permissions-v1-design.md`

## Global Constraints

- Attention Router is the authoritative boundary for Human Identity validation.
- The authoritative provider identity key is `(provider="google", subject=<Google sub>)`; email is not an identity key.
- Google ID tokens are transient and must never be persisted or logged.
- The plaintext challenge nonce is returned to the client once and must never be persisted or logged; PostgreSQL stores only a SHA-256 digest.
- Human-auth challenges are unpredictable, short-lived, single-use, and fail closed on replay.
- Default challenge TTL is exactly `300` seconds.
- A consumed challenge is never treated as idempotently reusable. If a successful HTTP response is lost, the client starts a new challenge.
- Provider/network infrastructure failure does not consume an otherwise pending challenge; deterministic credential rejection or nonce mismatch does consume it.
- This plan creates no tenant membership, active tenant, server device enrollment, device-to-human binding, access token, refresh token, final application session, integration credential, WhatsApp state, or capability authorization.
- The existing integration contract under `contracts/integration/v1` is not modified.
- The existing local Android Device Identity is unrelated to this backend frontier and is not represented in the Human Identity HTTP contract.
- CI uses synthetic provider verifier results; no personal Google account or live Google login is required.
- HTTP response schemas expose only bounded status, opaque Human Identity reference, challenge reference, nonce, and expiry data defined by the canonical contract.
- The 2026-09-12 Human Identity spec is authority for the overlapping human-login scope. The older `docs/superpowers/plans/2026-09-11-client-api-authority-bootstrap-v1.md` must not be executed for Human Identity semantics that conflict with this plan.

---

## File Structure

### Canonical contract

- Create `contracts/client/v1/client-api.openapi.json` — canonical Client API V1 wire contract for the Human Identity slice only.
- Create `tests/test_client_human_identity_contract.py` — structural and negative-scope assertions for the OpenAPI artifact.

### Domain and configuration

- Create `attention_router/core/human_identity.py` — provider-neutral immutable Human Identity and auth-state records plus typed domain exceptions.
- Modify `attention_router/config.py` — feature flag, `300` second challenge TTL, and Google audience configuration with fail-closed validation.
- Modify `.env.example` — document the three new environment variables with the feature disabled by default.
- Modify `tests/test_platform_config.py` — configuration acceptance and failure cases.

### Persistence

- Create `attention_router/infrastructure/human_identity_models.py` — SQLAlchemy rows for Human Identity, provider binding, and auth transaction.
- Create `attention_router/infrastructure/human_identity_repository.py` — focused persistence operations, row locking, and race-safe provider binding resolution.
- Create `alembic/versions/0038_human_identity_v1.py` — PostgreSQL/SQLite-compatible schema migration from current `0037_integration_admission_v0` head.
- Modify `alembic/env.py` — import Human Identity models so metadata is complete.
- Create `tests/integration/test_postgres_human_identity_migration.py` — migration/constraint proof.
- Create `tests/integration/test_postgres_human_identity_repository.py` — concurrency and uniqueness proof.

### Provider adapter

- Modify `pyproject.toml` — add `google-auth==2.58.0`.
- Create `attention_router/integrations/google_identity.py` — Google ID-token verifier adapter that returns verified claims without granting Attention Router authority.
- Create `tests/test_google_identity.py` — deterministic adapter tests with Google verification calls replaced by test doubles.

### Application and HTTP

- Create `attention_router/application/human_identity.py` — challenge issuance and verification orchestration.
- Create `tests/test_human_identity.py` — application-state and fail-closed tests.
- Create `attention_router/api/v1/human_identity.py` — focused FastAPI router and stable error mapping.
- Modify `attention_router/web/app.py` — include the router; do not add Human Identity endpoint bodies to the existing large app module.
- Create `tests/test_human_identity_api.py` — HTTP contract behavior with a fake verifier.
- Create `tests/integration/test_postgres_human_identity.py` — end-to-end backend proof against PostgreSQL, including concurrency and no-authority-side-effect assertions.

---

### Task 1: Freeze the Human Identity wire contract and conflicting-plan authority

**Files:**
- Create: `contracts/client/v1/client-api.openapi.json`
- Create: `tests/test_client_human_identity_contract.py`
- Modify: `docs/superpowers/plans/2026-09-11-client-api-authority-bootstrap-v1.md`

**Interfaces:**
- Consumes: approved Human Identity V1 spec.
- Produces: `POST /api/v1/auth/google/challenges`, `POST /api/v1/auth/google/challenges/{challenge_id}/verify`, and stable component schemas used by the HTTP implementation.

- [ ] **Step 1: Mark the old bootstrap plan as non-authoritative for the overlapping Human Identity slice**

Add this block immediately below its title/header:

```markdown
> **Human Identity scope superseded:** Human-login semantics in this 2026-09-11 plan are superseded by `2026-09-13-human-identity-v1-backend.md`, which implements the approved 2026-09-12 Human Identity + Device Permissions V1 design. Do not implement Google Human Identity from this older plan. Tenant, device-enrollment, and session work remains outside the Human Identity V1 frontier until separately authorized.
```

- [ ] **Step 2: Write the failing contract tests**

Create `tests/test_client_human_identity_contract.py`:

```python
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts/client/v1/client-api.openapi.json"


def load_contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_human_identity_contract_has_only_approved_auth_paths():
    document = load_contract()
    assert document["openapi"] == "3.1.0"
    assert set(document["paths"]) == {
        "/api/v1/auth/google/challenges",
        "/api/v1/auth/google/challenges/{challenge_id}/verify",
    }


def test_pre_session_human_auth_paths_require_no_bearer_session():
    document = load_contract()
    for path in document["paths"].values():
        assert path["post"]["security"] == []


def test_human_identity_contract_excludes_forbidden_authority_fields():
    raw = CONTRACT.read_text(encoding="utf-8")
    forbidden = {
        "tenant_id",
        "device_id",
        "session_id",
        "access_token",
        "refresh_token",
        "membership",
        "integration_credential",
    }
    for field in forbidden:
        assert f'"{field}"' not in raw


def test_verify_request_contains_only_id_token():
    schemas = load_contract()["components"]["schemas"]
    request = schemas["GoogleHumanIdentityVerifyRequest"]
    assert request["required"] == ["id_token"]
    assert set(request["properties"]) == {"id_token"}
    assert request["additionalProperties"] is False


def test_validated_response_exposes_opaque_identity_not_google_subject():
    schemas = load_contract()["components"]["schemas"]
    response = schemas["HumanIdentityValidatedResponse"]
    assert response["required"] == ["status", "human_identity_id"]
    assert set(response["properties"]) == {"status", "human_identity_id"}
    assert response["properties"]["status"]["const"] == "HUMAN_IDENTITY_VALIDATED"
```

- [ ] **Step 3: Run the focused test and verify it fails because the contract does not exist**

Run:

```bash
python -m pytest -q tests/test_client_human_identity_contract.py
```

Expected: FAIL loading `contracts/client/v1/client-api.openapi.json`.

- [ ] **Step 4: Create the minimal OpenAPI 3.1 document**

The document must define these exact schemas and semantics:

```json
{
  "openapi": "3.1.0",
  "info": {"title": "Attention Router Client API", "version": "1"},
  "servers": [],
  "paths": {
    "/api/v1/auth/google/challenges": {
      "post": {
        "operationId": "createGoogleHumanAuthChallenge",
        "security": [],
        "responses": {
          "201": {
            "description": "Challenge issued",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/HumanAuthChallengeResponse"}}}
          },
          "503": {
            "description": "Human Identity feature unavailable",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/HumanAuthErrorResponse"}}}
          }
        }
      }
    },
    "/api/v1/auth/google/challenges/{challenge_id}/verify": {
      "post": {
        "operationId": "verifyGoogleHumanIdentity",
        "security": [],
        "parameters": [
          {"name": "challenge_id", "in": "path", "required": true, "schema": {"type": "string", "pattern": "^hac_[A-Za-z0-9_-]{20,}$"}}
        ],
        "requestBody": {
          "required": true,
          "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GoogleHumanIdentityVerifyRequest"}}}
        },
        "responses": {
          "200": {"description": "Human Identity validated", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/HumanIdentityValidatedResponse"}}}},
          "401": {"description": "Credential rejected", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/HumanAuthErrorResponse"}}}},
          "404": {"description": "Challenge not found", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/HumanAuthErrorResponse"}}}},
          "409": {"description": "Challenge already consumed", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/HumanAuthErrorResponse"}}}},
          "410": {"description": "Challenge expired", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/HumanAuthErrorResponse"}}}},
          "503": {"description": "Provider or feature unavailable", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/HumanAuthErrorResponse"}}}}
        }
      }
    }
  },
  "components": {
    "schemas": {
      "HumanAuthChallengeResponse": {
        "type": "object",
        "additionalProperties": false,
        "required": ["challenge_id", "nonce", "expires_at"],
        "properties": {
          "challenge_id": {"type": "string", "pattern": "^hac_[A-Za-z0-9_-]{20,}$"},
          "nonce": {"type": "string", "minLength": 32, "maxLength": 256},
          "expires_at": {"type": "string", "format": "date-time"}
        }
      },
      "GoogleHumanIdentityVerifyRequest": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id_token"],
        "properties": {"id_token": {"type": "string", "minLength": 32, "maxLength": 8192}}
      },
      "HumanIdentityValidatedResponse": {
        "type": "object",
        "additionalProperties": false,
        "required": ["status", "human_identity_id"],
        "properties": {
          "status": {"const": "HUMAN_IDENTITY_VALIDATED"},
          "human_identity_id": {"type": "string", "pattern": "^hid_[A-Za-z0-9_-]{20,}$"}
        }
      },
      "HumanAuthErrorResponse": {
        "type": "object",
        "additionalProperties": false,
        "required": ["code"],
        "properties": {
          "code": {
            "type": "string",
            "enum": [
              "HUMAN_AUTH_DISABLED",
              "HUMAN_AUTH_CHALLENGE_NOT_FOUND",
              "HUMAN_AUTH_CHALLENGE_EXPIRED",
              "HUMAN_AUTH_CHALLENGE_CONSUMED",
              "HUMAN_AUTH_CREDENTIAL_REJECTED",
              "HUMAN_AUTH_NONCE_MISMATCH",
              "HUMAN_AUTH_PROVIDER_UNAVAILABLE"
            ]
          }
        }
      }
    }
  }
}
```

- [ ] **Step 5: Run contract tests**

Run:

```bash
python -m pytest -q tests/test_client_human_identity_contract.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add contracts/client/v1/client-api.openapi.json tests/test_client_human_identity_contract.py docs/superpowers/plans/2026-09-11-client-api-authority-bootstrap-v1.md
git commit -m "feat: freeze Human Identity V1 client contract"
```

---

### Task 2: Add pure Human Identity domain types and fail-closed configuration

**Files:**
- Create: `attention_router/core/human_identity.py`
- Modify: `attention_router/config.py`
- Modify: `.env.example`
- Modify: `tests/test_platform_config.py`
- Create: `tests/test_human_identity_core.py`

**Interfaces:**
- Produces: `HumanAuthState`, `VerifiedProviderIdentity`, `IssuedHumanAuthChallenge`, `HumanIdentityValidated`, and stable typed exceptions consumed by repository, service, and API tasks.

- [ ] **Step 1: Write failing core tests**

Create `tests/test_human_identity_core.py`:

```python
from datetime import UTC, datetime

from attention_router.core.human_identity import (
    HumanAuthState,
    HumanIdentityValidated,
    VerifiedProviderIdentity,
)


def test_human_auth_state_is_bounded():
    assert {state.value for state in HumanAuthState} == {"PENDING", "VERIFIED", "REJECTED"}


def test_verified_provider_identity_is_provider_neutral():
    verified = VerifiedProviderIdentity(
        provider="google",
        subject="google-sub-123",
        nonce="nonce-value",
        issued_at=datetime(2026, 9, 13, tzinfo=UTC),
        expires_at=datetime(2026, 9, 13, 0, 5, tzinfo=UTC),
    )
    assert verified.provider == "google"
    assert verified.subject == "google-sub-123"


def test_validated_result_exposes_only_opaque_identity_reference():
    result = HumanIdentityValidated(human_identity_id="hid_exampleopaqueidentity123")
    assert result.status == "HUMAN_IDENTITY_VALIDATED"
    assert not hasattr(result, "subject")
    assert not hasattr(result, "email")
```

- [ ] **Step 2: Run core tests and verify failure**

```bash
python -m pytest -q tests/test_human_identity_core.py
```

Expected: FAIL because `attention_router.core.human_identity` does not exist.

- [ ] **Step 3: Implement the pure types and exceptions**

Create immutable dataclasses/enums in `attention_router/core/human_identity.py`. The module must define these public names:

```python
class HumanAuthState(str, Enum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"

@dataclass(frozen=True)
class VerifiedProviderIdentity:
    provider: str
    subject: str
    nonce: str
    issued_at: datetime
    expires_at: datetime

@dataclass(frozen=True)
class IssuedHumanAuthChallenge:
    challenge_id: str
    nonce: str
    expires_at: datetime

@dataclass(frozen=True)
class HumanIdentityValidated:
    human_identity_id: str
    status: str = "HUMAN_IDENTITY_VALIDATED"
```

Also define exception classes with exact `.code` values matching the OpenAPI enum: `HumanAuthDisabled`, `HumanAuthChallengeNotFound`, `HumanAuthChallengeExpired`, `HumanAuthChallengeConsumed`, `HumanAuthCredentialRejected`, `HumanAuthNonceMismatch`, and `HumanAuthProviderUnavailable`.

- [ ] **Step 4: Add failing configuration tests**

Extend `tests/test_platform_config.py` with direct `Settings(...)` cases that prove:

```python
assert Settings(
    admin_auth_enabled=False,
    internal_ingress_hmac_secret="x" * 32,
).human_auth_challenge_ttl_seconds == 300
```

and that `Settings(human_identity_enabled=True, google_identity_audience=None, ...)` raises `ValueError` containing `GOOGLE_IDENTITY_AUDIENCE`.

- [ ] **Step 5: Add exact settings and environment examples**

Add to `Settings`:

```python
human_identity_enabled: bool = False
human_auth_challenge_ttl_seconds: int = 300
google_identity_audience: str | None = None
```

Validation rules:

```python
if not 60 <= self.human_auth_challenge_ttl_seconds <= 900:
    raise ValueError("HUMAN_AUTH_CHALLENGE_TTL_SECONDS must be between 60 and 900")
if self.human_identity_enabled and not self.google_identity_audience:
    raise ValueError("GOOGLE_IDENTITY_AUDIENCE is required when HUMAN_IDENTITY_ENABLED=true")
```

Add to `.env.example`:

```dotenv
HUMAN_IDENTITY_ENABLED=false
HUMAN_AUTH_CHALLENGE_TTL_SECONDS=300
GOOGLE_IDENTITY_AUDIENCE=
```

- [ ] **Step 6: Run focused tests**

```bash
python -m pytest -q tests/test_human_identity_core.py tests/test_platform_config.py
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add attention_router/core/human_identity.py attention_router/config.py .env.example tests/test_human_identity_core.py tests/test_platform_config.py
git commit -m "feat: add Human Identity core and configuration"
```

---

### Task 3: Add Human Identity persistence and migration

**Files:**
- Create: `attention_router/infrastructure/human_identity_models.py`
- Create: `alembic/versions/0038_human_identity_v1.py`
- Modify: `alembic/env.py`
- Create: `tests/integration/test_postgres_human_identity_migration.py`

**Interfaces:**
- Produces tables `human_identities`, `external_identity_bindings`, and `human_auth_transactions`.

- [ ] **Step 1: Write the failing PostgreSQL migration test**

Create a `@pytest.mark.postgres` test that inspects the migrated database and asserts exact columns/constraints:

```python
expected_tables = {
    "human_identities",
    "external_identity_bindings",
    "human_auth_transactions",
}
assert expected_tables <= set(inspector.get_table_names())
```

Assert `external_identity_bindings` has a unique constraint over `provider, subject`; `human_auth_transactions` has no `tenant_id`, `device_id`, `session_id`, or token column; and auth state is constrained to `PENDING`, `VERIFIED`, `REJECTED`.

- [ ] **Step 2: Run the focused PostgreSQL selector and verify failure**

```bash
POSTGRES_TEST_SELECTOR='tests/integration/test_postgres_human_identity_migration.py -m postgres' ./scripts/postgres_test_harness.sh
```

Expected: FAIL because migration `0038_human_identity_v1` and the tables do not exist.

- [ ] **Step 3: Create focused SQLAlchemy models**

`HumanIdentityRow`:

- table `human_identities`
- `id: String(64)` primary key
- `created_at: DateTime(timezone=True)` non-null

`ExternalIdentityBindingRow`:

- table `external_identity_bindings`
- `id: String(64)` primary key
- `provider: String(32)` non-null
- `subject: String(255)` non-null
- `human_identity_id: String(64)` FK `human_identities.id`, non-null
- `created_at`, `last_verified_at`: timezone-aware, non-null
- unique constraint `(provider, subject)` named `uq_external_identity_provider_subject`

`HumanAuthTransactionRow`:

- table `human_auth_transactions`
- `id: String(64)` primary key
- `provider: String(32)` non-null
- `nonce_digest: String(64)` non-null and unique
- `state: String(16)` non-null
- `created_at`, `expires_at`: timezone-aware, non-null
- `consumed_at`: timezone-aware nullable
- `resolved_human_identity_id`: nullable FK `human_identities.id`
- check constraint named `ck_human_auth_transaction_state` for `PENDING`, `VERIFIED`, `REJECTED`

Do not add email, name, tenant, device, session, raw nonce, ID token, token digest, refresh credential, or provider access token columns.

- [ ] **Step 4: Create migration `0038_human_identity_v1.py`**

Set:

```python
revision = "0038_human_identity_v1"
down_revision = "0037_integration_admission_v0"
```

Create the three tables and named constraints exactly as modeled. Downgrade order must be `human_auth_transactions`, `external_identity_bindings`, `human_identities`.

- [ ] **Step 5: Register model metadata with Alembic**

Add:

```python
from attention_router.infrastructure import human_identity_models  # noqa: F401
```

to `alembic/env.py` next to the existing model imports.

- [ ] **Step 6: Run migration proof**

```bash
POSTGRES_TEST_SELECTOR='tests/integration/test_postgres_human_identity_migration.py -m postgres' ./scripts/postgres_test_harness.sh
```

Expected: PASS, and `alembic heads` reports one head at `0038_human_identity_v1`.

- [ ] **Step 7: Commit**

```bash
git add attention_router/infrastructure/human_identity_models.py alembic/versions/0038_human_identity_v1.py alembic/env.py tests/integration/test_postgres_human_identity_migration.py
git commit -m "feat: add Human Identity persistence schema"
```

---

### Task 4: Add race-safe repository operations

**Files:**
- Create: `attention_router/infrastructure/human_identity_repository.py`
- Create: `tests/integration/test_postgres_human_identity_repository.py`

**Interfaces:**
- Produces: `create_human_auth_transaction`, `get_human_auth_transaction`, `lock_human_auth_transaction`, `resolve_human_identity`, `mark_human_auth_verified`, and `mark_human_auth_rejected`.

- [ ] **Step 1: Write failing repository tests**

Cover at minimum:

```python
row = create_human_auth_transaction(
    session,
    transaction_id="hac_repositorychallenge123456789",
    provider="google",
    nonce_digest="a" * 64,
    now=now,
    expires_at=expires_at,
)
assert row.state == "PENDING"
```

and:

```python
first = resolve_human_identity(session, provider="google", subject="stable-sub", now=now)
second = resolve_human_identity(session, provider="google", subject="stable-sub", now=now)
assert first == second
```

Also assert two different subjects resolve to different `hid_...` values even if test metadata uses the same synthetic email outside the repository.

- [ ] **Step 2: Run repository tests and verify failure**

```bash
POSTGRES_TEST_SELECTOR='tests/integration/test_postgres_human_identity_repository.py -m postgres' ./scripts/postgres_test_harness.sh
```

Expected: FAIL because repository functions do not exist.

- [ ] **Step 3: Implement transaction access and row locking**

`lock_human_auth_transaction` must use:

```python
select(HumanAuthTransactionRow).where(
    HumanAuthTransactionRow.id == transaction_id
).with_for_update()
```

and return one row or `None`.

- [ ] **Step 4: Implement race-safe `(provider, subject)` resolution**

Algorithm:

1. query existing binding;
2. if present, update only `last_verified_at` and return its Human Identity ID;
3. otherwise create `hid_` + `secrets.token_urlsafe(24)` Human Identity and binding inside `session.begin_nested()`;
4. flush the savepoint;
5. on unique-key `IntegrityError`, allow the savepoint to roll back, re-query `(provider, subject)`, and return the winner's Human Identity ID;
6. never roll back the caller's outer transaction solely because another request won the provider-binding race.

- [ ] **Step 5: Implement state mutation helpers**

`mark_human_auth_verified` sets `state="VERIFIED"`, `consumed_at=now`, and `resolved_human_identity_id`.

`mark_human_auth_rejected` sets `state="REJECTED"`, `consumed_at=now`, and leaves `resolved_human_identity_id=None`.

Both helpers must reject mutation when row state is no longer `PENDING`.

- [ ] **Step 6: Add a concurrent first-login test**

Use two PostgreSQL sessions and a `threading.Barrier`. Give each thread a different pending challenge but the same synthetic `provider="google", subject="same-sub"`. Both calls to `resolve_human_identity` must finish with the same ID and only one `external_identity_bindings` row must exist for that pair.

- [ ] **Step 7: Run repository tests**

```bash
POSTGRES_TEST_SELECTOR='tests/integration/test_postgres_human_identity_repository.py -m postgres' ./scripts/postgres_test_harness.sh
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add attention_router/infrastructure/human_identity_repository.py tests/integration/test_postgres_human_identity_repository.py
git commit -m "feat: add Human Identity repository semantics"
```

---

### Task 5: Add the Google verifier adapter

**Files:**
- Modify: `pyproject.toml`
- Create: `attention_router/integrations/google_identity.py`
- Create: `tests/test_google_identity.py`

**Interfaces:**
- Consumes: Google ID token string and configured audience.
- Produces: `VerifiedProviderIdentity(provider="google", subject, nonce, issued_at, expires_at)` or typed `HumanAuthCredentialRejected` / `HumanAuthProviderUnavailable`.

- [ ] **Step 1: Write failing adapter tests**

Patch the library verification call; do not call Google over the network. Cover a valid claim set:

```python
claims = {
    "iss": "https://accounts.google.com",
    "aud": "server-client-id.example.apps.googleusercontent.com",
    "sub": "google-sub-123",
    "nonce": "nonce-value",
    "iat": 1789257600,
    "exp": 1789257900,
}
```

Assert the adapter returns provider `google`, subject `google-sub-123`, and nonce `nonce-value`.

Also test missing `sub`, missing `nonce`, wrong issuer, library `ValueError` -> credential rejected, and `google.auth.exceptions.TransportError` -> provider unavailable.

- [ ] **Step 2: Run adapter tests and verify failure**

```bash
python -m pytest -q tests/test_google_identity.py
```

Expected: FAIL because adapter/dependency do not exist.

- [ ] **Step 3: Pin Google Auth dependency**

Add exactly:

```toml
"google-auth==2.58.0",
```

to the main dependency list in `pyproject.toml`.

- [ ] **Step 4: Implement verifier adapter**

Use `google.oauth2.id_token.verify_oauth2_token` with `google.auth.transport.requests.Request()` and the configured audience. After library verification, explicitly require:

```python
claims["iss"] in {"accounts.google.com", "https://accounts.google.com"}
claims["sub"]
claims["nonce"]
claims["iat"]
claims["exp"]
```

Convert integer timestamps to timezone-aware UTC datetimes. Never log the token, nonce, subject, or full claim dictionary.

- [ ] **Step 5: Run tests**

```bash
python -m pytest -q tests/test_google_identity.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml attention_router/integrations/google_identity.py tests/test_google_identity.py
git commit -m "feat: add Google Human Identity verifier"
```

---

### Task 6: Implement challenge issuance and Human Identity verification service

**Files:**
- Create: `attention_router/application/human_identity.py`
- Create: `tests/test_human_identity.py`

**Interfaces:**
- Consumes repository functions, settings, and a verifier implementing `verify(id_token: str) -> VerifiedProviderIdentity`.
- Produces `issue_google_challenge(session, now=None) -> IssuedHumanAuthChallenge` and `verify_google_challenge(session, challenge_id, id_token, now=None) -> HumanIdentityValidated` through a `HumanIdentityService` object.

- [ ] **Step 1: Write failing service tests for challenge issuance**

Use an isolated SQLAlchemy test session and assert:

```python
issued = service.issue_google_challenge(session, now=now)
assert issued.challenge_id.startswith("hac_")
assert len(issued.nonce) >= 32
assert issued.expires_at == now + timedelta(seconds=300)
```

Query the stored row and assert `row.nonce_digest == sha256(issued.nonce.encode()).hexdigest()` and `issued.nonce not in vars(row).values()`.

- [ ] **Step 2: Write failing service tests for verification state**

Cover:

- missing challenge -> `HumanAuthChallengeNotFound`;
- expired challenge -> `HumanAuthChallengeExpired`;
- previously consumed challenge -> `HumanAuthChallengeConsumed`;
- verifier rejects credential -> mark pending challenge `REJECTED` then raise `HumanAuthCredentialRejected`;
- verified token whose nonce digest differs -> mark `REJECTED` then raise `HumanAuthNonceMismatch`;
- provider unavailable -> preserve `PENDING` and raise `HumanAuthProviderUnavailable`;
- valid token -> resolve `google:sub`, mark challenge `VERIFIED`, return opaque `hid_...`;
- second call after successful validation -> `HumanAuthChallengeConsumed`;
- same Google subject through a fresh second challenge -> same Human Identity ID.

- [ ] **Step 3: Run service tests and verify failure**

```bash
python -m pytest -q tests/test_human_identity.py
```

Expected: FAIL because `HumanIdentityService` does not exist.

- [ ] **Step 4: Implement secure challenge generation**

Use:

```python
challenge_id = f"hac_{secrets.token_urlsafe(24)}"
nonce = secrets.token_urlsafe(32)
nonce_digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
```

Persist only `nonce_digest`.

- [ ] **Step 5: Implement verification without holding a database lock during provider I/O**

The exact order is:

1. read challenge without lock and reject not-found / non-pending / expired;
2. call the provider verifier outside a row lock;
3. if provider is unavailable, return the typed infrastructure failure without consuming the challenge;
4. if provider deterministically rejects the credential, lock the challenge, re-check state/expiry, mark `REJECTED`, then raise credential rejection;
5. for a verified provider result, lock the challenge, re-check state/expiry, compare `sha256(verified.nonce)` with persisted `nonce_digest` using `hmac.compare_digest`;
6. on nonce mismatch mark `REJECTED` and raise mismatch;
7. resolve `(provider, subject)` race-safely;
8. mark challenge `VERIFIED` and bind the opaque Human Identity result;
9. return `HumanIdentityValidated`.

This ordering intentionally allows duplicate provider verification work under concurrency but permits only one database-side challenge consumption.

- [ ] **Step 6: Run service tests**

```bash
python -m pytest -q tests/test_human_identity.py
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add attention_router/application/human_identity.py tests/test_human_identity.py
git commit -m "feat: implement Human Identity challenge service"
```

---

### Task 7: Expose the focused FastAPI Human Identity router

**Files:**
- Create: `attention_router/api/v1/human_identity.py`
- Modify: `attention_router/web/app.py`
- Create: `tests/test_human_identity_api.py`

**Interfaces:**
- Produces runtime HTTP behavior matching `contracts/client/v1/client-api.openapi.json` exactly for the two Human Identity operations.

- [ ] **Step 1: Write failing isolated router tests**

Construct a small test FastAPI app from `build_human_identity_router(get_session=..., service=...)` using a fake service. Assert:

```python
response = client.post("/api/v1/auth/google/challenges")
assert response.status_code == 201
assert set(response.json()) == {"challenge_id", "nonce", "expires_at"}
```

and:

```python
response = client.post(
    "/api/v1/auth/google/challenges/hac_examplechallenge123456789/verify",
    json={"id_token": "synthetic-token-value-that-is-not-logged"},
)
assert response.status_code == 200
assert response.json() == {
    "status": "HUMAN_IDENTITY_VALIDATED",
    "human_identity_id": "hid_exampleopaqueidentity123",
}
```

Test error mappings exactly:

- not found -> `404` + `HUMAN_AUTH_CHALLENGE_NOT_FOUND`
- expired -> `410` + `HUMAN_AUTH_CHALLENGE_EXPIRED`
- consumed -> `409` + `HUMAN_AUTH_CHALLENGE_CONSUMED`
- rejected -> `401` + `HUMAN_AUTH_CREDENTIAL_REJECTED`
- nonce mismatch -> `401` + `HUMAN_AUTH_NONCE_MISMATCH`
- provider unavailable -> `503` + `HUMAN_AUTH_PROVIDER_UNAVAILABLE`
- feature disabled -> `503` + `HUMAN_AUTH_DISABLED`

Also prove extra JSON properties are rejected by Pydantic and that the API error body contains only `code`.

- [ ] **Step 2: Run API tests and verify failure**

```bash
python -m pytest -q tests/test_human_identity_api.py
```

Expected: FAIL because router does not exist.

- [ ] **Step 3: Implement strict request/response models and router builder**

Use Pydantic `ConfigDict(extra="forbid")` for request models. Keep the router module focused; do not add route bodies directly to `attention_router/web/app.py`.

Required builder signature:

```python
def build_human_identity_router(*, get_session, service: HumanIdentityService) -> APIRouter:
    ...
```

- [ ] **Step 4: Wire the default service into the application**

In `attention_router/web/app.py`, instantiate the Google verifier only from configured settings and include the returned router. When `HUMAN_IDENTITY_ENABLED=false`, the routes remain present but fail closed with `HUMAN_AUTH_DISABLED`; missing Google audience must never silently accept arbitrary audience.

- [ ] **Step 5: Run API and existing admin-auth tests**

```bash
python -m pytest -q tests/test_human_identity_api.py tests/test_admin_auth.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add attention_router/api/v1/human_identity.py attention_router/web/app.py tests/test_human_identity_api.py
git commit -m "feat: expose Human Identity V1 API"
```

---

### Task 8: Prove PostgreSQL concurrency, privacy, and absence of authority side effects

**Files:**
- Create: `tests/integration/test_postgres_human_identity.py`

**Interfaces:**
- Validates the complete backend frontier from service + repository against real PostgreSQL with a deterministic fake provider verifier.

- [ ] **Step 1: Add same-challenge concurrency test**

Create one pending challenge, then use two sessions and two threads to submit the same valid synthetic provider result concurrently. Assert exactly one call returns `HUMAN_IDENTITY_VALIDATED` and the other raises `HumanAuthChallengeConsumed`; the transaction row finishes `VERIFIED` once.

- [ ] **Step 2: Add concurrent first-login race test**

Create two independent challenges and verify both concurrently with the same synthetic Google subject. Assert both successful results contain the same `human_identity_id`, exactly one binding exists for `(google, subject)`, and no orphan duplicate Human Identity remains after the losing savepoint path.

- [ ] **Step 3: Add stable-subject and changed-email semantics test**

The fake verifier may expose test-only ancillary email metadata, but the application result and persistence must ignore it. Verify the same `sub` with two different synthetic emails through two fresh challenges and assert the same Human Identity ID. Then verify two different `sub` values with the same synthetic email and assert different Human Identity IDs.

- [ ] **Step 4: Add no-secret-persistence proof**

Use a distinctive nonce and ID-token string. After completion, inspect every string column in `human_identities`, `external_identity_bindings`, and `human_auth_transactions`; assert neither plaintext nonce nor token appears in any stored value. Assert the persisted nonce value is exactly a 64-character SHA-256 hex digest.

- [ ] **Step 5: Add no tenant/device/session side-effect proof**

Before Human Identity verification, use SQLAlchemy inspection to collect row counts for every existing table whose lower-case name contains `tenant`, `device`, or `session`. Repeat after successful verification and assert the snapshots are identical. This makes the negative boundary robust without hard-coding current platform table names.

- [ ] **Step 6: Run focused PostgreSQL proof**

```bash
POSTGRES_TEST_SELECTOR='tests/integration/test_postgres_human_identity.py -m postgres' ./scripts/postgres_test_harness.sh
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/integration/test_postgres_human_identity.py
git commit -m "test: prove Human Identity V1 PostgreSQL boundary"
```

---

### Task 9: Run the complete release gate for the backend frontier

**Files:**
- No new functional files unless a failing existing test reveals a regression caused by this frontier. Any fix must remain inside the Human Identity scope.

**Interfaces:**
- Produces one exact candidate SHA with contract, unit, lint, migration, PostgreSQL, and regression evidence.

- [ ] **Step 1: Run Ruff across the repository**

```bash
ruff check .
```

Expected: PASS.

- [ ] **Step 2: Run all non-PostgreSQL tests**

```bash
python -m pytest -q
```

Expected: PASS.

- [ ] **Step 3: Run the full PostgreSQL harness**

```bash
./scripts/postgres_test_harness.sh
```

Expected: Alembic upgrade to head succeeds, `alembic heads` reports one head, Ruff passes inside the runner, and all `-m postgres` tests pass.

- [ ] **Step 4: Run the public secret scan**

```bash
./scripts/public_secret_scan.sh
```

Expected: PASS with no Google ID token, OAuth secret, real provider identity, signing secret, or credential committed.

- [ ] **Step 5: Re-run focused contract and Human Identity suites after the full gate**

```bash
python -m pytest -q \
  tests/test_client_human_identity_contract.py \
  tests/test_human_identity_core.py \
  tests/test_google_identity.py \
  tests/test_human_identity.py \
  tests/test_human_identity_api.py
```

Expected: PASS.

- [ ] **Step 6: Record exact candidate SHA and diff scope**

```bash
git status --short
git rev-parse HEAD
git diff --stat main...HEAD
git diff --name-only main...HEAD
```

Expected: clean working tree; changed files are limited to the contract, Human Identity core/config/persistence/provider/application/API/tests, the Alembic registration/migration, dependency/env example, and the explicit supersession note in the older plan.

- [ ] **Step 7: Do not deploy or merge automatically**

Stop with evidence. The backend candidate must be reviewed at the exact SHA before merge. Runtime activation remains disabled by default through `HUMAN_IDENTITY_ENABLED=false` until a separately controlled acceptance step supplies the real Google audience and the Android client is ready to exercise the contract.

---

## Self-Review Results

### Spec coverage

- Backend-issued short-lived challenge: Tasks 2, 3, 4, 6.
- Single-use/replay rejection: Tasks 3, 4, 6, 8.
- Google signature/issuer/audience/expiry verification boundary: Task 5.
- Nonce binding: Tasks 5 and 6.
- `google:sub` identity key and email exclusion: Tasks 3, 4, 6, 8.
- Opaque Human Identity result: Tasks 1, 2, 6, 7.
- No tenant/session/device enrollment side effects: Tasks 1 and 8.
- No Google ID token or nonce persistence/logging: Tasks 3, 5, 6, 8.
- CI without personal Google account: Tasks 5 through 9.
- Backend/Android implementation separation: this plan contains no Android code.

### Type consistency

The plan uses one stable vocabulary throughout: `HumanAuthState`, `VerifiedProviderIdentity`, `IssuedHumanAuthChallenge`, `HumanIdentityValidated`, `HumanIdentityService`, `HumanIdentityRow`, `ExternalIdentityBindingRow`, and `HumanAuthTransactionRow`.

### Scope result

This plan ends at a testable backend Human Identity V1 boundary. Android Credential Manager, stable Android signing, Android permission cards, and physical-phone acceptance belong to the next plan and must not be implemented from this document.
