# Client API Authority Bootstrap V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the first language-neutral Client API V1 bootstrap contract and a pure fail-closed server authority model for human membership, device enrollment context, active tenant, and short client sessions, without shipping HTTP routes, persistence, provider calls, or mobile code.

**Architecture:** This is the first implementation sub-project from the approved Andy Android foundation spec. The canonical external artifact is a hand-authored OpenAPI 3.1 JSON contract for the bootstrap slice, validated by a shared synthetic conformance corpus. Server authority is modeled separately as pure Python immutable records and deterministic guards; a later plan will bind these contracts to PostgreSQL, Google validation, email delivery, cryptographic device proof, FastAPI routes, and session issuance.

**Tech Stack:** Python 3.11+, OpenAPI 3.1 JSON, JSON Schema 2020-12, `jsonschema`, pytest, existing GitHub Public CI.

**Spec:** `docs/superpowers/specs/2026-09-11-andy-android-foundation-design.md`

## Global Constraints

- Attention Router remains the authoritative backend and policy/execution boundary.
- The mobile client never accesses PostgreSQL, workers, containers, internal ingress, or provider internals directly.
- Android is the first mobile client, not the only possible client.
- A user may belong to multiple tenants, but one tenant is active per application session.
- A client-supplied `tenant_id`, `device_id`, membership claim, or capability claim never grants authority by itself.
- New-device verification is distinct from normal session use.
- Application sessions are short-lived and scoped to `human identity + device + active tenant`.
- Integration Bearer credentials are not reused as Android human-session credentials.
- The existing `contracts/integration/v1` schema is not modified or overloaded with client login/session semantics.
- No HTTP receiver, database migration, real Google call, email delivery, cryptographic key verification, SDK generation, Android repository, provider activation, runtime mutation, or deployment is part of this plan.
- All fixtures are synthetic. Never commit real tokens, email codes, identities, locations, conversations, provider credentials, or environment inventories.
- Failure is fail-closed: unknown or inconsistent authority state produces denial, never implicit fallback.

---

## File Structure

The bootstrap slice is intentionally small and isolated.

- Create `contracts/client/v1/client-api.openapi.json` — canonical language-neutral HTTP contract for the first client bootstrap operations and payload schemas.
- Create `contracts/client/v1/conformance-cases.json` — synthetic wire-valid/wire-invalid request and response corpus.
- Create `scripts/check_client_contract.py` — validates OpenAPI structural invariants and the shared conformance corpus without importing runtime/database/provider code.
- Create `attention_router/core/client_authority.py` — pure immutable authority records and deterministic fail-closed evaluation.
- Create `tests/test_client_contract.py` — protocol/security invariants for the OpenAPI artifact.
- Create `tests/test_client_contract_conformance.py` — independent JSON Schema validation of the shared corpus.
- Create `tests/test_client_authority.py` — authority matrix and cross-tenant/device negative tests.
- Create `docs/client-api-bootstrap-v1.md` — public status/scope document that clearly separates contract from shipped functionality.
- Modify `.github/workflows/ci.yml` — add a required Python-job contract checker step.

The next plan, not this one, will add runtime Pydantic/FastAPI request models, persistence, provider verification, email challenge state, real device proof, session issuance/renewal, and PostgreSQL tests.

---

### Task 1: Publish the canonical Client API V1 bootstrap contract

**Files:**
- Create: `contracts/client/v1/client-api.openapi.json`
- Test: `tests/test_client_contract.py`

**Interfaces:**
- Consumes: approved identity/tenant/device/session/bootstrap semantics from the design spec.
- Produces: OpenAPI 3.1 operations `POST /api/v1/auth/google/start`, `POST /api/v1/auth/device-verification/complete`, `POST /api/v1/tenants/{tenant_id}/activate`, and `GET /api/v1/bootstrap`; component schemas used by Tasks 2 and 4.

- [ ] **Step 1: Write the failing contract-presence and security test**

Create `tests/test_client_contract.py` with a loader and the first invariants:

```python
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "contracts/client/v1/client-api.openapi.json"


def load_openapi() -> dict:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def test_client_bootstrap_openapi_has_expected_version_and_operations():
    document = load_openapi()
    assert document["openapi"] == "3.1.0"
    assert document["info"]["version"] == "1"
    assert set(document["paths"]) == {
        "/api/v1/auth/google/start",
        "/api/v1/auth/device-verification/complete",
        "/api/v1/tenants/{tenant_id}/activate",
        "/api/v1/bootstrap",
    }


def test_pre_session_operations_do_not_claim_bearer_authority():
    document = load_openapi()
    assert document["paths"]["/api/v1/auth/google/start"]["post"]["security"] == []
    assert document["paths"]["/api/v1/auth/device-verification/complete"]["post"]["security"] == []


def test_tenant_activation_and_bootstrap_require_client_session():
    document = load_openapi()
    expected = [{"ClientSession": []}]
    assert document["paths"]["/api/v1/tenants/{tenant_id}/activate"]["post"]["security"] == expected
    assert document["paths"]["/api/v1/bootstrap"]["get"]["security"] == expected
```

