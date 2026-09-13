# Human Identity Google Backend V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the minimal Attention Router Client API backend needed to issue a short-lived single-use Google sign-in challenge and validate a Google ID token into an opaque Human Identity, without creating tenant membership, device enrollment, or an application session.

**Architecture:** A new isolated Client API app exposes two pre-session endpoints. Challenge state and provider bindings are persisted transactionally in PostgreSQL; raw nonces and Google ID tokens are never persisted. Google token verification is behind an injectable adapter using `google-auth==2.58.0`, while the application service owns challenge consumption and `google:sub` identity resolution.

**Tech Stack:** Python 3.11+, FastAPI 0.112.2, Pydantic, SQLAlchemy 2.0.32, PostgreSQL, Alembic 1.13.2, `google-auth==2.58.0`, pytest 8.3.2, existing PostgreSQL test harness.

**Spec:** `https://github.com/escossio/andy-android/blob/6d1e34638377eb6a304d7efba27f2a6b4571b341/docs/superpowers/specs/2026-09-12-human-identity-permissions-v1-design.md`

## Global Constraints

- Base implementation work on a freshly fetched `origin/main`; at plan-writing time `main` was `a0b5f5b7a83466691ac147e73458a605a7b2ff87`, but execution must re-check before branching.
- The newer Human Identity + Device Permissions V1 spec is authoritative for this slice. The older unexecuted `2026-09-11-client-api-authority-bootstrap-v1.md` plan must not cause device enrollment, email challenge, tenant activation, or session issuance to leak into this work.
- Human Identity authority comes only from successful server-side Google validation plus successful single-use challenge validation.
- The canonical provider key is logically `google:sub`; store a SHA-256 digest of `sub` for lookup rather than the raw subject.
- Never use email as the provider identity key.
- Never persist a Google ID token or raw challenge nonce.
- Never log Google ID tokens, raw nonce values, provider subject values, email addresses, or full opaque Human Identity IDs.
- No tenant membership, active tenant, device enrollment/binding, final application session, refresh token, integration credential, WhatsApp, or provider execution is introduced.
- No deployment, live database migration, runtime restart, or production Google activation is part of this plan.
- All tests use synthetic identities/tokens except the verifier adapter’s library boundary; CI must not require a real Google account.
- Keep existing integration admission and transport contracts untouched.
- Challenge TTL defaults to exactly `300` seconds and must be positive.
- Provider is exactly `google` in V1.
- Validation failures are fail-closed and reveal only bounded error codes.

---

## File Structure

Create focused units instead of adding another large block to `attention_router/web/app.py` or `attention_router/infrastructure/repository.py`:

- Create `contracts/client/v1/human-identity-v1.openapi.json` — canonical wire contract for challenge + Google validation.
- Create `attention_router/core/human_identity.py` — provider-neutral Human Identity value types, result/error vocabulary, verifier protocol.
- Create `attention_router/integrations/google_identity.py` — `google-auth` adapter that verifies a Google ID token and returns bounded claims.
- Modify `attention_router/config.py` — add Google audience and challenge TTL configuration without making existing runtime startup depend on Google.
- Modify `attention_router/infrastructure/models.py` — ORM rows for Human Identity, provider binding, and auth challenge.
- Create `alembic/versions/0038_human_identity_google_v1.py` — schema migration after 0037.
- Create `attention_router/infrastructure/human_identity_repository.py` — challenge persistence/locking and provider-identity resolution.
- Create `attention_router/application/human_auth.py` — issue/validate use cases, nonce hashing, post-lock expiry/consumption checks.
- Create `attention_router/web/client_api_app.py` — isolated FastAPI Client API factory and exactly two public pre-session routes.
- Modify `pyproject.toml` — pin `google-auth==2.58.0`.
- Create `tests/test_human_identity_contract.py`
- Create `tests/test_google_identity_verifier.py`
- Create `tests/test_human_auth_service.py`
- Create `tests/test_client_api_human_auth.py`
- Create `tests/integration/test_postgres_human_identity.py`
- Create `docs/human-identity-google-backend-v1.md`

