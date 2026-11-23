"""Transactional pilot boundary. No caller receives authority for external effects."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.domain.cognitive_graph import CognitiveGraphSlice, CognitiveRelationKind
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import ConversationMessageRow, TimelineEventRow
from attention_router.infrastructure.context_generations import advance_generations


@dataclass(frozen=True)
class ContextGeneration:
    scope: str
    generation: int
    authority_revision: tuple[str, ...]


class ContextValidity:
    """Hold the existing owner binding lock until caller commit or rollback.

    Only READ COMMITTED PostgreSQL transactions and participating writers are
    supported. Caller supplies an authenticated owner identity, never user text.
    A boundary cannot be reused after its transaction ends.
    """

    def __init__(
        self, session: Session, *, tenant_id: str, binding_id: str,
        owner_human_identity_id: str, source_account: str, start: datetime, now: datetime,
        source_allowed=None,
    ):
        if session.get_bind().dialect.name != "postgresql":
            raise ValueError("CONTEXT_VALIDITY_POSTGRESQL_REQUIRED")
        if session.new or session.dirty or session.deleted:
            raise ValueError("CONTEXT_VALIDITY_REQUIRES_CLEAN_SESSION")
        if session.connection().get_isolation_level() != "READ COMMITTED":
            raise ValueError("CONTEXT_VALIDITY_ISOLATION_UNSUPPORTED")
        if not source_account or source_account == "default" or start >= now:
            raise ValueError("CONTEXT_VALIDITY_SCOPE_INVALID")
        self.session = session
        self._source_allowed = source_allowed
        self.transaction = session.get_transaction()
        self.arguments = MappingProxyType(dict(
            tenant_id=tenant_id, binding_id=binding_id,
            owner_human_identity_id=owner_human_identity_id,
            source_account=source_account, start=start, now=now,
        ))
        from attention_router.application.cognitive_observation import _lock_owner

        self.binding, authority_revision = _lock_owner(
            session, tenant_id, binding_id, owner_human_identity_id,
        )
        self.authority_revision = tuple(authority_revision)
        # Expire clean tenant-local rows loaded before authority was checked.
        for row in list(session.identity_map.values()):
            if row is not self.binding and getattr(row, "tenant_id", None) == tenant_id:
                session.expire(row)

        self.scope = stable_hash(dict(
            tenant_id=tenant_id, binding_id=binding_id, source_account=source_account,
        ))

    def _check(self):
        if self.session.get_transaction() is not self.transaction or not self.transaction.is_active:
            raise ValueError("CONTEXT_VALIDITY_TRANSACTION_ENDED")

    def token(self) -> ContextGeneration:
        self._check()
        generations = (self.binding.binding_metadata or {}).get("context_generations", {})
        generation = generations.get(self.scope, 0)
        if type(generation) is not int or generation < 0:
            raise ValueError("CONTEXT_GENERATION_LEDGER_INVALID")
        return ContextGeneration(self.scope, generation, self.authority_revision)

    def validate(self, token: ContextGeneration) -> None:
        if token != self.token():
            raise ValueError("CONTEXT_VALIDITY_STALE_GENERATION")

    def _permitted_sources(self, message_ids, *, required_layers=()):
        ids = {message_id for message_id in message_ids if isinstance(message_id, str)}
        if self._source_allowed is None or not ids:
            return ids
        return {message.id for message in self.lock_permitted_source_rows(ids, required_layers=required_layers)}

    def lock_permitted_source_rows(self, message_ids, *, required_layers=()):
        """Return scoped consented rows held for share in this transaction."""
        self._check()
        ids = {message_id for message_id in message_ids if isinstance(message_id, str)}
        if self._source_allowed is None:
            raise ValueError("CONTEXT_CONSENT_REQUIRED")
        if not ids:
            return ()
        messages = self.session.scalars(select(ConversationMessageRow).where(
            ConversationMessageRow.id.in_(ids),
            ConversationMessageRow.tenant_id == self.arguments["tenant_id"],
            ConversationMessageRow.source_account == self.arguments["source_account"],
        ).order_by(ConversationMessageRow.id).with_for_update(read=True)
          .execution_options(populate_existing=True)).all()
        required = {"required_layers": required_layers} if required_layers else {}
        return tuple(
            message for message in messages
            if self._source_allowed(
                message, owner_id=self.arguments["owner_human_identity_id"], now=self.arguments["now"],
                **required,
            )
        )

    def observe(self, message_ids):
        self._check()
        from attention_router.application.cognitive_observation import observe_attachment_context

        message_ids = tuple(message_ids)
        if len(message_ids) > 100 or set(message_ids) != self._permitted_sources(message_ids):
            raise ValueError("CONTEXT_CONSENT_SOURCE_DENIED")
        with self.session.begin_nested():
            result = observe_attachment_context(
                self.session, message_ids=message_ids,
                observations_only=self._source_allowed is not None, **self.arguments,
            )
            if result.revised_episodes:
                self._advance("CONTEXT_SUPPORT_REVISED")
            elif result.new_links or result.new_candidates or result.new_memberships:
                self._advance("CONTEXT_DERIVED")
        return result

    def graph(self):
        return self._graph()

    def _graph(self, *, batch_sources=True):
        self._check()
        from attention_router.application.cognitive_graph import build_cognitive_graph_slice

        events = self.session.scalars(
            select(TimelineEventRow)
            .join(ConversationMessageRow, TimelineEventRow.event_ref["conversation_message_id"].as_string()
                  == ConversationMessageRow.id)
            .where(
                TimelineEventRow.tenant_id == self.arguments["tenant_id"],
                TimelineEventRow.event_type == "ATTACHMENT_OBSERVED",
                TimelineEventRow.event_ref["owner_binding_id"].as_string() == self.binding.id,
                ConversationMessageRow.tenant_id == self.arguments["tenant_id"],
                ConversationMessageRow.source_account == self.arguments["source_account"],
            ).order_by(TimelineEventRow.occurred_at.desc(), TimelineEventRow.id).limit(200)
            .execution_options(populate_existing=True)
        ).all()
        from attention_router.application.cognitive_observation import (
            _locked_observation_sources,
            observation_event_is_current,
        )

        permitted = self._permitted_sources(event.event_ref.get("conversation_message_id") for event in events)
        permitted_events = [
            event for event in events
            if tuple((event.metadata_json or {}).get("owner_authority_revision") or ()) == self.authority_revision
            and event.event_ref.get("conversation_message_id") in permitted
        ]
        sources = (
            _locked_observation_sources(self.session, self.arguments["tenant_id"], permitted_events)
            if batch_sources else None
        )
        start = self.arguments["start"]
        if batch_sources:
            start = max(start, datetime(1970, 1, 1, tzinfo=UTC))
        event_ids = tuple(event.id for event in permitted_events if observation_event_is_current(
            self.session, event, source_account=self.arguments["source_account"],
            start=start, now=self.arguments["now"], lock_sources=True,
            _source_rows=sources,
        ))
        if not event_ids:
            return self.token(), CognitiveGraphSlice(
                tenant_id=self.arguments["tenant_id"], nodes=(), edges=(), generated_at=self.arguments["now"],
            )
        graph = build_cognitive_graph_slice(
            self.session, self.arguments["tenant_id"], timeline_lineage="ORGANIC",
            timeline_event_ids=event_ids, now=self.arguments["now"],
            timeline_projection_only=self._source_allowed is not None,
            _locked_observation_ids=frozenset(event_ids) if batch_sources else frozenset(),
        )
        current_nodes = {node.node_id for node in graph.nodes
                         if node.source_type == "TIMELINE_EVENT" and node.source_id in event_ids}
        episode_scope = stable_hash({
            "binding_id": self.binding.id, "source_account": self.arguments["source_account"],
        })[:32]
        current_episodes = {
            node.node_id for node in graph.nodes
            if self._source_allowed is None and node.source_type == "SEMANTIC_EPISODE"
            and node.attributes.get("episode_type") == f"ATTACHMENT_OBSERVED:{episode_scope}"
        }
        edges = tuple(edge for edge in graph.edges if (
            edge.source_node_id in current_nodes
            and edge.relation_kind in {CognitiveRelationKind.EVENT_ACTOR, CognitiveRelationKind.EVENT_RESOURCE}
        ) or (
            edge.source_node_id in current_episodes and edge.target_node_id in current_nodes
            and edge.relation_kind == CognitiveRelationKind.EPISODE_MEMBER
        ))
        visible = current_nodes | {node for edge in edges for node in (edge.source_node_id, edge.target_node_id)}
        nodes = tuple(node for node in graph.nodes if node.node_id in visible)
        return self.token(), replace(graph, nodes=nodes, edges=edges)

    def compile(self, query: str):
        from attention_router.application.context_compiler import compile_graph_context

        token, graph = self.graph()
        packet = compile_graph_context(
            self.session, graph.tenant_id, query,
            now=self.arguments["now"], projected_graph=graph,
        )
        return token, packet

    def consume_compiled(self, token: ContextGeneration, packet):
        self.validate(token)
        if packet.tenant_id != self.arguments["tenant_id"]:
            raise ValueError("CONTEXT_VALIDITY_PACKET_SCOPE")
        # Return for internal read-only consumption under this transaction.
        return self.compile(packet.query)[1]

    def admit(self, candidate_id: str, decision_ref: str):
        self._check()
        from attention_router.application.candidate_insights import admit_candidate_insight
        from attention_router.infrastructure.candidate_insight_models import CandidateInsightRow

        candidate = self.session.get(CandidateInsightRow, candidate_id, populate_existing=True)
        if candidate is None or candidate.tenant_id != self.arguments["tenant_id"]:
            raise ValueError("CONTEXT_VALIDITY_CANDIDATE_SCOPE")
        from attention_router.infrastructure.candidate_insight_models import CandidateInsightEvidenceRow

        from attention_router.application.cognitive_observation import observation_event_is_current

        supports = self.session.scalars(select(CandidateInsightEvidenceRow).where(
            CandidateInsightEvidenceRow.candidate_id == candidate_id,
            CandidateInsightEvidenceRow.tenant_id == self.arguments["tenant_id"],
            CandidateInsightEvidenceRow.evidence_role == "SUPPORT",
        )).all()
        if not supports:
            raise ValueError("CONTEXT_VALIDITY_SUPPORT_MISSING")
        for support in supports:
            event = self.session.get(TimelineEventRow, support.source_ref)
            if (
                support.evidence_type != "TIMELINE_EVENT" or event is None
                or event.tenant_id != self.arguments["tenant_id"]
                or event.event_ref.get("owner_binding_id") != self.binding.id

            ):
                raise ValueError("CONTEXT_VALIDITY_SUPPORT_SCOPE")
            message = self.session.get(ConversationMessageRow, event.event_ref.get("conversation_message_id"))
            if message is None or message.source_account != self.arguments["source_account"]:
                raise ValueError("CONTEXT_VALIDITY_SUPPORT_SCOPE")
            if tuple((event.metadata_json or {}).get("owner_authority_revision") or ()) != self.authority_revision:
                raise ValueError("CONTEXT_VALIDITY_EVIDENCE_REVOKED")
            if message.id not in self._permitted_sources((message.id,)):
                raise ValueError("CONTEXT_CONSENT_SOURCE_DENIED")
            if not observation_event_is_current(
                self.session, event, source_account=self.arguments["source_account"],
                start=self.arguments["start"], now=self.arguments["now"], lock_sources=True,
            ):
                raise ValueError("CONTEXT_VALIDITY_EVIDENCE_REVOKED")
        return admit_candidate_insight(
            self.session, candidate_id=candidate_id, decision_ref=decision_ref,
            decision_actor_key=self.binding.actor_key, now=self.arguments["now"],
        )

    def _advance(self, reason: str, message_id: str | None = None):
        self._check()
        advance_generations(
            self.session, self.binding, [self.scope], reason, message_id,
        )

    def revoke_artifact(self, artifact_id: str):
        from attention_router.application.platform.artifacts import withdraw_context_artifact

        self._check()
        with self.session.begin_nested():
            return withdraw_context_artifact(self, artifact_id)

    def revoke_receipt(self, receipt_id: str, *, expires_at: datetime | None = None):
        from attention_router.application.platform.artifacts import withdraw_context_receipt

        self._check()
        with self.session.begin_nested():
            return withdraw_context_receipt(self, receipt_id, expires_at=expires_at)

    def revoke_message(self, message_id: str):
        self._check()
        with self.session.begin_nested():
            return self._revoke_message(message_id)

    def _revoke_message(self, message_id: str):
        message = self.session.get(
            ConversationMessageRow, message_id, populate_existing=True, with_for_update=True,
        )
        if (
            message is None or message.tenant_id != self.arguments["tenant_id"]
            or message.source_account != self.arguments["source_account"]
            or message.source != "wwebjs"
        ):
            raise ValueError("CONTEXT_VALIDITY_MESSAGE_SCOPE")
        from attention_router.application.cognitive_observation import _reconcile_observation_support

        if (message.metadata_json or {}).get("context_evidence_withdrawn") is not True:
            message.searchable = False
            message.metadata_json = {**(message.metadata_json or {}), "context_evidence_withdrawn": True}
            self._advance("EXPLICIT_PILOT_TECHNICAL_WITHDRAWAL", message_id)
        _reconcile_observation_support(
            self.session, tenant_id=self.arguments["tenant_id"],
            binding_id=self.arguments["binding_id"], source_account=self.arguments["source_account"],
            start=self.arguments["start"], stamp=self.arguments["now"],
        )
        return self.token()