- [ ] **Step 2: Run the focused test and verify it fails because the contract does not exist**

Run:

```bash
python -m pytest -q tests/test_client_contract.py
```

Expected: FAIL while loading `contracts/client/v1/client-api.openapi.json`.

- [ ] **Step 3: Create the minimal OpenAPI document and stable schema vocabulary**

Create `contracts/client/v1/client-api.openapi.json` as valid OpenAPI 3.1 JSON. The document must contain these exact semantic building blocks:

```json
{
  "openapi": "3.1.0",
  "info": {
    "title": "Attention Router Client API",
    "version": "1"
  },
  "servers": [],
  "components": {
    "securitySchemes": {
      "ClientSession": {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "opaque-client-session"
      }
    },
    "schemas": {
      "DeviceRole": {
        "type": "string",
        "enum": ["CLIENT", "CAPABILITY_NODE"]
      },
      "TenantRole": {
        "type": "string",
        "enum": ["OWNER", "ADMIN", "MEMBER"]
      },
      "DeviceKeyDescriptor": {
        "type": "object",
        "additionalProperties": false,
        "required": ["algorithm", "public_key_spki_b64url"],
        "properties": {
          "algorithm": {"const": "ES256"},
          "public_key_spki_b64url": {"type": "string", "minLength": 32, "maxLength": 2048}
        }
      },
      "ClientVersionInfo": {
        "type": "object",
        "additionalProperties": false,
        "required": ["app_version", "sdk_version", "contract_version"],
        "properties": {
          "app_version": {"type": "string", "minLength": 1, "maxLength": 64},
          "sdk_version": {"type": "string", "minLength": 1, "maxLength": 64},
          "contract_version": {"const": "1"}
        }
      }
    }
  },
  "paths": {}
}
```

Use `additionalProperties: false` on every request/response object unless the field is explicitly an extensible sanitized metadata map. Use RFC 3339 `format: date-time` for server times. Use opaque bounded strings for server identities (`human_identity_id`, `tenant_id`, `device_id`, `auth_transaction_id`, `session_id`, `request_id`) instead of embedding provider identifiers into canonical IDs.

Define the request/response schemas with these exact responsibilities:

```text
GoogleAuthStartRequest
  google_id_token               sensitive provider assertion, never canonical identity
  installation_id               local installation reference, not authority
  canonical_device_name
  platform                      currently ANDROID/IOS, extensible by contract revision
  roles                         CLIENT and/or CAPABILITY_NODE
  device_key                    DeviceKeyDescriptor
  client                        ClientVersionInfo

DeviceVerificationChallenge
  contract_version = "1"
  auth_transaction_id
  device_challenge_b64url
  email_hint                    masked/display-only, never authority
  expires_at

DeviceVerificationCompleteRequest
  auth_transaction_id
  email_code
  device_signature_b64url

ClientSession
  session_id
  access_token                  opaque short-lived session token
  token_type = "Bearer"
  expires_at
  human_identity_id
  device_id
  tenant_id

TenantMembershipView
  tenant_id
  role
  status                        ACTIVE/SUSPENDED/REVOKED

DeviceView
  device_id
  canonical_name
  platform
  roles
  status                        ACTIVE/REVOKED

BootstrapSnapshot
  contract_version = "1"
  human_identity_id
  active_tenant_id
  memberships[]
  device
  capabilities[]
  channel_installations[]
  integration_installations[]
  pending_attention_count
  sync_cursor                   nullable opaque cursor
  server_time

DeviceVerificationCompleteResponse
  session
  bootstrap

TenantActivationResponse
  session                       new active-tenant-scoped session
  bootstrap

ClientError
  contract_version = "1"
  error_code
  request_id
```

The operation behavior encoded in OpenAPI descriptions/responses must be:

```text
POST /api/v1/auth/google/start
  202 -> DeviceVerificationChallenge
  400 -> INVALID_REQUEST
  401 -> GOOGLE_AUTH_INVALID
  429 -> RATE_LIMITED
  503 -> AUTH_UNAVAILABLE

POST /api/v1/auth/device-verification/complete
  200 -> DeviceVerificationCompleteResponse
  400 -> INVALID_REQUEST
  401 -> VERIFICATION_INVALID_OR_EXPIRED
  409 -> ENROLLMENT_CONFLICT
  429 -> RATE_LIMITED
  503 -> AUTH_UNAVAILABLE

POST /api/v1/tenants/{tenant_id}/activate
  200 -> TenantActivationResponse
  401 -> UNAUTHENTICATED
  403 -> TENANT_FORBIDDEN
  404 -> not used to reveal cross-tenant existence; unauthorized selection is 403
  409 -> DEVICE_NOT_ACTIVE when applicable
  503 -> SESSION_UNAVAILABLE

GET /api/v1/bootstrap
  200 -> BootstrapSnapshot
  401 -> UNAUTHENTICATED
  403 -> TENANT_FORBIDDEN
  409 -> DEVICE_NOT_ACTIVE
  503 -> BOOTSTRAP_UNAVAILABLE
```