Do not add these routes to the admin app. Do not change ingress listeners.

---

### Task 1: Freeze the Human Identity V1 wire contract

**Files:**
- Create: `contracts/client/v1/human-identity-v1.openapi.json`
- Create: `tests/test_human_identity_contract.py`

**Interfaces:**
- Produces HTTP operations:
  - `POST /api/v1/human-auth/challenges`
  - `POST /api/v1/human-auth/google/validate`
- Produces schemas:
  - `HumanAuthChallengeResponse`
  - `GoogleHumanIdentityValidationRequest`
  - `HumanIdentityValidatedResponse`
  - `ClientError`

- [ ] **Step 1: Write the failing contract test**

Create `tests/test_human_identity_contract.py`:

```python
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "contracts/client/v1/human-identity-v1.openapi.json"


def load_contract() -> dict:
    return json.loads(OPENAPI.read_text(encoding="utf-8"))


def test_human_identity_contract_has_only_v1_pre_session_operations():
    document = load_contract()
    assert document["openapi"] == "3.1.0"
    assert document["info"]["version"] == "1"
    assert set(document["paths"]) == {
        "/api/v1/human-auth/challenges",
        "/api/v1/human-auth/google/validate",
    }
    for path_item in document["paths"].values():
        assert path_item["post"]["security"] == []


def test_validate_response_is_human_only_not_tenant_device_or_session():
    schemas = load_contract()["components"]["schemas"]
    payload = schemas["HumanIdentityValidatedResponse"]
    assert payload["required"] == ["status", "human_identity_id"]
    assert payload["properties"]["status"] == {"const": "HUMAN_IDENTITY_VALIDATED"}
    forbidden = {"tenant_id", "device_id", "session_id", "access_token", "refresh_token"}
    assert forbidden.isdisjoint(payload["properties"])


def test_request_never_accepts_email_tenant_or_device_authority():
    schema = load_contract()["components"]["schemas"]["GoogleHumanIdentityValidationRequest"]
    assert schema["required"] == ["challenge_id", "id_token"]
    assert set(schema["properties"]) == {"challenge_id", "id_token"}


def test_contract_uses_bounded_error_codes():
    schemas = load_contract()["components"]["schemas"]
    assert schemas["ClientError"]["properties"]["error_code"]["enum"] == [
        "INVALID_REQUEST",
        "GOOGLE_CREDENTIAL_INVALID",
        "CHALLENGE_CONSUMED",
        "CHALLENGE_EXPIRED",
        "HUMAN_AUTH_UNAVAILABLE",
    ]
```

- [ ] **Step 2: Run the focused test and verify RED**

```bash
python -m pytest -q tests/test_human_identity_contract.py
```

Expected: FAIL because `contracts/client/v1/human-identity-v1.openapi.json` does not exist.

- [ ] **Step 3: Create the minimal OpenAPI contract**

Create `contracts/client/v1/human-identity-v1.openapi.json` with these exact semantics:

```json
{
  "openapi": "3.1.0",
  "info": {"title": "Attention Router Human Identity API", "version": "1"},
  "servers": [],
  "paths": {
    "/api/v1/human-auth/challenges": {
      "post": {
        "operationId": "issueHumanAuthChallenge",
        "security": [],
        "responses": {
          "201": {"description": "Single-use Google human-auth challenge", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/HumanAuthChallengeResponse"}}}},
          "503": {"description": "Human authentication unavailable", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ClientError"}}}}
        }
      }
    },
    "/api/v1/human-auth/google/validate": {
      "post": {
        "operationId": "validateGoogleHumanIdentity",
        "security": [],
        "requestBody": {"required": true, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GoogleHumanIdentityValidationRequest"}}}},
        "responses": {
          "200": {"description": "Google credential and challenge validated", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/HumanIdentityValidatedResponse"}}}},
          "400": {"description": "Malformed request", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ClientError"}}}},
          "401": {"description": "Google credential, issuer, audience, signature, expiry, or nonce invalid", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ClientError"}}}},
          "409": {"description": "Challenge already consumed", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ClientError"}}}},
          "410": {"description": "Challenge expired", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ClientError"}}}},
          "503": {"description": "Human authentication unavailable", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ClientError"}}}}
        }
      }
    }
  },
  "components": {
    "schemas": {
      "HumanAuthChallengeResponse": {
        "type": "object", "additionalProperties": false,
        "required": ["challenge_id", "nonce", "expires_at"],
        "properties": {
          "challenge_id": {"type": "string", "minLength": 1, "maxLength": 64},
          "nonce": {"type": "string", "minLength": 32, "maxLength": 256},
          "expires_at": {"type": "string", "format": "date-time"}
        }
      },
      "GoogleHumanIdentityValidationRequest": {
        "type": "object", "additionalProperties": false,
        "required": ["challenge_id", "id_token"],
        "properties": {
          "challenge_id": {"type": "string", "minLength": 1, "maxLength": 64},
          "id_token": {"type": "string", "minLength": 20, "maxLength": 16384}
        }
      },
      "HumanIdentityValidatedResponse": {
        "type": "object", "additionalProperties": false,
        "required": ["status", "human_identity_id"],
        "properties": {
          "status": {"const": "HUMAN_IDENTITY_VALIDATED"},
          "human_identity_id": {"type": "string", "minLength": 1, "maxLength": 64}
        }
      },
      "ClientError": {
        "type": "object", "additionalProperties": false,
        "required": ["error_code", "request_id"],
        "properties": {
          "error_code": {"type": "string", "enum": ["INVALID_REQUEST", "GOOGLE_CREDENTIAL_INVALID", "CHALLENGE_CONSUMED", "CHALLENGE_EXPIRED", "HUMAN_AUTH_UNAVAILABLE"]},
          "request_id": {"type": "string", "minLength": 1, "maxLength": 64}
        }
      }
    }
  }
}
```

- [ ] **Step 4: Run the contract test and verify GREEN**

```bash
python -m pytest -q tests/test_human_identity_contract.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add contracts/client/v1/human-identity-v1.openapi.json tests/test_human_identity_contract.py
git commit -m "feat: define human identity client contract"
```

---

### Task 2: Add platform-neutral Human Identity types and Google verifier boundary

**Files:**
- Create: `attention_router/core/human_identity.py`
- Create: `attention_router/integrations/google_identity.py`
- Modify: `pyproject.toml`
- Create: `tests/test_google_identity_verifier.py`

**Interfaces:**
- Produces `GoogleTokenClaims(subject: str, nonce: str)`.
- Produces `GoogleIdentityVerifier.verify(id_token: str, audience: str) -> GoogleTokenClaims`.
- Produces `GoogleAuthLibraryVerifier.verify(...)` and `HumanAuthErrorCode`.

- [ ] **Step 1: Write verifier boundary tests before adding the dependency**

Create tests proving the adapter returns only `sub` and `nonce`, rejects missing/malformed claims, and never echoes the raw token when the Google library raises. Inject both `verify_oauth2_token` and `request_factory` so tests have no network dependency.

- [ ] **Step 2: Run tests and verify RED**

```bash
python -m pytest -q tests/test_google_identity_verifier.py
```

Expected: FAIL because the new modules do not exist.

- [ ] **Step 3: Add the pinned Google verification dependency**

In `pyproject.toml`, add exactly:

```toml
  "google-auth==2.58.0",
```

- [ ] **Step 4: Implement the core types**

Create `attention_router/core/human_identity.py`:

```python
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

class HumanAuthErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    GOOGLE_CREDENTIAL_INVALID = "GOOGLE_CREDENTIAL_INVALID"
    CHALLENGE_CONSUMED = "CHALLENGE_CONSUMED"
    CHALLENGE_EXPIRED = "CHALLENGE_EXPIRED"
    HUMAN_AUTH_UNAVAILABLE = "HUMAN_AUTH_UNAVAILABLE"

@dataclass(frozen=True)
class GoogleTokenClaims:
    subject: str
    nonce: str

class GoogleIdentityVerifier(Protocol):
    def verify(self, id_token: str, audience: str) -> GoogleTokenClaims: ...
```

