# Project status

Updated: 2026-09-09

## Public prerelease baseline

- Main: `4f8df380eaa4d5ffedc0b4fe7587f5af1b625c95`
- Security fix remains traceable as `31d3f8b5cab44928ded455d80a72ba3b57e49431`.
- Main is protected with seven required checks and conversation resolution.
- Dependabot is active for pip, npm, and GitHub Actions, targeting `main` weekly.
- CodeQL High open: `0`; alert #3 is dismissed with technical justification.
- Verification: Python `986`, Transport `228`, PostgreSQL `325`.
- Public CI, Docker build, Secret Scan, and CodeQL passed after merge.
- Runtime and database were not mutated by this professionalization work.

## Release state

The repository is prepared for the `v0.1.0` public prerelease. The release tag remains gated on final tag and public checkout verification.