Do not add session renewal in this slice. Renewal requires a separately reviewed device-possession protocol; publishing an under-specified refresh endpoint now would freeze an insecure boundary.

- [ ] **Step 4: Extend the test with authority and disclosure invariants**

Append tests that ensure no tenant-selection header or integration credential is introduced:

```python
def test_client_contract_has_only_client_session_security_scheme():
    document = load_openapi()
    schemes = document["components"]["securitySchemes"]
    assert set(schemes) == {"ClientSession"}
    assert schemes["ClientSession"]["scheme"] == "bearer"


def test_tenant_activation_uses_path_assertion_but_server_session_is_authority():
    document = load_openapi()
    operation = document["paths"]["/api/v1/tenants/{tenant_id}/activate"]["post"]
    parameter = next(item for item in operation["parameters"] if item["name"] == "tenant_id")
    assert parameter["in"] == "path"
    assert "authority" in operation["description"].lower()
    assert "membership" in operation["description"].lower()


def test_all_application_error_payloads_use_client_error_schema():
    document = load_openapi()
    for path_item in document["paths"].values():
        for operation in path_item.values():
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            for status, response in operation["responses"].items():
                if str(status).startswith(("4", "5")):
                    schema = response["content"]["application/json"]["schema"]
                    assert schema == {"$ref": "#/components/schemas/ClientError"}
```

- [ ] **Step 5: Run the contract tests and make them pass**

Run:

```bash
python -m pytest -q tests/test_client_contract.py
```

Expected: PASS.

- [ ] **Step 6: Commit the contract slice**

```bash
git add contracts/client/v1/client-api.openapi.json tests/test_client_contract.py
git commit -m "feat: define client API bootstrap contract"
```

---

### Task 2: Add shared wire conformance cases and a standalone contract checker

**Files:**
- Create: `contracts/client/v1/conformance-cases.json`
- Create: `scripts/check_client_contract.py`
- Create: `tests/test_client_contract_conformance.py`

**Interfaces:**
- Consumes: component schema names from Task 1.
- Produces: a language-neutral corpus later reused by Kotlin/Swift SDK conformance; command `python scripts/check_client_contract.py` used by CI.

- [ ] **Step 1: Write the failing conformance harness**

Create `tests/test_client_contract_conformance.py`:

```python
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, RefResolver


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = ROOT / "contracts/client/v1"
OPENAPI = json.loads((CONTRACT_DIR / "client-api.openapi.json").read_text(encoding="utf-8"))
CORPUS = json.loads((CONTRACT_DIR / "conformance-cases.json").read_text(encoding="utf-8"))


def validator_for(schema_name: str) -> Draft202012Validator:
    schema = OPENAPI["components"]["schemas"][schema_name]
    resolver = RefResolver.from_schema(OPENAPI)
    return Draft202012Validator(schema, resolver=resolver, format_checker=FormatChecker())


def test_conformance_corpus_has_unique_ids_and_known_schemas():
    cases = CORPUS["cases"]
    assert CORPUS["contract_version"] == "1"
    assert len({case["id"] for case in cases}) == len(cases)
    known = set(OPENAPI["components"]["schemas"])
    assert {case["schema"] for case in cases} <= known


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda case: case["id"])
def test_wire_cases_match_expected_validity(case):
    payload = copy.deepcopy(case["message"])
    errors = list(validator_for(case["schema"]).iter_errors(payload))
    assert (not errors) == case["wire_valid"], [error.message for error in errors]
    assert payload == case["message"]
```

- [ ] **Step 2: Run the harness and verify it fails because the corpus is absent**

Run:

```bash
python -m pytest -q tests/test_client_contract_conformance.py
```

Expected: FAIL loading `conformance-cases.json`.

- [ ] **Step 3: Create a synthetic conformance corpus that covers every bootstrap payload family**

Create `contracts/client/v1/conformance-cases.json` with this structure and at least these cases:

```json
{
  "contract_version": "1",
  "cases": [
    {
      "id": "google-start-valid-android",
      "schema": "GoogleAuthStartRequest",
      "wire_valid": true,
      "message": {
        "google_id_token": "synthetic-google-id-token-not-a-secret",
        "installation_id": "synthetic-installation-1",
        "canonical_device_name": "Synthetic Android",
        "platform": "ANDROID",
        "roles": ["CLIENT", "CAPABILITY_NODE"],
        "device_key": {
          "algorithm": "ES256",
          "public_key_spki_b64url": "c3ludGhldGljLXB1YmxpYy1rZXktbWF0ZXJpYWwtbm90LWEtc2VjcmV0"
        },
        "client": {
          "app_version": "0.1.0-dev",
          "sdk_version": "0.1.0-dev",
          "contract_version": "1"
        }
      }
    },
    {
      "id": "google-start-rejects-extra-tenant-authority",
      "schema": "GoogleAuthStartRequest",
      "wire_valid": false,
      "message": {
        "google_id_token": "synthetic-google-id-token-not-a-secret",
        "installation_id": "synthetic-installation-1",
        "canonical_device_name": "Synthetic Android",
        "platform": "ANDROID",
        "roles": ["CLIENT"],
        "device_key": {
          "algorithm": "ES256",
          "public_key_spki_b64url": "c3ludGhldGljLXB1YmxpYy1rZXktbWF0ZXJpYWwtbm90LWEtc2VjcmV0"
        },
        "client": {
          "app_version": "0.1.0-dev",
          "sdk_version": "0.1.0-dev",
          "contract_version": "1"
        },
        "tenant_id": "attacker-chosen-tenant"
      }
    },
    {
      "id": "verification-complete-valid",
      "schema": "DeviceVerificationCompleteRequest",
      "wire_valid": true,
      "message": {
        "auth_transaction_id": "synthetic-auth-txn-1",
        "email_code": "123456",
        "device_signature_b64url": "c3ludGhldGljLXNpZ25hdHVyZS1ub3QtYS1zZWNyZXQ"
      }
    },
    {
      "id": "verification-complete-rejects-missing-signature",
      "schema": "DeviceVerificationCompleteRequest",
      "wire_valid": false,
      "message": {
        "auth_transaction_id": "synthetic-auth-txn-1",
        "email_code": "123456"
      }
    }
  ]
}
```

Add valid and invalid cases for all of these schemas: `DeviceVerificationChallenge`, `ClientSession`, `TenantMembershipView`, `DeviceView`, `BootstrapSnapshot`, `DeviceVerificationCompleteResponse`, `TenantActivationResponse`, and `ClientError`.

The negative cases must explicitly include:

```text
unknown object property
wrong contract version
blank opaque identity
invalid date-time
invalid role/status enum
bootstrap active_tenant_id absent from ACTIVE memberships
client session tenant differing from bootstrap active tenant
empty device roles
server response containing a provider refresh token field
```

For the two cross-field bootstrap invariants, encode the wire shape as valid JSON Schema where appropriate, then assert semantic rejection in Task 3 authority/bootstrap tests rather than pretending JSON Schema proves server authority.

- [ ] **Step 4: Create the standalone checker using the same corpus**

Create `scripts/check_client_contract.py` with an explicit zero-on-success/nonzero-on-failure interface:

```python
from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, RefResolver


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = ROOT / "contracts/client/v1"
OPENAPI_PATH = CONTRACT_DIR / "client-api.openapi.json"
CORPUS_PATH = CONTRACT_DIR / "conformance-cases.json"


def main() -> None:
    openapi = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))

    if openapi.get("openapi") != "3.1.0":
        raise SystemExit("client contract must use OpenAPI 3.1.0")
    if openapi.get("info", {}).get("version") != corpus.get("contract_version"):
        raise SystemExit("contract/corpus version mismatch")

    seen: set[str] = set()
    for case in corpus["cases"]:
        if case["id"] in seen:
            raise SystemExit(f"duplicate conformance case id: {case['id']}")
        seen.add(case["id"])
        schema_name = case["schema"]
        try:
            schema = openapi["components"]["schemas"][schema_name]
        except KeyError as exc:
            raise SystemExit(f"unknown conformance schema: {schema_name}") from exc
        validator = Draft202012Validator(
            schema,
            resolver=RefResolver.from_schema(openapi),
            format_checker=FormatChecker(),
        )
        valid = not list(validator.iter_errors(case["message"]))
        if valid != case["wire_valid"]:
            raise SystemExit(f"conformance mismatch: {case['id']}")

    print(f"CLIENT_CONTRACT_CHECK=PASS cases={len(seen)}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run both independent checks**

Run:

```bash
python scripts/check_client_contract.py
python -m pytest -q tests/test_client_contract.py tests/test_client_contract_conformance.py
```

Expected: checker prints `CLIENT_CONTRACT_CHECK=PASS ...`; pytest PASS.

- [ ] **Step 6: Commit the conformance boundary**

```bash
git add contracts/client/v1/conformance-cases.json scripts/check_client_contract.py tests/test_client_contract_conformance.py
git commit -m "test: add client API wire conformance"
```

---

### Task 3: Implement the pure fail-closed client authority model

**Files:**
- Create: `attention_router/core/client_authority.py`
- Test: `tests/test_client_authority.py`

**Interfaces:**
- Consumes: canonical identity concepts and status names published in Task 1.
- Produces: `MembershipGrant`, `DeviceGrant`, `ClientSessionGrant`, `ClientAuthorityDecision`, `evaluate_client_authority()`, and `require_client_authority()` for the later persistence/HTTP plan.

- [ ] **Step 1: Write the failing happy-path and matrix tests**

Create `tests/test_client_authority.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from attention_router.core.client_authority import (
    ClientAuthorityError,
    ClientSessionGrant,
    DeviceGrant,
    DeviceStatus,
    MembershipGrant,
    MembershipStatus,
    TenantRole,
    evaluate_client_authority,
    require_client_authority,
)