- [ ] **Step 5: Implement the Google adapter**

Create `attention_router/integrations/google_identity.py` using `google.auth.transport.requests.Request` and `google.oauth2.id_token.verify_oauth2_token`. Catch library exceptions and raise `GoogleCredentialInvalid("google credential invalid")`; validate non-empty string `sub` and `nonce`; return only `GoogleTokenClaims(subject, nonce)`. Do not return email, name, picture, raw token, issuer, or audience.

- [ ] **Step 6: Run verifier tests**

```bash
python -m pytest -q tests/test_google_identity_verifier.py
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml attention_router/core/human_identity.py attention_router/integrations/google_identity.py tests/test_google_identity_verifier.py
git commit -m "feat: add Google identity verifier boundary"
```

---

### Task 3: Persist challenges and Human Identity provider bindings

**Files:**
- Modify: `attention_router/infrastructure/models.py`
- Create: `alembic/versions/0038_human_identity_google_v1.py`
- Create: `attention_router/infrastructure/human_identity_repository.py`
- Create: `tests/integration/test_postgres_human_identity.py`

**Interfaces:**
- Produces ORM rows `HumanIdentityRow`, `HumanIdentityProviderBindingRow`, and `HumanAuthChallengeRow`.
- Produces `insert_challenge`, `lock_challenge`, `consume_challenge`, and `resolve_human_identity` repository functions.

- [ ] **Step 1: Write PostgreSQL schema and concurrency tests first**

Use the existing `postgres` marker and the connection/thread pattern from `tests/integration/test_postgres_integration_admission.py`. Assert duplicate `(provider, subject_sha256)` is rejected, `expires_at <= issued_at` is rejected, providers other than `google` are rejected, the same subject resolves to one Human Identity, and row locking serializes double consumption.

- [ ] **Step 2: Run the focused PostgreSQL target and verify RED**

```bash
POSTGRES_TEST_SELECTOR="tests/integration/test_postgres_human_identity.py -m postgres" ./scripts/postgres_test_harness.sh
```

Expected: FAIL because migration/rows/repository do not exist.

- [ ] **Step 3: Add ORM rows**

Append focused models to `attention_router/infrastructure/models.py`:

```python
class HumanIdentityRow(Base):
    __tablename__ = "human_identities"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (CheckConstraint("status = 'ACTIVE'", name="ck_human_identity_status"),)

class HumanIdentityProviderBindingRow(Base):
    __tablename__ = "human_identity_provider_bindings"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    human_identity_id: Mapped[str] = mapped_column(String(64), ForeignKey("human_identities.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("provider", "subject_sha256", name="uq_human_provider_subject"),
        CheckConstraint("provider = 'google'", name="ck_human_provider_google_v1"),
        CheckConstraint("length(subject_sha256) = 64", name="ck_human_subject_digest"),
    )

class HumanAuthChallengeRow(Base):
    __tablename__ = "human_auth_challenges"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    nonce_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("nonce_sha256", name="uq_human_auth_nonce_digest"),
        CheckConstraint("provider = 'google'", name="ck_human_auth_provider_google_v1"),
        CheckConstraint("length(nonce_sha256) = 64", name="ck_human_auth_nonce_digest"),
        CheckConstraint("expires_at > issued_at", name="ck_human_auth_challenge_lifetime"),
    )
```

- [ ] **Step 4: Create migration 0038**

Create `alembic/versions/0038_human_identity_google_v1.py` with `revision = "0038_human_identity_google_v1"` and `down_revision = "0037_integration_admission_v0"`, creating exactly the three tables above. Downgrade must refuse while any table contains data by raising `RuntimeError("HUMAN_IDENTITY_DOWNGRADE_REQUIRES_DATA_EXPORT")`, then drop challenges, bindings, identities in that order.

