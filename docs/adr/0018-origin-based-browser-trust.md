# ADR 0018: Origin-Based Browser Trust

## Status

Accepted.

## Context

Browser navigation is a trust boundary. Textual substring checks can accept a
malicious hostname, userinfo, path or protocol. The current transport security
fix established exact URL-origin validation.

## Decision

Parse navigation URLs with the WHATWG URL parser and compare the exact trusted
origin and expected protocol/host components. Invalid or unexpected URLs fail
closed.

## Consequences

Lookalike domains and userinfo confusion do not satisfy the trust check.
Allowlist changes require an explicit contract change and regression tests.

## Alternatives considered

Substring, prefix/suffix and permissive regex checks were rejected because
they validate appearance rather than URL structure.
