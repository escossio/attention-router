# Owner Temporal Understanding V1

Status: implementation candidate in an isolated branch, feature OFF by default. No authorization for production rollout.

## Contracts

The owner may say "aguarde um ano", "configure a espera padrão para vinte minutos" or "pause as respostas automáticas por um ano". The interpreter separates duration, intention, scope and condition. A bare "aguarde um ano" is ambiguous and must trigger Clarification; it cannot automatically mutate the global default.

The units SECOND, MINUTE, HOUR, DAY, WEEK, MONTH and YEAR are supported in a strictly typed proposal. The AI may propose semantic fields but cannot supply a trusted clock, timezone, computed deadline, authority or delivery permission. Complex language can use the existing opt-in semantic model; simple phrases have a deterministic fallback.

The currently executable new operation is SET_OWNER_REPLY_GRACE_PERIOD, a persistent default-grace configuration controlled by the existing authenticated owner path. ONE_SHOT_REPLY_DELAY_PERIOD remains UNAVAILABLE. A global timed pause can be understood but has no executor in this phase, so it does not become a different command.

## Arithmetic and persistence

Seconds, minutes, hours, days and weeks use elapsed UTC intervals. Months and years use a trusted IANA timezone and local calendar arithmetic, clamping the day to the last day of the target month. Invalid or ambiguous daylight-saving local times are rejected. The horizon is bounded to ten years.

The structured period is persisted in existing operational-control and per-conversation grace-window provenance, along with each window's absolute due timestamp. The existing integer seconds field is only legacy compatibility metadata for calendar units, not the source of deadline arithmetic. No migration or new container is required.

An inbound message can extend a trailing-edge wait. A manual owner reply cancels it. Changing the default recalculates open windows. Duplicate commands remain idempotent, while replaying the same command ID with a different period is rejected. An overdue long wait beyond a short safety tolerance is canceled rather than replayed.

The existing freshness ceiling is NOT increased globally. An old inbound can pass the narrowly scoped exception only with a timely, provenance-consistent release of its own waiting window and a valid activation watermark.

## Safety, rollout and limits

OWNER_TEMPORAL_WAIT_ENABLED defaults to false. OWNER_TEMPORAL_TIMEZONE is a trusted operator configuration; the fallback is UTC. The old 0-to-300-second path remains supported. Disabling the feature also disables the long-wait control, even if it has a persisted period.

The implementation does not change the Android numeric slider, does not activate one-shot reply scheduling and does not implement globally timed re-enablement of responses. These remain separate changes. It does not turn on existing automatic replies, send WhatsApp messages, call providers or deploy runtime components.

Live continuity retains the established ingress, decision, policy, outbox and idempotency paths. Historical acceleration is unchanged, with no import, replay or cursor reset. No new VLAN, IP, L2/L3 network identity, port or endpoint is introduced.

Rollback requires the operator to disable the feature and review any long-lived pending windows; do not silently convert calendar waits into approximated second counts or dispatch overdue messages.