- [ ] **Step 5: Implement the focused repository**

Create `attention_router/infrastructure/human_identity_repository.py`. Use `select(...).with_for_update()` in `lock_challenge`. Use `uuid.uuid4().hex` with `human_` and `hbind_` prefixes. In PostgreSQL use `sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_nothing(...)` on `(provider, subject_sha256)` so concurrent valid challenges for the same Google subject converge to one provider binding. Re-select the winning binding before returning. Delete any orphan Human Identity created by the losing race in the same transaction.

- [ ] **Step 6: Run PostgreSQL tests**

```bash
POSTGRES_TEST_SELECTOR="tests/integration/test_postgres_human_identity.py -m postgres" ./scripts/postgres_test_harness.sh
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add attention_router/infrastructure/models.py attention_router/infrastructure/human_identity_repository.py alembic/versions/0038_human_identity_google_v1.py tests/integration/test_postgres_human_identity.py
git commit -m "feat: persist human auth challenges and identities"
```

---

### Task 4: Implement challenge issuance and atomic Google validation

**Files:**
- Create: `attention_router/application/human_auth.py`
- Modify: `attention_router/config.py`
- Create: `tests/test_human_auth_service.py`
- Modify: `tests/integration/test_postgres_human_identity.py`

**Interfaces:**
- Produces `HumanAuthChallenge`, `HumanIdentityValidated`, `HumanAuthFailure`, and `HumanAuthService`.

- [ ] **Step 1: Write service tests with in-memory fakes**

Create tests proving: raw nonce is returned but only its SHA-256 reaches the repository; valid claims consume then resolve; nonce mismatch never consumes/resolves; consumed challenge fails; expiry uses a `now_factory` called after the row lock; verifier failure never consumes; missing audience fails closed. Use synthetic strings and tiny fakes; no PostgreSQL or Google network in unit tests.

- [ ] **Step 2: Run tests and verify RED**

```bash
python -m pytest -q tests/test_human_auth_service.py
```

Expected: FAIL because `attention_router.application.human_auth` does not exist.

- [ ] **Step 3: Add configuration fields**

Add to `Settings` in `attention_router/config.py`:

```python
    google_oauth_client_id: str | None = None
    human_auth_challenge_ttl_seconds: int = 300
```

Add validation:

```python
        if self.human_auth_challenge_ttl_seconds <= 0:
            raise ValueError("HUMAN_AUTH_CHALLENGE_TTL_SECONDS must be positive")
```

Do not make `GOOGLE_OAUTH_CLIENT_ID` globally mandatory; existing non-client runtimes must continue to start.

- [ ] **Step 4: Implement the service**

Create `attention_router/application/human_auth.py` with:

```python
@dataclass(frozen=True)
class HumanAuthChallenge:
    challenge_id: str
    nonce: str
    expires_at: datetime

@dataclass(frozen=True)
class HumanIdentityValidated:
    human_identity_id: str

class HumanAuthFailure(Exception):
    def __init__(self, code: HumanAuthErrorCode):
        super().__init__(code.value)
        self.code = code
```

`HumanAuthService.issue_google_challenge(session)` uses `secrets.token_urlsafe(32)`, stores only nonce SHA-256, uses `hauth_<uuidhex>`, and expires after configured TTL. `validate_google` verifies the token first, then locks the challenge, then captures current time; rejects consumed before expired; rejects `now >= expires_at`; compares nonce digests with `hmac.compare_digest`; resolves `google` subject by SHA-256; marks challenge consumed; flushes. Caller owns commit/rollback so resolution and consumption commit atomically.

Error mapping: missing audience → `HUMAN_AUTH_UNAVAILABLE`; library failure/unknown challenge/nonce mismatch → `GOOGLE_CREDENTIAL_INVALID`; consumed → `CHALLENGE_CONSUMED`; expiry → `CHALLENGE_EXPIRED`.

- [ ] **Step 5: Run service tests**

