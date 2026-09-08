# Contributing

This is a prerelease staging branch. Keep changes focused and explain which contract they preserve or change. Do not import private deployment material or use real identities in fixtures.

## Development and checks

Use Python 3.12, Node.js 22, `ffmpeg`, Git and Docker Compose v2. Do not source a production `.env` for tests.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m ruff check .
python -m compileall -q attention_router tests
python -m pytest -q
cd whatsapp-transport-local
PUPPETEER_SKIP_DOWNLOAD=true npm ci --ignore-scripts
npm test
cd ..
bash scripts/postgres_test_harness.sh
docker build -t attention-router:local .
bash scripts/public_secret_scan.sh
```

The secret-scan helper downloads a checksum-pinned Linux x86-64 gitleaks release to a temporary directory. It scans only `HEAD` ancestry, not unrelated historical refs.

## PostgreSQL integration

The main pytest profile excludes `postgres`. CI has a separate PostgreSQL 16 service and runs **all** tests marked `postgres`; a green unit suite is not a substitute.

The Docker harness uses a disposable database and a dedicated test URL. `PUBLIC_POSTGRES_TEST_URL` is an explicit test-only opt-in, restricted to local/test host names, the `ar_test` user and an `attention_router_test` database name. Never point it at a retained database. The harness removes its own container/network on exit.

The existing integration suite is under certification. Failures involving old expectations or product semantics must be investigated explicitly, not fixed by weakening assertions, skipping tests or altering product behavior merely to make CI green.

## Pull requests

Include targeted tests and relevant regression results. Keep test data synthetic and preserve identity/ordering relationships. Architectural changes should state authority, privacy, failure and rollback implications. Do not introduce provider calls into tests.

CI separates Python/static checks, transport, PostgreSQL, Docker build and secret scanning. CodeQL requires public-repository availability or private Code Security entitlement and explicit enablement. Dependabot configuration is staged with version-update PR creation disabled until the repository migration is approved.

Report vulnerabilities privately according to [SECURITY.md](SECURITY.md), not in a normal issue.
