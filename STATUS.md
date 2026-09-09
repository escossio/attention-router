# Project status

Updated: 2026-09-09

## Architecture governance

- Phase 4E merged in `e643f634247f5e918d1b519607e6333f5ca13480`.
- Public ADR index and supplementary decisions cover authority, approval,
  outbox, delivery evidence, memory, providers and browser-origin trust.
- Public threat model, trust-boundary diagram and directional roadmap are
  available; implemented, planned and conceptual scope remains separated.
- No core product logic, database schema, migrations, runtime or immutable
  `v0.1.0` tag was changed.

## Public prerelease baseline

- Main: current protected branch; Phase 4B merge SHA is recorded in the final scorecard.
- Security fix remains traceable as `31d3f8b5cab44928ded455d80a72ba3b57e49431`.
- Main is protected with seven required checks and conversation resolution.
- Dependabot is active for pip, npm, and GitHub Actions, targeting `main` weekly.
- CodeQL High open: `0`; alert #3 is dismissed with technical justification.
- Verification: Python `986`, Transport `228`, PostgreSQL `325`.
- Public CI, Docker build, Secret Scan, and CodeQL passed after merge.
- Runtime and database were not mutated by this professionalization work.

## Release state

The immutable `v0.1.0` public prerelease targets
`7e6faa89b21e1c0f21d80c34fb3e1cf02f26c805`. Its public GHCR image is amd64
only and has an immutable digest recorded in the release assets. The release
contains an SPDX JSON SBOM, checksums, and artifact/build provenance plus SBOM
attestations. The deterministic offline architecture demo and social preview
assets are available in the repository.

No live runtime, real provider credential, real WhatsApp account, real
conversation, or real database was used or mutated.

## Portfolio demo

The public portfolio demo is available as a reproducible, silent 72-second
H.264 video at `docs/assets/demo/attention-router-demo.mp4`, with a 1920x1080
poster beside it. It is synthetic-only and offline: no real names, phone
numbers, messages, hostnames, private paths, secrets, or provider calls are
used. The renderer is `scripts/render_public_demo.py` and the README links to
the assets and `docs/demo.md`.
