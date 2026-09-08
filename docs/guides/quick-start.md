# Local core Quick Start

## Scope

This setup starts PostgreSQL and the core API on one development machine. It applies migrations and seeds policies. It does not send messages, synthesize audio or call an LLM. Base-image and package downloads require Internet access during the build.

Use the README's clone commands, then:

```bash
cp .env.example .env
docker compose -p attention-router-demo up --build --wait db api
curl --fail http://127.0.0.1:8080/health/ready
docker compose -p attention-router-demo ps
```

Keep `.env` untracked. Its marked example credentials are public, disposable local values, not secrets suitable for deployment. PostgreSQL is accessible only within the Compose network. The API host binding is loopback and its public example port is configurable with `PUBLIC_HTTP_PORT` (default 8080).

The container's internal API port is 18100; do not confuse it with the host port. `/health/ready` checks core readiness, not availability of optional providers or an authorized messaging channel.

## Optional worker and channels

`docker compose -p attention-router-demo up --build --wait` starts the worker too. Default external-delivery and provider flags remain disabled. The `channels` profile contains ingress processes; it is not a provisioned WhatsApp/browser installation.

Full functionality additionally requires choosing and configuring the optional Agent, authenticated STT/TTS services, a transport and the appropriate identity/policy/Owner Control configuration. The external TTS service must support the adapter's language contract. This guide deliberately does not bootstrap those services or bypass controls.

## Troubleshooting

- Port occupied: set an unused `PUBLIC_HTTP_PORT` in `.env`, then recreate only this demo project.
- Database unhealthy: inspect `docker compose -p attention-router-demo logs db`; do not reuse a production volume.
- API unhealthy: inspect this project's API logs for migration/startup failures. Redact credentials and payloads before sharing logs.
- Provider disabled: expected for the local core demo, not a reason to insert a real key into tests.

`docker compose -p attention-router-demo down` stops the demo. Volumes remain unless you explicitly choose to delete this disposable project's data. Never run cleanup commands against a production Compose project.