NOW = datetime(2026, 9, 11, 23, 0, tzinfo=timezone.utc)


def valid_state():
    membership = MembershipGrant(
        human_identity_id="human-1",
        tenant_id="tenant-1",
        role=TenantRole.OWNER,
        status=MembershipStatus.ACTIVE,
    )
    device = DeviceGrant(
        device_id="device-1",
        human_identity_id="human-1",
        status=DeviceStatus.ACTIVE,
    )
    session = ClientSessionGrant(
        session_id="session-1",
        human_identity_id="human-1",
        device_id="device-1",
        tenant_id="tenant-1",
        expires_at=NOW + timedelta(minutes=15),
    )
    return membership, device, session


def test_matching_active_membership_device_and_session_are_authorized():
    membership, device, session = valid_state()
    decision = evaluate_client_authority(
        membership=membership,
        device=device,
        session=session,
        now=NOW,
    )
    assert decision.allowed is True
    assert decision.reason_code == "AUTHORIZED"


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("expired", "SESSION_EXPIRED"),
        ("membership_suspended", "MEMBERSHIP_INACTIVE"),
        ("membership_revoked", "MEMBERSHIP_INACTIVE"),
        ("device_revoked", "DEVICE_REVOKED"),
        ("wrong_human", "IDENTITY_MISMATCH"),
        ("wrong_device", "DEVICE_MISMATCH"),
        ("wrong_tenant", "TENANT_MISMATCH"),
    ],
)
def test_authority_fails_closed_for_inconsistent_state(mutation, expected_reason):
    membership, device, session = valid_state()
    if mutation == "expired":
        session = ClientSessionGrant(**{**session.__dict__, "expires_at": NOW})
    elif mutation == "membership_suspended":
        membership = MembershipGrant(**{**membership.__dict__, "status": MembershipStatus.SUSPENDED})
    elif mutation == "membership_revoked":
        membership = MembershipGrant(**{**membership.__dict__, "status": MembershipStatus.REVOKED})
    elif mutation == "device_revoked":
        device = DeviceGrant(**{**device.__dict__, "status": DeviceStatus.REVOKED})
    elif mutation == "wrong_human":
        membership = MembershipGrant(**{**membership.__dict__, "human_identity_id": "human-2"})
    elif mutation == "wrong_device":
        session = ClientSessionGrant(**{**session.__dict__, "device_id": "device-2"})
    elif mutation == "wrong_tenant":
        session = ClientSessionGrant(**{**session.__dict__, "tenant_id": "tenant-2"})

    decision = evaluate_client_authority(
        membership=membership,
        device=device,
        session=session,
        now=NOW,
    )
    assert decision.allowed is False
    assert decision.reason_code == expected_reason
    with pytest.raises(ClientAuthorityError, match=expected_reason):
        require_client_authority(
            membership=membership,
            device=device,
            session=session,
            now=NOW,
        )