```bash
python -m pytest -q tests/test_human_auth_service.py
```

Expected: PASS.

- [ ] **Step 6: Add the real PostgreSQL replay-race test**

Start two independent validations for the same challenge with an injected fake verifier returning the same subject/nonce. Assert exactly one commits `HUMAN_IDENTITY_VALIDATED`, the other observes `CHALLENGE_CONSUMED`, and exactly one provider binding exists.

- [ ] **Step 7: Run focused PostgreSQL tests**

```bash
POSTGRES_TEST_SELECTOR="tests/integration/test_postgres_human_identity.py -m postgres" ./scripts/postgres_test_harness.sh
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add attention_router/application/human_auth.py attention_router/config.py tests/test_human_auth_service.py tests/integration/test_postgres_human_identity.py
git commit -m "feat: validate Google human identity atomically"
```

---

### Task 5: Expose the isolated Client API HTTP app

**Files:**
- Create: `attention_router/web/client_api_app.py`
- Create: `tests/test_client_api_human_auth.py`

**Interfaces:**
- Produces `create_client_api_app(...) -> FastAPI` and module-level `app`.
- Maps service failures to exact contract status/error codes.

- [ ] **Step 1: Write route tests against an injected fake service**

Use `fastapi.testclient.TestClient`. Assert challenge returns HTTP 201 and exactly `challenge_id`, `nonce`, `expires_at`; validate returns HTTP 200 and exactly `status=HUMAN_IDENTITY_VALIDATED`, `human_identity_id`; errors map `GOOGLE_CREDENTIAL_INVALID→401`, `CHALLENGE_CONSUMED→409`, `CHALLENGE_EXPIRED→410`, `HUMAN_AUTH_UNAVAILABLE→503`; malformed request maps 400 `INVALID_REQUEST`; synthetic tokens never appear in response bodies or captured logs.

- [ ] **Step 2: Run route tests and verify RED**

```bash
python -m pytest -q tests/test_client_api_human_auth.py
```

Expected: FAIL because the app module does not exist.

- [ ] **Step 3: Implement the Client API app**

Create `attention_router/web/client_api_app.py` with local Pydantic request/response models matching the OpenAPI document; a session dependency that commits on success, rolls back on exception, closes always; injected service support for tests; default service wired to `GoogleAuthLibraryVerifier`, `settings.google_oauth_client_id`, and `settings.human_auth_challenge_ttl_seconds`; `docs_url=None`, `redoc_url=None`; bounded `/health/live` and `/health/ready` are allowed but are not Human Identity contract operations.

Use exactly:

```python
STATUS_BY_ERROR = {
    HumanAuthErrorCode.INVALID_REQUEST: 400,
    HumanAuthErrorCode.GOOGLE_CREDENTIAL_INVALID: 401,
    HumanAuthErrorCode.CHALLENGE_CONSUMED: 409,
    HumanAuthErrorCode.CHALLENGE_EXPIRED: 410,
    HumanAuthErrorCode.HUMAN_AUTH_UNAVAILABLE: 503,
}
```

Generate request IDs as `req_<uuid4 hex>`. Never include exception text in client responses. Install a local `RequestValidationError` handler returning `INVALID_REQUEST`. End with `app = create_client_api_app()`. Do not mount this app from the admin app and do not modify ingress apps.

- [ ] **Step 4: Run route tests**

```bash
python -m pytest -q tests/test_client_api_human_auth.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add attention_router/web/client_api_app.py tests/test_client_api_human_auth.py
git commit -m "feat: expose human identity client API"
```

---

### Task 6: Document configuration, privacy, and exact boundary

**Files:**
- Create: `docs/human-identity-google-backend-v1.md`
- Modify: `tests/test_human_auth_service.py`
- Modify: `tests/test_client_api_human_auth.py`

**Interfaces:**
- Documents `GOOGLE_OAUTH_CLIENT_ID` and `HUMAN_AUTH_CHALLENGE_TTL_SECONDS=300`.
- Does not deploy.

