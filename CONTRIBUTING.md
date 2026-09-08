# Contributing

This baseline is under preparation. Keep pull requests focused and explain the user-visible behavior, security implications and tests run.

## Setup and checks

Use Python 3.11+ in a virtual environment, install `pip install -e '.[dev]'`, and load a private local `.env` based on `.env.example`. Run `ruff check .`, `python -m compileall -q attention_router tests` and `pytest -q`. The default suite excludes PostgreSQL integration tests. Use an isolated disposable database for those tests, never production.

For transport changes use Node.js 20.19.2+, run `PUPPETEER_SKIP_DOWNLOAD=true npm ci --ignore-scripts` inside `whatsapp-transport-local`, then `npm test`. Install ffmpeg for media normalization tests. Do not enable providers or authenticate WhatsApp to run unit tests.

## Changes

Preserve existing style and public contracts. Add a focused regression test for bug fixes. Use explicitly synthetic fixtures. Architectural changes should explain authority, isolation, retention, failure behavior and migration implications in a concise design note. Do not bundle operational deployment changes into a feature PR.

Security issues follow SECURITY.md, not public issue threads. Never include private runtime evidence or credentials.