```

- [ ] **Step 2: Run the test and verify imports fail**

Run:

```bash
python -m pytest -q tests/test_client_authority.py
```

Expected: FAIL because `attention_router.core.client_authority` does not exist.

- [ ] **Step 3: Implement immutable authority records and deterministic evaluation**

Create `attention_router/core/client_authority.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TenantRole(StrEnum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"


class MembershipStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"


class DeviceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


@dataclass(frozen=True)
class MembershipGrant:
    human_identity_id: str
    tenant_id: str
    role: TenantRole
    status: MembershipStatus


@dataclass(frozen=True)
class DeviceGrant:
    device_id: str
    human_identity_id: str
    status: DeviceStatus


@dataclass(frozen=True)
class ClientSessionGrant:
    session_id: str
    human_identity_id: str
    device_id: str
    tenant_id: str
    expires_at: datetime


@dataclass(frozen=True)
class ClientAuthorityDecision:
    allowed: bool
    reason_code: str


class ClientAuthorityError(PermissionError):
    pass


def evaluate_client_authority(
    *,
    membership: MembershipGrant,
    device: DeviceGrant,
    session: ClientSessionGrant,
    now: datetime,
) -> ClientAuthorityDecision:
    if session.expires_at.tzinfo is None or now.tzinfo is None:
        return ClientAuthorityDecision(False, "INVALID_TIME_CONTEXT")
    if session.expires_at <= now:
        return ClientAuthorityDecision(False, "SESSION_EXPIRED")
    if membership.status is not MembershipStatus.ACTIVE:
        return ClientAuthorityDecision(False, "MEMBERSHIP_INACTIVE")
    if device.status is not DeviceStatus.ACTIVE:
        return ClientAuthorityDecision(False, "DEVICE_REVOKED")
    if membership.human_identity_id != session.human_identity_id:
        return ClientAuthorityDecision(False, "IDENTITY_MISMATCH")
    if device.human_identity_id != session.human_identity_id:
        return ClientAuthorityDecision(False, "IDENTITY_MISMATCH")
    if device.device_id != session.device_id:
        return ClientAuthorityDecision(False, "DEVICE_MISMATCH")
    if membership.tenant_id != session.tenant_id:
        return ClientAuthorityDecision(False, "TENANT_MISMATCH")
    return ClientAuthorityDecision(True, "AUTHORIZED")


def require_client_authority(
    *,
    membership: MembershipGrant,
    device: DeviceGrant,
    session: ClientSessionGrant,
    now: datetime,
) -> ClientAuthorityDecision:
    decision = evaluate_client_authority(
        membership=membership,
        device=device,
        session=session,
        now=now,
    )
    if not decision.allowed:
        raise ClientAuthorityError(decision.reason_code)
    return decision
```

Do not add default tenant fallbacks. Do not import `DEFAULT_TENANT_ID`. Do not accept a separate caller-supplied tenant argument that can disagree with the session.

- [ ] **Step 4: Add explicit cross-tenant and timezone-negative cases**

Append:

```python
def test_cross_tenant_membership_never_authorizes_session():
    membership, device, session = valid_state()
    membership = MembershipGrant(
        human_identity_id=membership.human_identity_id,
        tenant_id="tenant-other",
        role=membership.role,
        status=membership.status,
    )
    decision = evaluate_client_authority(
        membership=membership,
        device=device,
        session=session,
        now=NOW,
    )
    assert decision == type(decision)(False, "TENANT_MISMATCH")


def test_naive_time_context_fails_closed():
    membership, device, session = valid_state()
    decision = evaluate_client_authority(
        membership=membership,
        device=device,
        session=session,
        now=NOW.replace(tzinfo=None),
    )
    assert decision.allowed is False
    assert decision.reason_code == "INVALID_TIME_CONTEXT"
```

- [ ] **Step 5: Run focused and nearby authority tests**

Run:

```bash
python -m pytest -q tests/test_client_authority.py tests/test_platform_invariants_assertions.py tests/test_integration_tenant_binding.py
```

Expected: PASS. Existing tenant/integration behavior remains unchanged.

- [ ] **Step 6: Commit the authority model**

```bash
git add attention_router/core/client_authority.py tests/test_client_authority.py
git commit -m "feat: add fail-closed client authority model"
```

---

### Task 4: Freeze bootstrap semantic invariants that JSON Schema alone cannot prove

**Files:**
- Modify: `tests/test_client_contract.py`
- Modify: `tests/test_client_authority.py`

**Interfaces:**
- Consumes: OpenAPI response schemas from Task 1 and authority types from Task 3.
- Produces: explicit tests preventing future implementers from treating well-formed client payloads as authority.

- [ ] **Step 1: Add a failing test that forbids semantic authority from OpenAPI input fields**

Append to `tests/test_client_contract.py`:

```python
def test_client_contract_descriptions_keep_claims_separate_from_authority():
    document = load_openapi()
    google_start = document["paths"]["/api/v1/auth/google/start"]["post"]["description"].lower()
    tenant_activate = document["paths"]["/api/v1/tenants/{tenant_id}/activate"]["post"]["description"].lower()
    bootstrap = document["paths"]["/api/v1/bootstrap"]["get"]["description"].lower()

    assert "google" in google_start and "server" in google_start and "validate" in google_start
    assert "tenant" in tenant_activate and "membership" in tenant_activate and "authority" in tenant_activate
    assert "authoritative" in bootstrap and "session" in bootstrap
```

- [ ] **Step 2: Run and verify the test fails if the OpenAPI descriptions are too weak**

Run:

```bash
python -m pytest -q tests/test_client_contract.py::test_client_contract_descriptions_keep_claims_separate_from_authority
```

Expected: FAIL until the operation descriptions explicitly encode those invariants.

- [ ] **Step 3: Strengthen the canonical OpenAPI descriptions, not runtime code**

Update only the relevant OpenAPI `description` fields so they state all of the following in plain language:

```text
Google ID token is untrusted until server validation succeeds.
installation_id and public device key describe an enrollment attempt; they do not identify an authorized device yet.
email verification authorizes the new-device enrollment transaction only after server validation and later key-possession verification.
requested tenant path value is an assertion; active server-side membership/session state is authority.
bootstrap state is derived from the authenticated server session and must not be reconstructed from cached client claims.
```

Do not add new fields to satisfy this test.

- [ ] **Step 4: Add a test that tenant switching needs a distinct matching membership**

Append to `tests/test_client_authority.py`:

```python
def test_switching_tenant_requires_membership_for_the_target_session_tenant():
    _, device, current_session = valid_state()
    target_membership = MembershipGrant(
        human_identity_id="human-1",
        tenant_id="tenant-2",
        role=TenantRole.MEMBER,
        status=MembershipStatus.ACTIVE,
    )
    target_session = ClientSessionGrant(
        session_id="session-2",
        human_identity_id=current_session.human_identity_id,
        device_id=current_session.device_id,
        tenant_id="tenant-2",
        expires_at=current_session.expires_at,
    )
    assert require_client_authority(
        membership=target_membership,
        device=device,
        session=target_session,
        now=NOW,
    ).allowed is True

    with pytest.raises(ClientAuthorityError, match="TENANT_MISMATCH"):
        require_client_authority(
            membership=target_membership,
            device=device,
            session=current_session,
            now=NOW,
        )
```

This test encodes the rule “one active tenant per session” without creating a global active-tenant singleton.

- [ ] **Step 5: Run the whole new client slice**

Run:

```bash
python scripts/check_client_contract.py
python -m pytest -q tests/test_client_contract.py tests/test_client_contract_conformance.py tests/test_client_authority.py
```

Expected: PASS.

- [ ] **Step 6: Commit semantic authority invariants**

```bash
git add contracts/client/v1/client-api.openapi.json tests/test_client_contract.py tests/test_client_authority.py
git commit -m "test: freeze client bootstrap authority invariants"
```

---

### Task 5: Add public scope documentation and the CI gate

**Files:**
- Create: `docs/client-api-bootstrap-v1.md`
- Modify: `.github/workflows/ci.yml`
- Test: `tests/test_client_contract.py`

**Interfaces:**
- Consumes: completed contract/checker/authority model from Tasks 1–4.
- Produces: durable project status boundary and a CI gate that prevents unnoticed contract/corpus drift.

- [ ] **Step 1: Write a failing CI-configuration test**

Append to `tests/test_client_contract.py`:

```python
def test_public_ci_runs_client_contract_checker():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "python scripts/check_client_contract.py" in workflow
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
python -m pytest -q tests/test_client_contract.py::test_public_ci_runs_client_contract_checker
```

Expected: FAIL because CI does not yet call the checker.

- [ ] **Step 3: Add the checker to the existing `python-tests` job**

In `.github/workflows/ci.yml`, add this step after `python -m compileall -q attention_router tests` and before SDK generation:

```yaml
      - name: Check client API bootstrap contract
        run: python scripts/check_client_contract.py
```

Do not create a new required job for this small offline check. Keeping it in the existing required `python-tests` job avoids branch-protection churn.

- [ ] **Step 4: Create the scope/status document**

Create `docs/client-api-bootstrap-v1.md` with these exact top-level sections:

```markdown
# Client API Bootstrap V1

Status: contract + pure authority model only; no network/runtime implementation.

## Purpose
## Canonical artifacts
## Bootstrap operations
## Authority boundary
## What is implemented
## What is deliberately not implemented
## Next implementation gate
```

The content must state explicitly:

```text
Canonical HTTP artifact: contracts/client/v1/client-api.openapi.json.
Conformance corpus: contracts/client/v1/conformance-cases.json.
Pure authority model: attention_router/core/client_authority.py.
No FastAPI route serves these paths yet.
No Google token is validated by production code yet.
No email is sent by production code yet.
No device signature is verified by production code yet.
No client session token is issued by production code yet.
No tenant/device/client persistence or migration is added by this increment.
No Android/Kotlin SDK or app is created by this increment.
The next gate is a separate backend persistence + authenticated bootstrap execution plan.
```

Also document why session renewal is absent: the device-possession renewal protocol must be specified and reviewed before publishing that endpoint.

- [ ] **Step 5: Run all repository checks affected by this increment**

Run:

```bash
python -m ruff check .
python -m compileall -q attention_router tests
python scripts/check_client_contract.py
python scripts/generate_integration_sdks.py --check
python -m pytest -q
```

Expected: all commands PASS. The integration SDK check proves the new client contract did not mutate existing generated integration artifacts.

- [ ] **Step 6: Commit the docs and CI gate**

```bash
git add .github/workflows/ci.yml docs/client-api-bootstrap-v1.md tests/test_client_contract.py
git commit -m "ci: enforce client API bootstrap contract"
```

---

### Task 6: Final review gate before any backend runtime implementation

**Files:**
- Review only: all files created/modified by Tasks 1–5.

**Interfaces:**
- Consumes: the complete bootstrap contract increment.
- Produces: a reviewed commit/PR candidate suitable as the dependency for the next implementation plan; no new runtime behavior.

- [ ] **Step 1: Verify the branch diff is scope-pure**

Run:

```bash
git diff --name-only origin/main...HEAD
```

Expected paths only:

```text
.github/workflows/ci.yml
attention_router/core/client_authority.py
contracts/client/v1/client-api.openapi.json
contracts/client/v1/conformance-cases.json
docs/client-api-bootstrap-v1.md
scripts/check_client_contract.py
tests/test_client_authority.py
tests/test_client_contract.py
tests/test_client_contract_conformance.py
```

If any migration, runtime route, provider adapter, WhatsApp transport, database model, deployment file, SDK package, or Android file appears, stop and remove that unrelated change from this increment.

- [ ] **Step 2: Re-run the complete verification set from a clean working tree**

Run:

```bash
python -m ruff check .
python -m compileall -q attention_router tests
python scripts/check_client_contract.py
python scripts/generate_integration_sdks.py --check
python -m pytest -q
```

Expected: PASS with no generated drift and no provider/network calls.

- [ ] **Step 3: Inspect the authority-negative matrix manually**

Confirm tests explicitly prove denial for:

```text
expired session
suspended membership
revoked membership
revoked device
human identity mismatch
device mismatch
tenant mismatch
naive/invalid time context
client-supplied extra tenant claim in pre-auth payload
unknown request properties
wrong contract version
invalid response timestamps
```

- [ ] **Step 4: Inspect the public contract for accidental secret/provider coupling**

Run:

```bash
rg -n "AGT01|192\.168\.|DATABASE_URL|INTERNAL_INGRESS|WhatsApp Web|refresh_token|client_secret" \
  contracts/client/v1 docs/client-api-bootstrap-v1.md attention_router/core/client_authority.py
```

Expected: no infrastructure/private coupling. The string `refresh_token` may appear only in prose explaining that provider refresh tokens are forbidden from client bootstrap payloads; it must not be a contract field.

- [ ] **Step 5: Prepare the review summary without merging or deploying**

The review summary must report:

```text
CLIENT_CONTRACT_VERSION=1
CLIENT_HTTP_IMPLEMENTED=NO
CLIENT_DB_MUTATED=NO
GOOGLE_PROVIDER_CALLED=NO
EMAIL_SENT=NO
DEVICE_CRYPTO_VERIFIED=NO
ANDROID_CREATED=NO
LIVE_RUNTIME_MUTATED=NO
```

Include the contract checker case count and pytest result from the actual run. Do not claim `main` or CI is green until GitHub checks for the candidate PR have actually completed.

- [ ] **Step 6: Stop at the review boundary**

Do not implement FastAPI routes, migrations, Google verification, email delivery, session issuance, Kotlin SDK, or Android application as part of this plan. Those belong to the next separately reviewed plan.

---

## Plan Self-Review

### Spec coverage for this sub-project

This plan deliberately implements only the first dependency in spec section 19: **dedicated Client API contract and server authority model**. It covers the permanent client/integration API separation, language-neutral contract, Google-start/new-device-verification wire boundary, personal device/session/tenant semantics, one-active-tenant-per-session authority, fail-closed device/membership/session checks, bootstrap response shape, synthetic conformance, and CI enforcement.

The following approved spec areas are intentionally deferred to later plans rather than partially implemented here: PostgreSQL persistence/migrations, actual Google ID-token verification, email sending/code storage, device cryptographic challenge verification, personal-tenant creation transaction, real session issuance/renewal/revocation, FastAPI routes, Kotlin SDK generation, `andy-android`, encrypted local store/outbox, location/camera/notification capability providers, WhatsApp installation, Google/Home Assistant integrations, push notifications, and polished UX.

### Type and naming consistency

The stable names used across tasks are: `TenantRole`, `MembershipStatus`, `DeviceStatus`, `MembershipGrant`, `DeviceGrant`, `ClientSessionGrant`, `ClientAuthorityDecision`, `evaluate_client_authority`, `require_client_authority`, `ClientSession`, `BootstrapSnapshot`, and Client API contract version `"1"`.

No task introduces `DEFAULT_TENANT_ID`, provider refresh credentials, integration Bearer credentials, or a generic remote command capability.