- [ ] **Step 1: Add privacy regression assertions**

Exercise success and failure with strings `synthetic-sensitive-token`, `synthetic-sensitive-subject`, and `synthetic-sensitive-nonce`; capture logs with `caplog` and assert none occur raw.

- [ ] **Step 2: Run the privacy tests**

```bash
python -m pytest -q tests/test_human_auth_service.py tests/test_client_api_human_auth.py
```

Expected: PASS after any sensitive logging is removed.

- [ ] **Step 3: Write the scope document**

Create `docs/human-identity-google-backend-v1.md` documenting the two endpoints; `GOOGLE_OAUTH_CLIENT_ID` is the Google Web/server OAuth client ID used as audience; raw nonce returned once and stored only as SHA-256; provider subject stored only as SHA-256; ID token never persisted; success grants Human Identity recognition only; no tenant/device/session side effect; deployment requires explicit later authorization; real mobile acceptance requires HTTPS.

- [ ] **Step 4: Run focused tests**

```bash
python -m pytest -q tests/test_human_identity_contract.py tests/test_google_identity_verifier.py tests/test_human_auth_service.py tests/test_client_api_human_auth.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/human-identity-google-backend-v1.md tests/test_human_auth_service.py tests/test_client_api_human_auth.py
git commit -m "docs: define human identity backend boundary"
```

---

### Task 7: Full backend verification and PR gate

**Files:**
- No new production files unless verification exposes a defect.

**Interfaces:**
- Produces an exact candidate SHA ready for protected-branch review.
- Does not merge or deploy.

- [ ] **Step 1: Run all offline Python tests**

```bash
python -m pytest -q
```

Expected: PASS.

- [ ] **Step 2: Run lint and compile verification**

```bash
ruff check .
python -m compileall -q attention_router tests scripts
```

Expected: PASS.

- [ ] **Step 3: Run the full PostgreSQL harness**

```bash
./scripts/postgres_test_harness.sh
```

Expected: PASS, including migration 0038 and new concurrency cases.

- [ ] **Step 4: Run the repository secret scan**

Use the exact secret-scan command wired by `.github/workflows/ci.yml` on current main. Do not replace it with a weaker scanner. Expected: PASS with no real token/client secret/signing material.

- [ ] **Step 5: Confirm scope by diff**

```bash
git diff --check origin/main...HEAD
git diff --name-only origin/main...HEAD
```

Expected changed paths are limited to:

```text
alembic/versions/0038_human_identity_google_v1.py
attention_router/application/human_auth.py
attention_router/config.py
attention_router/core/human_identity.py
attention_router/infrastructure/human_identity_repository.py
attention_router/infrastructure/models.py
attention_router/integrations/google_identity.py
attention_router/web/client_api_app.py
contracts/client/v1/human-identity-v1.openapi.json
docs/human-identity-google-backend-v1.md
pyproject.toml
tests/integration/test_postgres_human_identity.py
tests/test_client_api_human_auth.py
tests/test_google_identity_verifier.py
tests/test_human_auth_service.py
tests/test_human_identity_contract.py
```

- [ ] **Step 6: Open a PR; do not merge**

Suggested title: `feat(auth): add Google-backed Human Identity V1`.

PR body must state Human Identity only; short-lived/single-use challenge; nonce and subject stored only as digests; token not persisted; no tenant/device/session side effect; no deployment; exact local and PostgreSQL test evidence.

Wait for required checks: `python-tests`, `transport-tests`, `postgres-integration`, `docker-build`, `secret-scan`, `analyze (python)`, and `analyze (javascript-typescript)`.

- [ ] **Step 7: Stop at exact-SHA review gate**

Record:

```text
HUMAN_IDENTITY_BACKEND_V1_STATUS=READY_FOR_REVIEW
BASE_SHA=<fresh-main-sha-used-for-branch>
HEAD_SHA=<candidate-sha>
PR_URL=<pull-request-url>
DEPLOYED=NO
MERGED=NO
```

Do not merge until explicit user authorization.
