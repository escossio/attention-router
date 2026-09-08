# Attention Router / Andy

Attention Router is a policy-controlled conversation system; Andy is its contextual personal agent.

Messages deserve different levels of attention, but an AI reply is not permission to act. The Router separates interpretation, policy, approval, execution and confirmed delivery, keeping the human owner in control.

## Current capabilities

- Structured inbound routing and actor/contact context.
- An optional OpenAI Agent with bounded context and structured action proposals.
- Policy evaluation, review, controlled autonomy and owner pause/resume boundaries.
- Recent user/assistant history backed by delivered replies, limited to six previous interactions with a temporal cutoff.
- Optional voice input through STT and voice output through an external TTS service, followed by Ogg/Opus normalization.
- Idempotency, transactional outbox, audit provenance and optional tracing.
- Persistent memory modules with separate eligibility/disclosure controls; disabled by default.

This is an early public-baseline candidate, not a production-ready hosted service. Fixtures are synthetic. No availability, calendar action or external delivery should be inferred from generated text alone.

## Architecture

```text
Inbound -> effective text / optional STT -> allowed Agent context
        -> structured proposal -> policy / review / autonomy
        -> execution intent -> text or external TTS -> Ogg/Opus
        -> outbox -> transport -> delivery evidence
```

See [architecture](docs/architecture/overview.md), [voice boundary](docs/guides/voice.md) and [security](SECURITY.md).

## Safety and controlled autonomy

External delivery, autonomous execution and live providers are disabled in the local example. Owner Control remains an execution authority, not a prompt instruction. Model output is a proposal; the Router decides what is authorized. An ambiguous delivery must not be treated as a confirmed reply or blindly replayed.

## Preliminary local quick start

Requires Docker with Compose. The intended startup runs a local database and Router services, not WhatsApp or an AI provider. Full Compose startup has not yet been certified; this remains a prototype configuration.

```sh
cp .env.example .env
docker compose up --build
```

The admin service binds to `127.0.0.1:18100`. The example includes deliberately public local-only credentials; replace them before sharing any deployment. Do not expose this configuration to the internet. Database migrations and synthetic policy seeding run during API startup. Use `docker compose down` to stop it; database data remains in a named volume.

For a provider-free local check using existing deterministic adapters:

```sh
docker compose run --rm --no-deps api python -m pytest -q \
  tests/test_tts_adapter.py tests/test_agent_conversation_history.py \
  tests/test_conversation_language.py
```

This fixture demonstrates context and the TTS client contract with a mock, not actual speech or live model reasoning. Optional channel ingress requires the `channels` Compose profile and explicit security configuration. Voice setup is documented separately; no private TTS source is required to run offline tests.

## Development and tests

The public suite covers the Router and WhatsApp transport. Live synthetic-peer and pairing tools are private engineering utilities, not release dependencies. Optional scenario-readiness contracts remain fail-closed without an external test harness.

Python 3.11+ (Docker uses 3.12), Node.js 20.19.2+ for transport, and ffmpeg for codec tests.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
# pytest supplies isolated synthetic settings in tests/conftest.py.
# Do not source a production .env; shell and dotenv syntax are not equivalent.
ruff check .
python -m compileall -q attention_router tests
pytest -q
cd whatsapp-transport-local
PUPPETEER_SKIP_DOWNLOAD=true npm ci --ignore-scripts
npm test
```

Default pytest excludes tests marked `postgres`. PostgreSQL integration and full Compose startup require separate isolated validation; never point tests at a production database. See [contributing](CONTRIBUTING.md).

## Roadmap and future vision

Next: reproducible onboarding, CI/security automation, dependency remediation and clearer deployment guides. Temporary Conversation Session State is future evolution, not an implemented fix for every context error. [Andy Enterprise](docs/andy-enterprise-evolution.md) is a design vision; its session, ledger, goal and management modules are not current capabilities.

## License

Apache License 2.0. See [LICENSE](LICENSE). Third-party dependencies retain their own licenses.
