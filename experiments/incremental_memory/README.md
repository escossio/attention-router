# Independent memory V0 — experimental

Tracking: #479. No production activation; G3 NO_GO, G6 pending.

Python 3.13 and PostgreSQL 16.15 images are digest-pinned in compose.yaml.
Both containers use network_mode none; PostgreSQL listens only on the dedicated
Unix socket. No host ports, Docker socket, existing volumes or production secrets.
The core has a read-only root, no capabilities and offline hash-locked wheels.
The temporary dependency directory permits native library loading. No auto-restart.

## Rebuild from a validated immutable SHA

On an authorized isolated host, inspect resource and name availability first.
Do not run against existing resources bearing these names without reconciling them.
Download this repository at the validated SHA, enter this directory, then:

```sh
python3 prepare.py dependencies
python3 prepare.py credentials
docker compose up -d
# Wait for both services to finish initialization; do not print secret files.
python3 prepare.py initialize
python3 seed.py
python3 validate.py
```

Initialization and fixtures are one-time operations. validate.py is an acceptance
scenario on the initial fixture: it corrects blue to white and restarts/stops only
the two dedicated containers. It is not a production health check or an idempotent
test against an already corrected dataset. Never remove/recreate data implicitly.
The host download is an explicit temporary dependency-fetch step, not container egress.
Keep generated secrets local; never commit vendor wheels, dumps or private inventories.

Operations: register, current, history, correct(expected revision), query.
Original evidence and revisions are append-only for application roles. Revision
insertion and current pointer updates commit atomically with a deferred FK.
The CLI has one synthetic dataset and is NOT a multi-tenant authenticated API.

Vocabulary: vehicle.color (carro/veículo, qual a cor/de que cor), employment.company
(onde X trabalha). Entity names and values come from persisted rows, never fixtures
inside interpretation. Unknown questions abstain; normalized name collisions are
ambiguous; database failures are UNAVAILABLE. No model provider is called.

## Evidence and integration boundary

evidence/M0.json is historical local evidence obtained BEFORE GitHub-first was
reaffirmed. It is not certification or deployment proof of this commit. That run
passed 31 targeted checks including restart, correction/history, alternate entities,
abstention, ambiguity, real DB unavailability, network/mount isolation and SQL ACLs.
Private host inventories are deliberately excluded.

integration/composition.py is preparation only, not an activated authenticated API.
It reuses ClientSessionService/build_client_session_router and requires a real scope
authority and authority-bound memory implementation. No fake ClientSession or
APP_ENV=test bypass. Android follows escossio/andy-android#103. Obtain the first
textual end-to-end response before adding cognitive capabilities.
