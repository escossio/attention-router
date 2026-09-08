"""Recover historical Meta state and add exclusive inbox claims."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0030_meta_callback_release_gate"
down_revision = "0029_meta_callback_remediation"
branch_labels = None
depends_on = None

_WINDOW_SECONDS = 604800
_GRACE_SECONDS = 30
_HISTORICAL_EVIDENCE_AMBIGUITY = "HISTORICAL_EVIDENCE_INBOX_AMBIGUOUS"


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode()).hexdigest()[:48]}"


def _scope_fingerprint(scope: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            scope,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()


def _block(reason: str) -> None:
    raise RuntimeError(f"0030 historical Meta recovery blocked: {reason}")


def _recover_historical_attempts(bind: sa.Connection) -> None:
    orphans = bind.execute(
        sa.text(
            "SELECT o.id, o.interaction_id, o.execution_intent_id, "
            "o.causation_id, o.correlation_id, o.claimed_by, o.attempt_count, "
            "o.created_at, o.payload, i.tenant_id "
            "FROM outbox_messages o "
            "JOIN interactions i ON i.id = o.interaction_id "
            "LEFT JOIN meta_delivery_reconciliations mr "
            "ON mr.outbox_message_id = o.id "
            "WHERE o.destination = 'meta_whatsapp_cloud' "
            "AND o.action_type = 'production_conversation_reply' "
            "AND o.status = 'AWAITING_DELIVERY' "
            "AND mr.id IS NULL ORDER BY o.id FOR UPDATE OF o"
        )
    ).mappings().all()

    for orphan in orphans:
        payload = orphan["payload"] if isinstance(orphan["payload"], dict) else {}
        provider_id = payload.get("provider_message_id")
        if (
            not isinstance(provider_id, str)
            or not provider_id
            or len(provider_id) > 255
            or payload.get("retry_policy") != "NONE"
            or orphan["attempt_count"] != 1
            or not orphan["execution_intent_id"]
            or not orphan["causation_id"]
            or not orphan["claimed_by"]
        ):
            _block("invalid exact outbox scope")

        provider_collision = bind.execute(
            sa.text(
                "SELECT count(*) FROM meta_delivery_reconciliations mr "
                "JOIN outbox_messages o ON o.id = :outbox_id "
                "WHERE mr.provider_message_id = o.payload->>'provider_message_id'"
            ),
            {"outbox_id": orphan["id"]},
        ).scalar_one()
        if provider_collision:
            _block("provider collision")

        acceptance_rows = bind.execute(
            sa.text(
                "SELECT ae.id, ae.created_at FROM audit_events ae "
                "JOIN outbox_messages o ON o.id = :outbox_id "
                "JOIN interactions i ON i.id = o.interaction_id "
                "WHERE ae.event_type = 'production_meta_api_accepted' "
                "AND ae.interaction_id = o.interaction_id "
                "AND ae.tenant_id = i.tenant_id "
                "AND ae.correlation_id IS NOT DISTINCT FROM o.correlation_id "
                "AND ae.causation_id IS NOT DISTINCT FROM o.causation_id "
                "AND ae.payload->>'outbox_id' = o.id "
                "AND ae.payload->>'provider_message_id' = "
                "o.payload->>'provider_message_id' ORDER BY ae.id"
            ),
            {"outbox_id": orphan["id"]},
        ).mappings().all()
        if len(acceptance_rows) != 1:
            _block("acceptance evidence is missing or ambiguous")
        accepted_at = acceptance_rows[0]["created_at"]
        if accepted_at is None or accepted_at < orphan["created_at"]:
            _block("acceptance timestamp is invalid")

        graph_rows = bind.execute(
            sa.text(
                "SELECT a.id AS agent_id, a.execution_intent_id AS parent_id, "
                "a.agent_decision_id, a.authorization_source, a.capability_name, "
                "a.idempotency_key AS agent_idempotency_key, a.recipient_reference, "
                "a.status AS agent_status, a.execution_allowed, "
                "a.external_delivery_allowed, a.executed_at, "
                "a.execution_intent_fingerprint, p.state AS parent_state, "
                "p.scope_fingerprint AS parent_fingerprint, p.scope AS parent_scope, "
                "d.interaction_id AS decision_interaction_id, i.tenant_id, "
                "sr.id AS run_id, sr.tenant_id AS run_tenant_id, "
                "sr.agent_execution_intent_id AS run_agent_id, "
                "sr.effect_budget_id AS run_budget_id, "
                "sr.root_correlation_id, sr.status AS run_status, "
                "ec.id AS consumption_id, ec.tenant_id AS consumption_tenant_id, "
                "ec.effect_budget_id AS consumption_budget_id, "
                "ec.execution_lease_id AS consumption_lease_id, "
                "ec.execution_intent_id AS consumption_agent_id, "
                "ec.outbox_message_id AS consumption_outbox_id, "
                "ec.logical_effect_id, ec.idempotency_key AS consumption_key, "
                "ec.target_scope AS consumption_target, ec.state AS consumption_state, "
                "el.id AS lease_id, el.tenant_id AS lease_tenant_id, "
                "el.scenario_run_id AS lease_run_id, "
                "el.effect_budget_id AS lease_budget_id, el.logical_execution_id, "
                "el.claimant_id, el.status AS lease_status, "
                "eb.id AS budget_id, eb.tenant_id AS budget_tenant_id, "
                "eb.scenario_run_id AS budget_run_id, eb.status AS budget_status, "
                "eb.reserved_count, eb.consumed_count, eb.stimulus_limit, "
                "eb.system_effect_limit, bra.id AS bounded_id, "
                "bra.tenant_id AS bounded_tenant_id, "
                "bra.scenario_run_id AS bounded_run_id, "
                "bra.effect_budget_id AS bounded_budget_id, bra.actor_scope, "
                "bra.target_scope AS bounded_target, bra.capability_scope, "
                "bra.effect_scope, bra.max_effects, bra.status AS bounded_status, "
                "hea.id AS authorization_id, hea.execution_intent_id AS hea_parent_id, "
                "hea.execution_intent_fingerprint AS hea_fingerprint, "
                "hea.state AS hea_state, o.idempotency_key AS outbox_key "
                "FROM outbox_messages o "
                "JOIN agent_execution_intents a ON a.id = o.execution_intent_id "
                "JOIN execution_intents p ON p.id = a.execution_intent_id "
                "JOIN agent_decisions d ON d.id = a.agent_decision_id "
                "JOIN interactions i ON i.id = d.interaction_id "
                "JOIN scenario_runs sr ON sr.agent_execution_intent_id = a.id "
                "JOIN effect_consumptions ec ON ec.outbox_message_id = o.id "
                "JOIN execution_leases el ON el.id = ec.execution_lease_id "
                "JOIN effect_budgets eb ON eb.id = ec.effect_budget_id "
                "JOIN bounded_run_authorizations bra "
                "ON bra.scenario_run_id = sr.id "
                "AND bra.effect_budget_id = eb.id "
                "JOIN human_execution_authorizations hea ON hea.id = o.causation_id "
                "WHERE o.id = :outbox_id"
            ),
            {"outbox_id": orphan["id"]},
        ).mappings().all()
        if len(graph_rows) != 1:
            _block("authority graph is incomplete or ambiguous")
        graph = graph_rows[0]
        scope = graph["parent_scope"] if isinstance(graph["parent_scope"], dict) else {}
        target = scope.get("target") if isinstance(scope.get("target"), dict) else {}
        frozen = (
            scope.get("frozen_authority")
            if isinstance(scope.get("frozen_authority"), dict)
            else {}
        )
        expected_effect = f"execution:{graph['agent_id']}"
        exact_graph = (
            graph["tenant_id"] == orphan["tenant_id"]
            and graph["decision_interaction_id"] == orphan["interaction_id"]
            and graph["parent_state"] == "MATERIALIZED"
            and graph["parent_fingerprint"] == graph["execution_intent_fingerprint"]
            and graph["hea_parent_id"] == graph["parent_id"]
            and graph["hea_fingerprint"] == graph["parent_fingerprint"]
            and graph["authorization_source"] == "PRODUCTION_EXECUTION_INTENT"
            and graph["capability_name"] == "conversation.reply"
            and graph["outbox_key"] == expected_effect
            and graph["root_correlation_id"] == orphan["correlation_id"]
            and graph["run_agent_id"] == graph["agent_id"]
            and graph["run_budget_id"] == graph["budget_id"]
            and graph["budget_run_id"] == graph["run_id"]
            and graph["lease_run_id"] == graph["run_id"]
            and graph["lease_budget_id"] == graph["budget_id"]
            and graph["logical_execution_id"] == graph["agent_idempotency_key"]
            and graph["claimant_id"] == graph["agent_id"]
            and graph["consumption_budget_id"] == graph["budget_id"]
            and graph["consumption_lease_id"] == graph["lease_id"]
            and graph["consumption_agent_id"] == graph["agent_id"]
            and graph["consumption_outbox_id"] == orphan["id"]
            and graph["logical_effect_id"] == expected_effect
            and graph["consumption_key"] == expected_effect
            and graph["consumption_target"] == graph["recipient_reference"]
            and graph["bounded_run_id"] == graph["run_id"]
            and graph["bounded_budget_id"] == graph["budget_id"]
            and graph["actor_scope"] == f"human-approval:{graph['authorization_id']}"
            and graph["bounded_target"] == graph["recipient_reference"]
            and graph["capability_scope"] == "conversation.reply"
            and graph["effect_scope"] == "WHATSAPP_TEXT"
            and graph["max_effects"] == 1
            and _scope_fingerprint(scope) == graph["parent_fingerprint"]
            and target.get("tenant") == orphan["tenant_id"]
            and target.get("transport") == "meta_whatsapp"
            and frozen.get("transport") == "meta_whatsapp"
            and frozen.get("operation") == "conversation.reply"
            and frozen.get("capability") == "conversation.reply"
            and frozen.get("outbound_messages") == 1
            and frozen.get("action_count") == 1
            and frozen.get("retries") == 0
            and {
                graph["run_tenant_id"],
                graph["budget_tenant_id"],
                graph["lease_tenant_id"],
                graph["consumption_tenant_id"],
                graph["bounded_tenant_id"],
            }
            == {orphan["tenant_id"]}
        )
        reserved = (
            graph["hea_state"] == "APPROVED"
            and graph["bounded_status"] == "ACTIVE"
            and graph["lease_status"] == "CLAIMED"
            and graph["consumption_state"] == "RESERVED"
            and graph["budget_status"] == "RESERVED"
            and graph["reserved_count"] == 1
            and graph["consumed_count"] == 0
            and graph["agent_status"] == "QUEUED"
            and graph["run_status"] == "RUNNING"
            and graph["execution_allowed"] is False
            and graph["external_delivery_allowed"] is False
            and graph["executed_at"] is None
        )
        if not exact_graph or not reserved:
            _block("authority graph state is not exactly recoverable")

        common = {"accepted_at": accepted_at}
        bind.execute(
            sa.text(
                "UPDATE effect_consumptions SET state = 'CONSUMED', "
                "consumed_at = :accepted_at WHERE id = :id AND state = 'RESERVED'"
            ),
            {**common, "id": graph["consumption_id"]},
        )
        bind.execute(
            sa.text(
                "UPDATE execution_leases SET status = 'CONSUMED', "
                "consumed_at = :accepted_at, version = version + 1, "
                "updated_at = :accepted_at WHERE id = :id AND status = 'CLAIMED'"
            ),
            {**common, "id": graph["lease_id"]},
        )
        bind.execute(
            sa.text(
                "UPDATE effect_budgets SET status = 'CONSUMED', reserved_count = 0, "
                "consumed_count = 1, version = version + 1, updated_at = :accepted_at "
                "WHERE id = :id AND status = 'RESERVED' AND reserved_count = 1 "
                "AND consumed_count = 0"
            ),
            {**common, "id": graph["budget_id"]},
        )
        bind.execute(
            sa.text(
                "UPDATE human_execution_authorizations SET state = 'CONSUMED', "
                "updated_at = :accepted_at WHERE id = :id AND state = 'APPROVED'"
            ),
            {**common, "id": graph["authorization_id"]},
        )
        bind.execute(
            sa.text(
                "UPDATE bounded_run_authorizations SET status = 'CONSUMED', "
                "updated_at = :accepted_at WHERE id = :id AND status = 'ACTIVE'"
            ),
            {**common, "id": graph["bounded_id"]},
        )
        bind.execute(
            sa.text(
                "UPDATE agent_execution_intents SET execution_allowed = true, "
                "external_delivery_allowed = true, executed_at = :accepted_at "
                "WHERE id = :id AND status = 'QUEUED'"
            ),
            {**common, "id": graph["agent_id"]},
        )
        bind.execute(
            sa.text(
                "INSERT INTO meta_delivery_reconciliations "
                "(id, tenant_id, outbox_message_id, provider_message_id, "
                "api_accepted_at, deadline_at, closure_after, next_reconcile_at, "
                "state, evidence_count, created_at, updated_at, operational_state, "
                "failure_count) SELECT :id, i.tenant_id, o.id, "
                "o.payload->>'provider_message_id', :accepted_at, "
                ":accepted_at + make_interval(secs => :window_seconds), "
                ":accepted_at + make_interval(secs => :closure_seconds), "
                ":accepted_at + make_interval(secs => :closure_seconds), "
                "'PENDING', 0, :accepted_at, :accepted_at, 'ACTIVE', 0 "
                "FROM outbox_messages o JOIN interactions i ON i.id = o.interaction_id "
                "WHERE o.id = :outbox_id"
            ),
            {
                "id": _stable_id("metarecon", orphan["id"]),
                "accepted_at": accepted_at,
                "window_seconds": _WINDOW_SECONDS,
                "closure_seconds": _WINDOW_SECONDS + _GRACE_SECONDS,
                "outbox_id": orphan["id"],
            },
        )


def _quarantine_historical_evidence_ambiguity(
    bind: sa.Connection,
    *,
    evidence: sa.RowMapping,
    inbox: sa.RowMapping,
) -> None:
    graph = bind.execute(
        sa.text(
            "SELECT r.state, r.operational_state, r.tenant_id, "
            "o.interaction_id, o.correlation_id, o.causation_id "
            "FROM meta_delivery_reconciliations r "
            "JOIN outbox_messages o ON o.id = r.outbox_message_id "
            "WHERE r.id = :reconciliation_id FOR UPDATE OF r, o"
        ),
        {"reconciliation_id": evidence["reconciliation_id"]},
    ).mappings().one_or_none()
    linked_evidence = bind.execute(
        sa.text(
            "SELECT count(*) FROM meta_callback_evidence "
            "WHERE inbox_id = :inbox_id"
        ),
        {"inbox_id": inbox["id"]},
    ).scalar_one()
    if (
        graph is None
        or graph["state"] != "PENDING"
        or graph["operational_state"] != "ACTIVE"
        or inbox["state"] != "PENDING"
        or inbox["reconciliation_id"] is not None
        or linked_evidence != 0
    ):
        _block("historical evidence inbox association is ambiguous")

    bind.execute(
        sa.text(
            "UPDATE meta_delivery_reconciliations SET "
            "operational_state = 'QUARANTINED', "
            "last_failure_at = transaction_timestamp(), "
            "last_failure_code = :reason, "
            "quarantined_at = transaction_timestamp(), "
            "quarantine_reason = :reason, updated_at = transaction_timestamp() "
            "WHERE id = :reconciliation_id AND state = 'PENDING' "
            "AND operational_state = 'ACTIVE'"
        ),
        {
            "reason": _HISTORICAL_EVIDENCE_AMBIGUITY,
            "reconciliation_id": evidence["reconciliation_id"],
        },
    )
    bind.execute(
        sa.text(
            "UPDATE meta_callback_inbox SET state = 'QUARANTINED', "
            "last_error_code = :reason, quarantined_at = transaction_timestamp(), "
            "updated_at = transaction_timestamp() "
            "WHERE id = :inbox_id AND state = 'PENDING'"
        ),
        {
            "reason": f"PRODUCTION_META_{_HISTORICAL_EVIDENCE_AMBIGUITY}",
            "inbox_id": inbox["id"],
        },
    )
    bind.execute(
        sa.text(
            "INSERT INTO audit_events "
            "(id, tenant_id, interaction_id, event_type, correlation_id, "
            "causation_id, previous_state, next_state, origin, payload, created_at) "
            "VALUES (:id, :tenant_id, :interaction_id, :event_type, "
            ":correlation_id, :causation_id, 'ACTIVE', 'QUARANTINED', :origin, "
            "CAST(:payload AS jsonb), transaction_timestamp())"
        ),
        {
            "id": _stable_id("audit", f"0030:ambiguous:{evidence['id']}"),
            "tenant_id": graph["tenant_id"],
            "interaction_id": graph["interaction_id"],
            "event_type": "production_meta_historical_evidence_quarantined",
            "correlation_id": graph["correlation_id"],
            "causation_id": graph["causation_id"],
            "origin": "meta_callback_reconciler_v2_migration",
            "payload": json.dumps(
                {
                    "evidence_id": evidence["id"],
                    "inbox_id": inbox["id"],
                    "reconciliation_id": evidence["reconciliation_id"],
                    "reason_code": _HISTORICAL_EVIDENCE_AMBIGUITY,
                },
                sort_keys=True,
            ),
        },
    )


def _backfill_historical_evidence(bind: sa.Connection) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT e.id, e.reconciliation_id, e.deduplication_key, "
            "e.provider_status, e.provider_timestamp_raw, e.provider_timestamp, "
            "e.received_at, e.valid, e.errors_present, e.error_fingerprint, "
            "e.created_at, r.provider_message_id, o.id AS outbox_id "
            "FROM meta_callback_evidence e "
            "LEFT JOIN meta_delivery_reconciliations r ON r.id = e.reconciliation_id "
            "LEFT JOIN outbox_messages o ON o.id = r.outbox_message_id "
            "WHERE e.inbox_id IS NULL ORDER BY e.id FOR UPDATE OF e"
        )
    ).mappings().all()
    prepared: list[tuple[sa.RowMapping, sa.RowMapping | None, str]] = []
    for row in rows:
        if not row["provider_message_id"] or not row["outbox_id"]:
            _block("historical evidence has no exact reconciliation")
        exact_scope = bind.execute(
            sa.text(
                "SELECT count(*) FROM outbox_messages o "
                "JOIN meta_delivery_reconciliations r ON r.outbox_message_id = o.id "
                "WHERE o.id = :outbox_id "
                "AND o.destination = 'meta_whatsapp_cloud' "
                "AND o.action_type = 'production_conversation_reply' "
                "AND o.attempt_count = 1 "
                "AND o.payload->>'retry_policy' = 'NONE' "
                "AND o.payload->>'provider_message_id' = r.provider_message_id"
            ),
            {"outbox_id": row["outbox_id"]},
        ).scalar_one()
        if exact_scope != 1:
            _block("historical evidence scope is invalid")
        existing = bind.execute(
            sa.text(
                "SELECT * FROM meta_callback_inbox "
                "WHERE provider_message_id = "
                "(SELECT provider_message_id FROM meta_delivery_reconciliations "
                "WHERE id = :reconciliation_id) "
                "AND deduplication_key = :deduplication_key FOR UPDATE"
            ),
            {
                "reconciliation_id": row["reconciliation_id"],
                "deduplication_key": row["deduplication_key"],
            },
        ).mappings().one_or_none()
        if existing is not None:
            same_event = (
                existing["provider_status"] == row["provider_status"]
                and existing["provider_timestamp_raw"]
                == row["provider_timestamp_raw"]
                and existing["provider_timestamp"] == row["provider_timestamp"]
                and existing["valid"] == row["valid"]
                and existing["errors_present"] == row["errors_present"]
                and existing["error_fingerprint"] == row["error_fingerprint"]
                and (
                    existing["state"] in {"PENDING", "CORRELATED"}
                    or (
                        existing["state"] == "QUARANTINED"
                        and existing["last_error_code"]
                        == "PRODUCTION_META_RECONCILIATION_NOT_FOUND"
                    )
                )
                and existing["reconciliation_id"]
                in {None, row["reconciliation_id"]}
            )
            if not same_event:
                _quarantine_historical_evidence_ambiguity(
                    bind,
                    evidence=row,
                    inbox=existing,
                )
                continue
            inbox_id = existing["id"]
        else:
            inbox_id = _stable_id("metainbox", row["id"])
        prepared.append((row, existing, inbox_id))

    if not prepared:
        return
    bind.execute(
        sa.text(
            "ALTER TABLE meta_callback_evidence "
            "DISABLE TRIGGER meta_callback_evidence_immutable"
        )
    )
    for row, existing, inbox_id in prepared:
        if existing is None:
            bind.execute(
                sa.text(
                    "INSERT INTO meta_callback_inbox "
                    "(id, provider_message_id, deduplication_key, provider_status, "
                    "provider_timestamp_raw, provider_timestamp, received_at, valid, "
                    "errors_present, error_fingerprint, state, reconciliation_id, "
                    "correlation_attempt_count, next_correlation_at, last_error_code, "
                    "correlated_at, quarantined_at, created_at, updated_at) "
                    "SELECT :inbox_id, r.provider_message_id, e.deduplication_key, "
                    "e.provider_status, e.provider_timestamp_raw, e.provider_timestamp, "
                    "e.received_at, e.valid, e.errors_present, e.error_fingerprint, "
                    "'CORRELATED', e.reconciliation_id, 0, e.created_at, NULL, "
                    "e.created_at, NULL, e.created_at, e.created_at "
                    "FROM meta_callback_evidence e "
                    "JOIN meta_delivery_reconciliations r ON r.id = e.reconciliation_id "
                    "WHERE e.id = :evidence_id"
                ),
                {"inbox_id": inbox_id, "evidence_id": row["id"]},
            )
        elif existing["state"] == "PENDING":
            bind.execute(
                sa.text(
                    "UPDATE meta_callback_inbox SET state = 'CORRELATED', "
                    "reconciliation_id = :reconciliation_id, correlated_at = :at, "
                    "last_error_code = NULL, quarantined_at = NULL, updated_at = :at "
                    "WHERE id = :inbox_id AND state = 'PENDING'"
                ),
                {
                    "reconciliation_id": row["reconciliation_id"],
                    "at": row["created_at"],
                    "inbox_id": inbox_id,
                },
            )
        bind.execute(
            sa.text(
                "UPDATE meta_callback_evidence SET inbox_id = :inbox_id "
                "WHERE id = :evidence_id AND inbox_id IS NULL"
            ),
            {"inbox_id": inbox_id, "evidence_id": row["id"]},
        )
    bind.execute(
        sa.text(
            "ALTER TABLE meta_callback_evidence "
            "ENABLE TRIGGER meta_callback_evidence_immutable"
        )
    )


def _recover_exact_quarantined_inbox(bind: sa.Connection) -> None:
    terminal_conflicts = bind.execute(
        sa.text(
            "SELECT count(*) FROM meta_callback_inbox q "
            "JOIN meta_delivery_reconciliations r "
            "ON r.provider_message_id = q.provider_message_id "
            "WHERE q.state = 'QUARANTINED' AND q.valid "
            "AND r.state IN ('PASSED','FAILED')"
        )
    ).scalar_one()
    if terminal_conflicts:
        _block("quarantined inbox conflicts with terminal reconciliation")

    rows = bind.execute(
        sa.text(
            "SELECT q.*, r.id AS exact_reconciliation_id, r.tenant_id, "
            "r.state AS reconciliation_state, "
            "r.operational_state AS reconciliation_operational_state, "
            "r.deadline_at, r.api_accepted_at, o.id AS outbox_id, "
            "o.interaction_id, o.correlation_id, o.causation_id "
            "FROM meta_callback_inbox q "
            "JOIN meta_delivery_reconciliations r "
            "ON r.provider_message_id = q.provider_message_id "
            "JOIN outbox_messages o ON o.id = r.outbox_message_id "
            "JOIN interactions i ON i.id = o.interaction_id "
            "WHERE q.state = 'QUARANTINED' "
            "AND q.last_error_code = 'PRODUCTION_META_RECONCILIATION_NOT_FOUND' "
            "AND (o.destination <> 'meta_whatsapp_cloud' "
            "OR o.action_type <> 'production_conversation_reply' "
            "OR o.attempt_count <> 1 "
            "OR o.payload->>'retry_policy' IS DISTINCT FROM 'NONE' "
            "OR o.payload->>'provider_message_id' "
            "IS DISTINCT FROM r.provider_message_id "
            "OR r.tenant_id <> i.tenant_id)"
        )
    ).mappings().all()
    if rows:
        _block("quarantined inbox exact scope is invalid")

    rows = bind.execute(
        sa.text(
            "SELECT q.*, r.id AS exact_reconciliation_id, r.tenant_id, "
            "r.state AS reconciliation_state, "
            "r.operational_state AS reconciliation_operational_state, "
            "r.deadline_at, r.api_accepted_at, o.id AS outbox_id, "
            "o.interaction_id, o.correlation_id, o.causation_id "
            "FROM meta_callback_inbox q "
            "JOIN meta_delivery_reconciliations r "
            "ON r.provider_message_id = q.provider_message_id "
            "JOIN outbox_messages o ON o.id = r.outbox_message_id "
            "JOIN interactions i ON i.id = o.interaction_id "
            "WHERE q.state = 'QUARANTINED' "
            "AND q.last_error_code = 'PRODUCTION_META_RECONCILIATION_NOT_FOUND' "
            "AND o.destination = 'meta_whatsapp_cloud' "
            "AND o.action_type = 'production_conversation_reply' "
            "AND o.attempt_count = 1 "
            "AND o.payload->>'retry_policy' = 'NONE' "
            "AND o.payload->>'provider_message_id' = r.provider_message_id "
            "AND r.tenant_id = i.tenant_id ORDER BY q.id "
            "FOR UPDATE OF q, r, o"
        )
    ).mappings().all()
    if not rows:
        return

    prepared: list[tuple[sa.RowMapping, sa.RowMapping | None]] = []
    for row in rows:
        if (
            row["reconciliation_state"] != "PENDING"
            or row["reconciliation_operational_state"] != "ACTIVE"
        ):
            _block("quarantined inbox conflicts with terminal reconciliation")
        evidences = bind.execute(
            sa.text(
                "SELECT * FROM meta_callback_evidence "
                "WHERE inbox_id = :inbox_id OR "
                "(reconciliation_id = :reconciliation_id "
                "AND deduplication_key = :deduplication_key) "
                "ORDER BY id FOR UPDATE"
            ),
            {
                "inbox_id": row["id"],
                "reconciliation_id": row["exact_reconciliation_id"],
                "deduplication_key": row["deduplication_key"],
            },
        ).mappings().all()
        if len(evidences) > 1:
            _block("quarantined inbox evidence is ambiguous")
        evidence = evidences[0] if evidences else None
        if evidence is not None and (
            evidence["reconciliation_id"] != row["exact_reconciliation_id"]
            or evidence["inbox_id"] not in {None, row["id"]}
            or evidence["deduplication_key"] != row["deduplication_key"]
            or evidence["provider_status"] != row["provider_status"]
            or evidence["provider_timestamp_raw"] != row["provider_timestamp_raw"]
            or evidence["provider_timestamp"] != row["provider_timestamp"]
            or evidence["valid"] != row["valid"]
            or evidence["errors_present"] != row["errors_present"]
            or evidence["error_fingerprint"] != row["error_fingerprint"]
        ):
            _block("quarantined inbox evidence identity conflicts")
        prepared.append((row, evidence))

    bind.execute(
        sa.text(
            "ALTER TABLE meta_callback_inbox "
            "DISABLE TRIGGER meta_callback_inbox_lifecycle"
        )
    )
    for row, evidence in prepared:
        if evidence is None:
            bind.execute(
                sa.text(
                    "INSERT INTO meta_callback_evidence "
                    "(id, reconciliation_id, inbox_id, deduplication_key, "
                    "provider_status, provider_timestamp_raw, provider_timestamp, "
                    "received_at, valid, admissible, errors_present, "
                    "error_fingerprint, created_at) VALUES "
                    "(:id, :reconciliation_id, :inbox_id, :deduplication_key, "
                    ":provider_status, :provider_timestamp_raw, :provider_timestamp, "
                    ":received_at, :valid, :admissible, :errors_present, "
                    ":error_fingerprint, :created_at)"
                ),
                {
                    "id": _stable_id("metaevidence", row["id"]),
                    "reconciliation_id": row["exact_reconciliation_id"],
                    "inbox_id": row["id"],
                    "deduplication_key": row["deduplication_key"],
                    "provider_status": row["provider_status"],
                    "provider_timestamp_raw": row["provider_timestamp_raw"],
                    "provider_timestamp": row["provider_timestamp"],
                    "received_at": row["received_at"],
                    "valid": row["valid"],
                    "admissible": row["received_at"] <= row["deadline_at"],
                    "errors_present": row["errors_present"],
                    "error_fingerprint": row["error_fingerprint"],
                    "created_at": row["api_accepted_at"],
                },
            )
        elif evidence["inbox_id"] is None:
            bind.execute(
                sa.text(
                    "ALTER TABLE meta_callback_evidence "
                    "DISABLE TRIGGER meta_callback_evidence_immutable"
                )
            )
            bind.execute(
                sa.text(
                    "UPDATE meta_callback_evidence SET inbox_id = :inbox_id "
                    "WHERE id = :evidence_id AND inbox_id IS NULL"
                ),
                {"inbox_id": row["id"], "evidence_id": evidence["id"]},
            )
            bind.execute(
                sa.text(
                    "ALTER TABLE meta_callback_evidence "
                    "ENABLE TRIGGER meta_callback_evidence_immutable"
                )
            )
        bind.execute(
            sa.text(
                "UPDATE meta_callback_inbox SET state = 'CORRELATED', "
                "reconciliation_id = :reconciliation_id, "
                "correlated_at = :correlated_at, last_error_code = NULL, "
                "quarantined_at = NULL, updated_at = :correlated_at "
                "WHERE id = :inbox_id AND state = 'QUARANTINED'"
            ),
            {
                "reconciliation_id": row["exact_reconciliation_id"],
                "correlated_at": row["api_accepted_at"],
                "inbox_id": row["id"],
            },
        )
        bind.execute(
            sa.text(
                "UPDATE meta_delivery_reconciliations "
                "SET next_reconcile_at = LEAST(next_reconcile_at, "
                "transaction_timestamp()), updated_at = transaction_timestamp() "
                "WHERE id = :reconciliation_id AND state = 'PENDING' "
                "AND operational_state = 'ACTIVE'"
            ),
            {"reconciliation_id": row["exact_reconciliation_id"]},
        )
        bind.execute(
            sa.text(
                "INSERT INTO audit_events "
                "(id, tenant_id, interaction_id, event_type, correlation_id, "
                "causation_id, previous_state, next_state, origin, payload, "
                "created_at) VALUES "
                "(:id, :tenant_id, :interaction_id, :event_type, :correlation_id, "
                ":causation_id, 'QUARANTINED', 'CORRELATED', :origin, "
                "CAST(:payload AS jsonb), transaction_timestamp())"
            ),
            {
                "id": _stable_id("audit", f"0030:{row['id']}"),
                "tenant_id": row["tenant_id"],
                "interaction_id": row["interaction_id"],
                "event_type": "production_meta_callback_exact_acceptance_requeued",
                "correlation_id": row["correlation_id"],
                "causation_id": row["causation_id"],
                "origin": "meta_callback_reconciler_v2_migration",
                "payload": json.dumps(
                    {
                        "inbox_id": row["id"],
                        "reconciliation_id": row["exact_reconciliation_id"],
                        "scope": "HISTORICAL_EXACT_API_ACCEPTANCE",
                        "reason_code": "EXACT_RECONCILIATION_ALREADY_PRESENT",
                    },
                    sort_keys=True,
                ),
            },
        )
    bind.execute(
        sa.text(
            "ALTER TABLE meta_callback_inbox "
            "ENABLE TRIGGER meta_callback_inbox_lifecycle"
        )
    )


def _redact_historical_audits(bind: sa.Connection) -> None:
    surfaces = (
        (
            "production_meta_api_accepted",
            "provider_message_id",
            "provider_message_id_hash",
        ),
        (
            "production_conversation_reply_delivered",
            "provider_message_id",
            "provider_message_id_hash",
        ),
        (
            "production_conversation_reply_failed",
            "provider_message_id",
            "provider_message_id_hash",
        ),
        (
            "human_execution_authorization_requested",
            "request_wamid",
            "request_wamid_hash",
        ),
        (
            "human_execution_authorization_revoked",
            "request_wamid",
            "request_wamid_hash",
        ),
        (
            "human_execution_authorization_decided",
            "inbound_wamid",
            "inbound_wamid_hash",
        ),
        (
            "human_execution_auth_control_duplicate",
            "inbound_wamid",
            "inbound_wamid_hash",
        ),
        (
            "human_execution_auth_control_received",
            "inbound_wamid",
            "inbound_wamid_hash",
        ),
        (
            "human_execution_auth_control_rejected",
            "inbound_wamid",
            "inbound_wamid_hash",
        ),
        ("meta_event_normalized", "external_event_id", "external_event_id_hash"),
        ("meta_event_ignored", "id", "provider_message_id_hash"),
    )
    for event_type, raw_key, hash_key in surfaces:
        rows = bind.execute(
            sa.text(
                "SELECT id, payload->>:raw_key AS raw_value FROM audit_events "
                "WHERE event_type = :event_type AND payload ? :raw_key"
            ),
            {"event_type": event_type, "raw_key": raw_key},
        ).mappings().all()
        for row in rows:
            raw_value = row["raw_value"]
            digest = (
                hashlib.sha256(raw_value.encode()).hexdigest()
                if isinstance(raw_value, str) and raw_value
                else None
            )
            bind.execute(
                sa.text(
                    "UPDATE audit_events SET payload = "
                    "(payload - CAST(:raw_key AS text)) || "
                    "jsonb_build_object(CAST(:hash_key AS text), "
                    "CAST(:digest AS text)) "
                    "WHERE id = :id"
                ),
                {
                    "raw_key": raw_key,
                    "hash_key": hash_key,
                    "digest": digest,
                    "id": row["id"],
                },
            )


def _safe_historical_timestamp(value: Any) -> str | None:
    if isinstance(value, bool):
        return "bool:true" if value else "bool:false"
    if isinstance(value, int):
        return str(value)[:64]
    if isinstance(value, str) and value.isdigit():
        return value[:64]
    if value is None:
        return None
    return f"unsupported:{type(value).__name__}"[:64]


def _redact_historical_status_and_shadow_audits(bind: sa.Connection) -> None:
    status_rows = bind.execute(
        sa.text(
            "SELECT id, payload FROM audit_events "
            "WHERE event_type = 'meta_status_received'"
        )
    ).mappings().all()
    for row in status_rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        provider_id = payload.get("id")
        errors = payload.get("errors")
        bind.execute(
            sa.text(
                "UPDATE audit_events SET payload = CAST(:payload AS jsonb) "
                "WHERE id = :id"
            ),
            {
                "id": row["id"],
                "payload": json.dumps(
                    {
                        "provider_message_id_hash": (
                            hashlib.sha256(provider_id.encode()).hexdigest()
                            if isinstance(provider_id, str) and provider_id
                            else payload.get("provider_message_id_hash")
                        ),
                        "status": (
                            payload.get("status")
                            if payload.get("status")
                            in {"sent", "delivered", "read", "failed"}
                            else None
                        ),
                        "timestamp": _safe_historical_timestamp(
                            payload.get("timestamp")
                        ),
                        "errors_present": bool(errors)
                        or bool(payload.get("errors_present")),
                        "error_fingerprint": (
                            hashlib.sha256(
                                json.dumps(
                                    errors,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                    default=str,
                                ).encode()
                            ).hexdigest()
                            if errors
                            else payload.get("error_fingerprint")
                        ),
                    },
                    sort_keys=True,
                ),
            },
        )

    shadow_rows = bind.execute(
        sa.text(
            "SELECT id, payload FROM audit_events "
            "WHERE event_type = 'meta_event_shadow_normalized'"
        )
    ).mappings().all()
    for row in shadow_rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else {}

        def fingerprint(key: str, existing_key: str) -> str | None:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return hashlib.sha256(value.encode()).hexdigest()
            existing = payload.get(existing_key)
            return existing if isinstance(existing, str) else None

        bind.execute(
            sa.text(
                "UPDATE audit_events SET payload = CAST(:payload AS jsonb) "
                "WHERE id = :id"
            ),
            {
                "id": row["id"],
                "payload": json.dumps(
                    {
                        "object": payload.get("object"),
                        "field": payload.get("field"),
                        "waba_id_hash": fingerprint("waba_id", "waba_id_hash"),
                        "phone_number_id_hash": fingerprint(
                            "phone_number_id", "phone_number_id_hash"
                        ),
                        "external_message_id_hash": fingerprint(
                            "external_message_id", "external_message_id_hash"
                        ),
                        "sender_hash": fingerprint("sender", "sender_hash"),
                        "message_type": payload.get("message_type"),
                        "interactive_type": payload.get("interactive_type"),
                        "button_reply_id_hash": fingerprint(
                            "button_reply_id", "button_reply_id_hash"
                        ),
                        "context_id_hash": fingerprint(
                            "context_id", "context_id_hash"
                        ),
                        "interactive_canary_id": payload.get(
                            "interactive_canary_id"
                        ),
                        "interactive_canary_fingerprint": payload.get(
                            "interactive_canary_fingerprint"
                        ),
                        "referenced_outbound_wamid_hash": fingerprint(
                            "referenced_outbound_wamid",
                            "referenced_outbound_wamid_hash",
                        ),
                        "semantic_test_intent": payload.get(
                            "semantic_test_intent"
                        ),
                        "normalization": payload.get("normalization"),
                        "idempotency": payload.get("idempotency"),
                        "dispatch_enabled": payload.get("dispatch_enabled"),
                    },
                    sort_keys=True,
                ),
            },
        )


def _backfill_historical_meta_shadow_events(bind: sa.Connection) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT id, tenant_id, correlation_id, created_at, payload "
            "FROM audit_events WHERE event_type = 'meta_event_shadow_normalized' "
            "AND origin = 'ingress' AND payload ? 'external_message_id' "
            "ORDER BY id"
        )
    ).mappings().all()
    for row in rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        external_id = payload.get("external_message_id")
        if not isinstance(external_id, str) or not external_id or len(external_id) > 180:
            _block("historical Meta shadow event identity is invalid")
        operational_payload = {
            "sender": payload.get("sender"),
            "message_type": payload.get("message_type"),
            "interactive_type": payload.get("interactive_type"),
            "button_reply_id": payload.get("button_reply_id"),
            "context_id": payload.get("context_id"),
            "normalization": "PASS",
            "dispatch_enabled": False,
        }
        payload_hash = hashlib.sha256(
            json.dumps(
                operational_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        existing = bind.execute(
            sa.text(
                "SELECT ie.payload_hash FROM audit_events ae "
                "JOIN inbound_events ie ON ie.tenant_id = ae.tenant_id "
                "AND ie.source = 'meta_whatsapp_shadow' "
                "AND ie.external_event_id = ae.payload->>'external_message_id' "
                "WHERE ae.id = :audit_id"
            ),
            {"audit_id": row["id"]},
        ).scalar_one_or_none()
        if existing is not None:
            if existing != payload_hash:
                _block("historical Meta shadow event identity conflicts")
            continue
        bind.execute(
            sa.text(
                "INSERT INTO inbound_events "
                "(id, tenant_id, source, external_event_id, event_type, payload, "
                "payload_hash, received_at, processed_at, interaction_id, status, "
                "error, correlation_id, lineage_classification, scenario_run_id, "
                "scenario_step_run_id) "
                "SELECT :id, ae.tenant_id, 'meta_whatsapp_shadow', "
                "ae.payload->>'external_message_id', 'shadow_normalized', "
                "CAST(:payload AS jsonb), :payload_hash, ae.created_at, ae.created_at, "
                "NULL, 'SHADOW_NORMALIZED', NULL, "
                "COALESCE(ae.correlation_id, :fallback_correlation_id), "
                "'ORGANIC', NULL, NULL FROM audit_events ae WHERE ae.id = :audit_id"
            ),
            {
                "id": _stable_id("metashadow", f"0030:{row['id']}"),
                "payload": json.dumps(operational_payload, sort_keys=True),
                "payload_hash": payload_hash,
                "fallback_correlation_id": _stable_id(
                    "corr", f"0030:{row['id']}"
                ),
                "audit_id": row["id"],
            },
        )


def _create_human_approval_delivery_evidence(bind: sa.Connection) -> None:
    ambiguous_request_ids = bind.execute(
        sa.text(
            "SELECT count(*) FROM ("
            "SELECT request_wamid FROM human_execution_authorizations "
            "WHERE request_wamid IS NOT NULL GROUP BY request_wamid "
            "HAVING count(*) > 1) duplicate_request_ids"
        )
    ).scalar_one()
    if ambiguous_request_ids:
        _block("human approval provider correlation is ambiguous")
    provider_scope_collisions = bind.execute(
        sa.text(
            "SELECT count(*) FROM human_execution_authorizations hea "
            "JOIN meta_delivery_reconciliations mr "
            "ON mr.provider_message_id = hea.request_wamid "
            "WHERE hea.request_wamid IS NOT NULL"
        )
    ).scalar_one()
    if provider_scope_collisions:
        _block("provider identifier belongs to conflicting scopes")
    op.create_index(
        "uq_human_execution_authorization_request_wamid",
        "human_execution_authorizations",
        ["request_wamid"],
        unique=True,
        postgresql_where=sa.text("request_wamid IS NOT NULL"),
    )
    op.create_table(
        "human_approval_delivery_evidence",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "authorization_id",
            sa.String(64),
            sa.ForeignKey("human_execution_authorizations.id"),
            nullable=False,
        ),
        sa.Column("inbox_id", sa.String(64), sa.ForeignKey("meta_callback_inbox.id")),
        sa.Column("source_audit_id", sa.String(64), sa.ForeignKey("audit_events.id")),
        sa.Column("deduplication_key", sa.String(64), nullable=False),
        sa.Column("provider_status", sa.String(32), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider_status in ('sent','delivered','read','failed')",
            name="ck_human_approval_delivery_evidence_status",
        ),
        sa.CheckConstraint(
            "(inbox_id is null) <> (source_audit_id is null)",
            name="ck_human_approval_delivery_evidence_origin",
        ),
        sa.UniqueConstraint(
            "inbox_id", name="uq_human_approval_delivery_evidence_inbox"
        ),
        sa.UniqueConstraint(
            "source_audit_id",
            name="uq_human_approval_delivery_evidence_source_audit",
        ),
        sa.UniqueConstraint(
            "authorization_id",
            "deduplication_key",
            name="uq_human_approval_delivery_evidence_deduplication",
        ),
    )
    op.create_index(
        "ix_human_approval_delivery_evidence_authorization_received",
        "human_approval_delivery_evidence",
        ["authorization_id", "received_at", "id"],
    )
    op.execute(
        """
        CREATE FUNCTION enforce_human_approval_delivery_evidence_immutable()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'human approval delivery evidence is append-only';
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER human_approval_delivery_evidence_immutable
        BEFORE UPDATE OR DELETE ON human_approval_delivery_evidence
        FOR EACH ROW EXECUTE FUNCTION
        enforce_human_approval_delivery_evidence_immutable()
        """
    )


def _backfill_historical_human_approval_delivery_evidence(
    bind: sa.Connection,
) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT ae.id AS audit_id, ae.created_at, "
            "ae.payload->>'status' AS provider_status, "
            "ae.payload->>'id' AS provider_message_id, "
            "count(hea.id) AS authorization_count, min(hea.id) AS authorization_id "
            "FROM audit_events ae "
            "JOIN human_execution_authorizations hea "
            "ON hea.request_wamid = ae.payload->>'id' "
            "WHERE ae.event_type = 'meta_status_received' "
            "AND ae.origin = 'ingress' "
            "AND ae.payload ? 'id' "
            "AND ae.payload->>'status' IN ('sent','delivered','read','failed') "
            "AND hea.approval_channel = 'meta_whatsapp_interactive' "
            "GROUP BY ae.id, ae.created_at, ae.payload->>'status', ae.payload->>'id' "
            "ORDER BY ae.id"
        )
    ).mappings().all()
    for row in rows:
        if row["authorization_count"] != 1:
            _block("historical human approval delivery evidence is ambiguous")
        inbox_candidates = bind.execute(
            sa.text(
                "SELECT mci.id, mci.deduplication_key FROM audit_events ae "
                "JOIN meta_callback_inbox mci "
                "ON mci.provider_message_id = ae.payload->>'id' "
                "AND mci.provider_status = ae.payload->>'status' "
                "AND mci.received_at = ae.created_at "
                "AND mci.valid IS TRUE "
                "WHERE ae.id = :audit_id ORDER BY mci.id"
            ),
            {"audit_id": row["audit_id"]},
        ).mappings().all()
        if len(inbox_candidates) > 1:
            _block("historical human approval inbox evidence is ambiguous")
        inbox = inbox_candidates[0] if inbox_candidates else None
        bind.execute(
            sa.text(
                "INSERT INTO human_approval_delivery_evidence "
                "(id, authorization_id, inbox_id, source_audit_id, "
                "deduplication_key, provider_status, received_at, created_at) VALUES "
                "(:id, :authorization_id, :inbox_id, :source_audit_id, "
                ":deduplication_key, :provider_status, :received_at, "
                "transaction_timestamp())"
            ),
            {
                "id": _stable_id("headel", f"0030:{row['audit_id']}"),
                "authorization_id": row["authorization_id"],
                "inbox_id": inbox["id"] if inbox is not None else None,
                "source_audit_id": (
                    None if inbox is not None else row["audit_id"]
                ),
                "deduplication_key": (
                    inbox["deduplication_key"]
                    if inbox is not None
                    else hashlib.sha256(
                        f"legacy-audit:{row['audit_id']}".encode()
                    ).hexdigest()
                ),
                "provider_status": row["provider_status"],
                "received_at": row["created_at"],
            },
        )


def _install_claim_schema() -> None:
    op.add_column("meta_callback_inbox", sa.Column("claim_token", sa.String(64)))
    op.add_column("meta_callback_inbox", sa.Column("claimed_by", sa.String(120)))
    op.add_column(
        "meta_callback_inbox", sa.Column("claimed_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "meta_callback_inbox",
        sa.Column("last_requeue_audit_id", sa.String(64)),
    )
    op.create_foreign_key(
        "fk_meta_callback_inbox_last_requeue_audit",
        "meta_callback_inbox",
        "audit_events",
        ["last_requeue_audit_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_meta_callback_inbox_claim_token", "meta_callback_inbox", ["claim_token"]
    )
    op.create_index(
        "ix_meta_callback_inbox_provider_recoverable",
        "meta_callback_inbox",
        ["provider_message_id", "received_at", "id"],
        postgresql_where=sa.text(
            "state = 'PENDING' OR (state = 'QUARANTINED' AND "
            "last_error_code = 'PRODUCTION_META_RECONCILIATION_NOT_FOUND')"
        ),
    )
    op.drop_constraint(
        "ck_meta_callback_inbox_lifecycle",
        "meta_callback_inbox",
        type_="check",
    )
    op.create_check_constraint(
        "ck_meta_callback_inbox_lifecycle",
        "meta_callback_inbox",
        "((state = 'PENDING' and reconciliation_id is null and correlated_at is null "
        "and quarantined_at is null) or "
        "(state = 'CORRELATED' and reconciliation_id is not null and correlated_at is not null "
        "and quarantined_at is null) or "
        "(state = 'QUARANTINED' and reconciliation_id is null and correlated_at is null "
        "and quarantined_at is not null)) and "
        "((claim_token is null and claimed_by is null and claimed_at is null) or "
        "(state = 'PENDING' and claim_token is not null and claimed_by is not null "
        "and claimed_at is not null))",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_meta_callback_inbox_lifecycle()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.provider_message_id IS DISTINCT FROM NEW.provider_message_id
             OR OLD.deduplication_key IS DISTINCT FROM NEW.deduplication_key
             OR OLD.provider_status IS DISTINCT FROM NEW.provider_status
             OR OLD.provider_timestamp_raw IS DISTINCT FROM NEW.provider_timestamp_raw
             OR OLD.provider_timestamp IS DISTINCT FROM NEW.provider_timestamp
             OR OLD.received_at IS DISTINCT FROM NEW.received_at
             OR OLD.valid IS DISTINCT FROM NEW.valid
             OR OLD.errors_present IS DISTINCT FROM NEW.errors_present
             OR OLD.error_fingerprint IS DISTINCT FROM NEW.error_fingerprint
             OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
            RAISE EXCEPTION 'Meta callback inbox evidence identity is immutable';
          END IF;
          IF OLD.last_requeue_audit_id IS DISTINCT FROM NEW.last_requeue_audit_id
             AND NOT (
               OLD.state = 'QUARANTINED'
               AND NEW.state = 'PENDING'
               AND NEW.last_requeue_audit_id IS NOT NULL
               AND NEW.last_requeue_audit_id IS DISTINCT FROM OLD.last_requeue_audit_id
               AND EXISTS (
                 SELECT 1 FROM audit_events ae
                 WHERE ae.id = NEW.last_requeue_audit_id
                   AND ae.event_type = 'production_meta_callback_inbox_requeued'
                   AND ae.previous_state = 'QUARANTINED'
                   AND ae.next_state = 'PENDING'
                   AND ae.origin = 'meta_callback_reconciler_v2'
                   AND ae.payload->>'inbox_id' = OLD.id
               )
             ) THEN
            RAISE EXCEPTION 'Meta callback inbox requeue audit identity is invalid';
          END IF;
          IF OLD.state = 'CORRELATED'
             AND to_jsonb(OLD) IS DISTINCT FROM to_jsonb(NEW) THEN
            RAISE EXCEPTION 'resolved Meta callback inbox row is immutable';
          END IF;
          IF OLD.state = 'QUARANTINED'
             AND NOT (
               OLD.last_error_code = 'PRODUCTION_META_RECONCILIATION_NOT_FOUND'
               AND NEW.state = 'CORRELATED'
               AND NEW.last_error_code IS NULL
               AND NEW.quarantined_at IS NULL
               AND EXISTS (
                 SELECT 1 FROM meta_delivery_reconciliations r
                 WHERE r.id = NEW.reconciliation_id
                   AND r.provider_message_id = OLD.provider_message_id
               )
               AND EXISTS (
                 SELECT 1 FROM meta_callback_evidence e
                 WHERE e.inbox_id = OLD.id
                   AND e.reconciliation_id = NEW.reconciliation_id
                   AND e.deduplication_key = OLD.deduplication_key
               )
               AND EXISTS (
                 SELECT 1 FROM audit_events ae
                 WHERE ae.event_type = 'production_meta_callback_exact_acceptance_requeued'
                   AND ae.previous_state = 'QUARANTINED'
                   AND ae.next_state = 'CORRELATED'
                   AND ae.origin IN (
                     'meta_callback_reconciler_v2',
                     'meta_callback_reconciler_v2_migration'
                   )
                   AND ae.payload->>'inbox_id' = OLD.id
                   AND ae.payload->>'reconciliation_id' = NEW.reconciliation_id
               )
             )
             AND NOT (
               NEW.state = 'PENDING'
               AND NEW.reconciliation_id IS NULL
               AND NEW.correlated_at IS NULL
               AND NEW.quarantined_at IS NULL
               AND NEW.last_error_code IS NULL
               AND NEW.correlation_attempt_count = 0
               AND NEW.claim_token IS NULL
               AND NEW.claimed_by IS NULL
               AND NEW.claimed_at IS NULL
               AND NEW.last_requeue_audit_id IS NOT NULL
               AND NEW.last_requeue_audit_id IS DISTINCT FROM OLD.last_requeue_audit_id
               AND EXISTS (
                 SELECT 1 FROM audit_events ae
                 WHERE ae.id = NEW.last_requeue_audit_id
                   AND ae.event_type = 'production_meta_callback_inbox_requeued'
                   AND ae.previous_state = 'QUARANTINED'
                   AND ae.next_state = 'PENDING'
                   AND ae.origin = 'meta_callback_reconciler_v2'
                   AND ae.payload->>'inbox_id' = OLD.id
               )
             ) THEN
            RAISE EXCEPTION 'quarantined Meta callback inbox requires exact acceptance requeue';
          END IF;
          RETURN NEW;
        END $$
        """
    )


def _install_reconciliation_requeue_guard() -> None:
    op.add_column(
        "meta_delivery_reconciliations",
        sa.Column("last_requeue_audit_id", sa.String(64)),
    )
    op.create_foreign_key(
        "fk_meta_delivery_reconciliation_last_requeue_audit",
        "meta_delivery_reconciliations",
        "audit_events",
        ["last_requeue_audit_id"],
        ["id"],
    )
    op.execute(
        """
        CREATE FUNCTION enforce_meta_delivery_reconciliation_quarantine()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.operational_state = 'QUARANTINED'
             AND to_jsonb(OLD) IS DISTINCT FROM to_jsonb(NEW)
             AND NOT (
               NEW.operational_state = 'ACTIVE'
               AND NEW.state = 'PENDING'
               AND NEW.failure_count = 0
               AND NEW.last_failure_at IS NULL
               AND NEW.last_failure_code IS NULL
               AND NEW.quarantined_at IS NULL
               AND NEW.quarantine_reason IS NULL
               AND NEW.last_requeue_audit_id IS NOT NULL
               AND NEW.last_requeue_audit_id IS DISTINCT FROM OLD.last_requeue_audit_id
               AND EXISTS (
                 SELECT 1 FROM audit_events ae
                 WHERE ae.id = NEW.last_requeue_audit_id
                   AND ae.event_type = 'production_meta_reconciliation_requeued'
                   AND ae.previous_state = 'QUARANTINED'
                   AND ae.next_state = 'ACTIVE'
                   AND ae.origin = 'meta_callback_reconciler_v2'
                   AND ae.payload->>'reconciliation_id' = OLD.id
               )
             ) THEN
            RAISE EXCEPTION 'quarantined Meta reconciliation requires audited requeue';
          END IF;
          IF OLD.last_requeue_audit_id IS DISTINCT FROM NEW.last_requeue_audit_id
             AND NOT (
               OLD.operational_state = 'QUARANTINED'
               AND NEW.operational_state = 'ACTIVE'
             ) THEN
            RAISE EXCEPTION 'Meta reconciliation requeue audit identity is immutable';
          END IF;
          RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER meta_delivery_reconciliation_quarantine
        BEFORE UPDATE ON meta_delivery_reconciliations
        FOR EACH ROW EXECUTE FUNCTION
        enforce_meta_delivery_reconciliation_quarantine()
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("0030 Meta callback remediation requires PostgreSQL")
    bind.execute(
        sa.text(
            "LOCK TABLE meta_delivery_reconciliations IN ACCESS EXCLUSIVE MODE"
        )
    )
    concurrent_writes = False
    try:
        bind.execute(
            sa.text(
                "LOCK TABLE outbox_messages, execution_leases, effect_budgets, "
                "effect_consumptions, agent_execution_intents, scenario_runs, "
                "bounded_run_authorizations, human_execution_authorizations, "
                "execution_intents, agent_decisions, interactions, audit_events, "
                "meta_callback_evidence, meta_callback_inbox, inbound_events "
                "IN SHARE ROW EXCLUSIVE MODE NOWAIT"
            )
        )
    except sa.exc.DBAPIError:
        concurrent_writes = True
    if concurrent_writes:
        _block("concurrent writes require a clean retry")
    _recover_historical_attempts(bind)
    _create_human_approval_delivery_evidence(bind)
    _backfill_historical_evidence(bind)
    _recover_exact_quarantined_inbox(bind)
    _backfill_historical_human_approval_delivery_evidence(bind)
    _backfill_historical_meta_shadow_events(bind)
    _redact_historical_status_and_shadow_audits(bind)
    _redact_historical_audits(bind)
    _install_claim_schema()
    _install_reconciliation_requeue_guard()


def downgrade() -> None:
    bind = op.get_bind()
    active_claims = bind.execute(
        sa.text("SELECT count(*) FROM meta_callback_inbox WHERE claim_token IS NOT NULL")
    ).scalar_one()
    if active_claims:
        raise RuntimeError("0030 downgrade requires zero active inbox claims")
    human_delivery_evidence = bind.execute(
        sa.text("SELECT count(*) FROM human_approval_delivery_evidence")
    ).scalar_one()
    if human_delivery_evidence:
        raise RuntimeError(
            "0030 downgrade requires zero human approval delivery evidence rows"
        )
    reconciliation_requeues = bind.execute(
        sa.text(
            "SELECT count(*) FROM meta_delivery_reconciliations "
            "WHERE last_requeue_audit_id IS NOT NULL"
        )
    ).scalar_one()
    if reconciliation_requeues:
        raise RuntimeError("0030 downgrade requires zero reconciliation requeues")
    op.execute(
        "DROP TRIGGER meta_delivery_reconciliation_quarantine "
        "ON meta_delivery_reconciliations"
    )
    op.execute("DROP FUNCTION enforce_meta_delivery_reconciliation_quarantine()")
    op.drop_constraint(
        "fk_meta_delivery_reconciliation_last_requeue_audit",
        "meta_delivery_reconciliations",
        type_="foreignkey",
    )
    op.drop_column("meta_delivery_reconciliations", "last_requeue_audit_id")
    op.drop_index(
        "ix_meta_callback_inbox_provider_recoverable",
        table_name="meta_callback_inbox",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_meta_callback_inbox_lifecycle()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.provider_message_id IS DISTINCT FROM NEW.provider_message_id
             OR OLD.deduplication_key IS DISTINCT FROM NEW.deduplication_key
             OR OLD.provider_status IS DISTINCT FROM NEW.provider_status
             OR OLD.provider_timestamp_raw IS DISTINCT FROM NEW.provider_timestamp_raw
             OR OLD.provider_timestamp IS DISTINCT FROM NEW.provider_timestamp
             OR OLD.received_at IS DISTINCT FROM NEW.received_at
             OR OLD.valid IS DISTINCT FROM NEW.valid
             OR OLD.errors_present IS DISTINCT FROM NEW.errors_present
             OR OLD.error_fingerprint IS DISTINCT FROM NEW.error_fingerprint
             OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
            RAISE EXCEPTION 'Meta callback inbox evidence identity is immutable';
          END IF;
          IF OLD.state IN ('CORRELATED','QUARANTINED')
             AND to_jsonb(OLD) IS DISTINCT FROM to_jsonb(NEW) THEN
            RAISE EXCEPTION 'resolved Meta callback inbox row is immutable';
          END IF;
          RETURN NEW;
        END $$
        """
    )
    op.drop_constraint(
        "ck_meta_callback_inbox_lifecycle",
        "meta_callback_inbox",
        type_="check",
    )
    op.create_check_constraint(
        "ck_meta_callback_inbox_lifecycle",
        "meta_callback_inbox",
        "(state = 'PENDING' and reconciliation_id is null and correlated_at is null "
        "and quarantined_at is null) or "
        "(state = 'CORRELATED' and reconciliation_id is not null and correlated_at is not null "
        "and quarantined_at is null) or "
        "(state = 'QUARANTINED' and reconciliation_id is null and correlated_at is null "
        "and quarantined_at is not null)",
    )
    op.drop_constraint(
        "uq_meta_callback_inbox_claim_token",
        "meta_callback_inbox",
        type_="unique",
    )
    op.drop_constraint(
        "fk_meta_callback_inbox_last_requeue_audit",
        "meta_callback_inbox",
        type_="foreignkey",
    )
    op.drop_column("meta_callback_inbox", "last_requeue_audit_id")
    for column in ("claimed_at", "claimed_by", "claim_token"):
        op.drop_column("meta_callback_inbox", column)
    op.drop_table("human_approval_delivery_evidence")
    op.execute("DROP FUNCTION enforce_human_approval_delivery_evidence_immutable()")
    op.drop_index(
        "uq_human_execution_authorization_request_wamid",
        table_name="human_execution_authorizations",
    )
