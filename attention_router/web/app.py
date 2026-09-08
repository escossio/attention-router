import hmac
import os
from typing import Annotated
from typing import Any

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from attention_router.application import agent_builder
from attention_router.application import services
from attention_router.application.decision_pipeline import decision_to_dict
from attention_router.application import response_review
from attention_router.application import execution
from attention_router.application import autonomy
from attention_router.application import memory
from attention_router.application.platform import devices as platform_devices
from attention_router.application.platform.registry import matrix_status
from attention_router.api.v1.contracts import DeviceBindingRequest, MatrixCapabilityView
from attention_router.core.devices import (
    DeviceCapabilityAnnouncement,
    DeviceHeartbeat,
    DeviceRegistrationRequest,
    DeviceRegistrationResponse,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.config import settings
from attention_router.adapters.synthetic_inbound import SyntheticInboundAdapter
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.repository import (
    activate_policy_version,
    actor_binding_to_dict,
    create_policy,
    deactivate_policy,
    get_actor_binding,
    interaction_to_dict,
    list_actor_bindings,
    list_policy_versions,
    list_policies,
    seed_policies,
    set_actor_binding_active,
    update_policy,
    upsert_actor_binding,
)
from attention_router.infrastructure.models import AgentDecisionRow, AgentExecutionIntentRow, AutonomyEvaluationRow, InteractionRow
from attention_router.platform.api import build_operations_router, read_operations_snapshot
from attention_router.platform.governance_api import build_governance_router


app = FastAPI(title="Attention Router", version="0.1.0")
app.mount("/static", StaticFiles(directory="attention_router/web/static"), name="static")
templates = Jinja2Templates(directory="attention_router/web/templates")
synthetic_adapter = SyntheticInboundAdapter()


def get_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class InteractionCreate(BaseModel):
    source: str = "simulator"
    external_event_id: str | None = None
    event_type: str = Field(pattern="^(message|call)$")
    contact_id: str
    contact_name: str
    relationship_category: str
    active_context: str | None = None
    inbound_text: str


class PolicyPatch(BaseModel):
    initial_wait_seconds: int | None = None
    tone: str | None = None
    escalation_steps: list[str] | None = None


class PolicyCreate(BaseModel):
    identifier: str
    config: dict[str, Any]


class ActorBindingCreate(BaseModel):
    source: str
    external_actor_id: str
    actor_key: str
    display_name: str | None = None
    actor_category: str = "unknown"
    active_context: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True


class ActorBindingPatch(BaseModel):
    actor_key: str | None = None
    display_name: str | None = None
    actor_category: str | None = None
    active_context: str | None = None
    metadata: dict[str, Any] | None = None
    is_active: bool | None = None


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=100)


class ConfigurationAnswer(BaseModel):
    text: str


class ConfigurationChat(BaseModel):
    text: str
    interviewer: str | None = None
    fallback_to_deterministic: bool = True


class ConfigurationCorrection(BaseModel):
    stage: str
    text: str


class ResponseReviewEdit(BaseModel):
    text: str


class ResponseReviewReject(BaseModel):
    reason: str | None = None


def require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
    if not settings.admin_auth_enabled:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="admin credential required")
    token = authorization.removeprefix("Bearer ").strip()
    if not settings.admin_token or not hmac.compare_digest(token, settings.admin_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin credential invalid")


app.include_router(build_operations_router(get_session=get_session, require_admin=require_admin))
app.include_router(build_governance_router(get_session=get_session, require_admin=require_admin))


@app.on_event("startup")
def startup() -> None:
    with SessionLocal() as session:
        seed_policies(session)
        session.commit()


@app.get("/health/live", tags=["health"])
def live() -> dict[str, str]:
    return {
        "status": "live",
        "app_env": settings.app_env,
        "version": "0.1.0",
        "runtime_head": os.environ.get("RUNTIME_HEAD", "unknown"),
    }


@app.get("/health/ready", tags=["health"])
def ready(session: Session = Depends(get_session)) -> dict[str, str]:
    session.execute(text("select 1"))
    revision = session.execute(text("select version_num from alembic_version limit 1")).scalar_one_or_none()
    return {
        "status": "ready",
        "app_env": settings.app_env,
        "schema_revision": revision or "unknown",
        "runtime_head": os.environ.get("RUNTIME_HEAD", "unknown"),
    }


@app.post("/api/v1/private/memory/search", tags=["private-memory"], dependencies=[Depends(require_admin)])
def private_memory_search(payload: MemorySearchRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    return {"memory": memory.search_memory(session, payload.query, payload.limit), "archive": memory.search_archive(session, payload.query, payload.limit)}


@app.get("/api/v1/private/memory/claims/{claim_id}/evidence", tags=["private-memory"], dependencies=[Depends(require_admin)])
def private_memory_evidence(claim_id: str, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return memory.get_memory_evidence(session, claim_id)


@app.get("/api/v1/private/memory/actors/{actor_id}", tags=["private-memory"], dependencies=[Depends(require_admin)])
def private_memory_actor(actor_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    return memory.memory_context(session, actor_id)


@app.get(
    "/api/v1/admin/platform/matrix",
    tags=["platform-matrix"],
    dependencies=[Depends(require_admin)],
    response_model=list[MatrixCapabilityView],
)
def api_platform_matrix(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return matrix_status(session, tenant_id=DEFAULT_TENANT_ID)


@app.post(
    "/api/v1/admin/devices/register",
    tags=["platform-devices"],
    dependencies=[Depends(require_admin)],
    response_model=DeviceRegistrationResponse,
)
def api_register_device(
    payload: DeviceRegistrationRequest,
    session: Session = Depends(get_session),
) -> DeviceRegistrationResponse:
    return platform_devices.register_device(session, tenant_id=DEFAULT_TENANT_ID, request=payload)


@app.post(
    "/api/v1/admin/devices/{device_id}/bindings",
    tags=["platform-devices"],
    dependencies=[Depends(require_admin)],
)
def api_bind_device(
    device_id: str,
    payload: DeviceBindingRequest,
    session: Session = Depends(get_session),
) -> dict[str, str | None]:
    row = platform_devices.bind_device(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        device_id=device_id,
        actor_key=payload.actor_key,
        resource_id=payload.resource_id,
    )
    return {"binding_id": row.id, "status": row.status}


@app.post(
    "/api/v1/admin/devices/{device_id}/capabilities",
    tags=["platform-devices"],
    dependencies=[Depends(require_admin)],
)
def api_announce_device_capabilities(
    device_id: str,
    payload: DeviceCapabilityAnnouncement,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rows = platform_devices.announce_capabilities(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        device_id=device_id,
        announcement=payload,
    )
    return {"device_id": device_id, "announced_count": len(rows), "authorized": False}


@app.post(
    "/api/v1/admin/devices/{device_id}/heartbeat",
    tags=["platform-devices"],
    dependencies=[Depends(require_admin)],
)
def api_device_heartbeat(
    device_id: str,
    payload: DeviceHeartbeat,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    row = platform_devices.record_heartbeat(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        device_id=device_id,
        heartbeat=payload,
    )
    return {"device_id": device_id, "health": row.health, "observed_at": row.observed_at}


@app.get("/api/v1/admin/policies", tags=["admin"], dependencies=[Depends(require_admin)])
def api_policies(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [policy.__dict__ for policy in list_policies(session)]


@app.patch("/api/v1/admin/policies/{identifier}", tags=["admin"], dependencies=[Depends(require_admin)])
def api_update_policy(identifier: str, patch: PolicyPatch, session: Session = Depends(get_session)) -> dict:
    try:
        return update_policy(session, identifier, patch.model_dump(exclude_none=True)).__dict__
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="policy not found") from exc


@app.post("/api/v1/admin/policies", tags=["admin"], dependencies=[Depends(require_admin)])
def api_create_policy(payload: PolicyCreate, session: Session = Depends(get_session)) -> dict:
    try:
        return create_policy(session, payload.identifier, payload.config).__dict__
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/admin/policies/{identifier}/versions", tags=["admin"], dependencies=[Depends(require_admin)])
def api_policy_versions(identifier: str, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return list_policy_versions(session, identifier)


@app.post("/api/v1/admin/policies/{identifier}/versions/{version_id}/activate", tags=["admin"], dependencies=[Depends(require_admin)])
def api_activate_policy_version(identifier: str, version_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        return activate_policy_version(session, identifier, version_id).__dict__
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="policy version not found") from exc


@app.post("/api/v1/admin/policies/{identifier}/deactivate", tags=["admin"], dependencies=[Depends(require_admin)])
def api_deactivate_policy(identifier: str, session: Session = Depends(get_session)) -> dict:
    try:
        deactivate_policy(session, identifier)
        return {"status": "deactivated", "identifier": identifier}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="policy not found") from exc


@app.post("/api/v1/admin/interactions", tags=["admin"], dependencies=[Depends(require_admin)])
def api_create_interaction(payload: InteractionCreate, session: Session = Depends(get_session)) -> dict:
    data = payload.model_dump()
    external_event_id = data.pop("external_event_id") or services.new_id()
    source = data.pop("source")
    try:
        return services.receive_inbound_event(session, source=source, external_event_id=external_event_id, **data)
    except services.DuplicatePayloadConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/admin/interactions/{interaction_id}", tags=["admin"], dependencies=[Depends(require_admin)])
def api_get_interaction(interaction_id: str, session: Session = Depends(get_session)) -> dict:
    row = session.get(InteractionRow, interaction_id)
    if row is None:
        raise HTTPException(status_code=404, detail="interaction not found")
    return interaction_to_dict(session, row)


@app.post("/api/v1/admin/interactions/{interaction_id}/human-reply", tags=["admin"], dependencies=[Depends(require_admin)])
def api_human_reply(interaction_id: str, payload: dict[str, str], session: Session = Depends(get_session)) -> dict:
    return services.human_reply(session, interaction_id, payload.get("text", "synthetic human reply"))


@app.post("/api/v1/admin/agent-builder/sessions", tags=["admin"], dependencies=[Depends(require_admin)])
def api_create_configuration_session(session: Session = Depends(get_session)) -> dict[str, Any]:
    row = agent_builder.create_configuration_session(session)
    question = agent_builder.get_next_question(session, row.id)
    return {
        "id": row.id,
        "status": row.status,
        "current_stage": row.current_stage,
        "blueprint_id": row.blueprint_id,
        "next_question": question.model_dump() if question else None,
    }


@app.get("/api/v1/admin/agent-builder/sessions/{session_id}", tags=["admin"], dependencies=[Depends(require_admin)])
def api_get_configuration_session(session_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        row = agent_builder.get_configuration_session(session, session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="configuration session not found") from exc
    return {
        "id": row.id,
        "status": row.status,
        "current_stage": row.current_stage,
        "blueprint_id": row.blueprint_id,
        "answers": row.answers,
        "review": agent_builder.render_review(row),
    }


@app.get(
    "/api/v1/admin/agent-builder/sessions/{session_id}/next-question",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)
def api_next_configuration_question(session_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        question = agent_builder.get_next_question(session, session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="configuration session not found") from exc
    return {"question": question.model_dump() if question else None}


@app.post("/api/v1/admin/agent-builder/sessions/{session_id}/answers", tags=["admin"], dependencies=[Depends(require_admin)])
def api_record_configuration_answer(
    session_id: str, payload: ConfigurationAnswer, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        row = agent_builder.record_configuration_answer(session, session_id, payload.text)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="configuration session not found") from exc
    question = agent_builder.get_next_question(session, session_id)
    return {
        "id": row.id,
        "status": row.status,
        "current_stage": row.current_stage,
        "next_question": question.model_dump() if question else None,
    }


@app.post("/api/v1/admin/agent-builder/sessions/{session_id}/chat", tags=["admin"], dependencies=[Depends(require_admin)])
def api_chat_configuration_session(
    session_id: str, payload: ConfigurationChat, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        return agent_builder.chat_configuration_session(
            session,
            session_id,
            payload.text,
            provider=payload.interviewer,
            fallback_to_deterministic=payload.fallback_to_deterministic,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="configuration session not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v1/admin/agent-builder/sessions/{session_id}/corrections", tags=["admin"], dependencies=[Depends(require_admin)])
def api_correct_configuration_answer(
    session_id: str, payload: ConfigurationCorrection, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        row = agent_builder.correct_configuration_answer(session, session_id, payload.stage, payload.text)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="configuration session not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"id": row.id, "status": row.status, "current_stage": row.current_stage, "answers": row.answers}


@app.get("/api/v1/admin/agent-builder/sessions/{session_id}/review", tags=["admin"], dependencies=[Depends(require_admin)])
def api_review_configuration_session(session_id: str, session: Session = Depends(get_session)) -> dict[str, str]:
    try:
        row = agent_builder.get_configuration_session(session, session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="configuration session not found") from exc
    return {"review": agent_builder.render_review(row)}


@app.post("/api/v1/admin/agent-builder/sessions/{session_id}/build", tags=["admin"], dependencies=[Depends(require_admin)])
def api_build_configuration_session(session_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        version = agent_builder.build_blueprint_from_session(session, session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="configuration session not found") from exc
    return {
        "blueprint_id": version.blueprint_id,
        "version_id": version.id,
        "version": version.version,
        "spec": version.spec,
    }


@app.get("/api/v1/admin/agent-blueprints/{blueprint_id}", tags=["admin"], dependencies=[Depends(require_admin)])
def api_get_agent_blueprint(blueprint_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        return agent_builder.get_agent_blueprint(session, blueprint_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="agent blueprint not found") from exc


@app.post("/api/v1/admin/actions/{action_id}/executed", tags=["admin"], dependencies=[Depends(require_admin)])
def api_action_executed(action_id: str, session: Session = Depends(get_session)) -> dict:
    return services.action_executed(session, action_id)


@app.post("/api/v1/admin/actions/{action_id}/failed", tags=["admin"], dependencies=[Depends(require_admin)])
def api_action_failed(action_id: str, session: Session = Depends(get_session)) -> dict:
    return services.action_failed(session, action_id)


@app.post("/api/v1/admin/actions/{action_id}/acknowledge", tags=["admin"], dependencies=[Depends(require_admin)])
def api_action_ack(action_id: str, payload: dict[str, str] | None = None, session: Session = Depends(get_session)) -> dict:
    return services.acknowledge_action(session, action_id, (payload or {}).get("source", "simulator"))


@app.post("/api/v1/admin/test-clock/advance-next", tags=["admin"], dependencies=[Depends(require_admin)])
def api_advance_clock(session: Session = Depends(get_session)) -> dict:
    result = services.force_next_timer(session)
    return result or {"processed": 0}


@app.get("/api/v1/admin/actors/bindings", tags=["admin"], dependencies=[Depends(require_admin)])
def api_list_actor_bindings(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [actor_binding_to_dict(row) for row in list_actor_bindings(session)]


@app.post("/api/v1/admin/actors/bindings", tags=["admin"], dependencies=[Depends(require_admin)])
def api_create_actor_binding(payload: ActorBindingCreate, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = upsert_actor_binding(session, **payload.model_dump())
    return actor_binding_to_dict(row)


@app.get("/api/v1/admin/actors/bindings/{binding_id}", tags=["admin"], dependencies=[Depends(require_admin)])
def api_get_actor_binding(binding_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        return actor_binding_to_dict(get_actor_binding(session, binding_id), reveal_external=True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="actor binding not found") from exc


@app.patch("/api/v1/admin/actors/bindings/{binding_id}", tags=["admin"], dependencies=[Depends(require_admin)])
def api_update_actor_binding(
    binding_id: str, payload: ActorBindingPatch, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        current = get_actor_binding(session, binding_id)
        changes = payload.model_dump(exclude_none=True)
        row = upsert_actor_binding(
            session,
            source=current.source,
            external_actor_id=current.external_actor_id,
            actor_key=changes.get("actor_key", current.actor_key),
            display_name=changes.get("display_name", current.display_name),
            actor_category=changes.get("actor_category", current.actor_category),
            active_context=changes.get("active_context", current.active_context),
            metadata=changes.get("metadata", current.binding_metadata),
            is_active=changes.get("is_active", current.is_active),
        )
        return actor_binding_to_dict(row)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="actor binding not found") from exc


@app.post("/api/v1/admin/actors/bindings/{binding_id}/activate", tags=["admin"], dependencies=[Depends(require_admin)])
def api_activate_actor_binding(binding_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        return actor_binding_to_dict(set_actor_binding_active(session, binding_id, True))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="actor binding not found") from exc


@app.post("/api/v1/admin/actors/bindings/{binding_id}/deactivate", tags=["admin"], dependencies=[Depends(require_admin)])
def api_deactivate_actor_binding(binding_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        return actor_binding_to_dict(set_actor_binding_active(session, binding_id, False))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="actor binding not found") from exc


@app.post("/api/v1/ingress/synthetic/events", tags=["ingress"])
def synthetic_ingress(payload: dict[str, Any], session: Session = Depends(get_session)) -> dict:
    try:
        normalized = synthetic_adapter.normalize(payload)
        result = services.receive_normalized_inbound_event(session, normalized)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    except services.DuplicatePayloadConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "status": "accepted",
        "schema_version": normalized.schema_version,
        "source": normalized.source,
        "external_event_id": normalized.external_event_id,
        "correlation_id": result["correlation_id"],
        "interaction_id": result["id"],
        "state": result["state"],
    }


@app.get("/api/v1/private/agent-decisions", tags=["private"], dependencies=[Depends(require_admin)])
def api_list_agent_decisions(
    event_id: str | None = None,
    interaction_id: str | None = None,
    decision_type: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    query = select(AgentDecisionRow).order_by(AgentDecisionRow.created_at.desc()).limit(100)
    if event_id:
        query = query.where(AgentDecisionRow.event_id == event_id)
    if interaction_id:
        query = query.where(AgentDecisionRow.interaction_id == interaction_id)
    if decision_type:
        query = query.where(AgentDecisionRow.decision_type == decision_type)
    return {"items": [decision_to_dict(row) for row in session.scalars(query).all()]}


@app.get("/api/v1/private/agent-decisions/{decision_id}", tags=["private"], dependencies=[Depends(require_admin)])
def api_get_agent_decision(decision_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = session.get(AgentDecisionRow, decision_id)
    if row is None:
        raise HTTPException(status_code=404, detail="agent decision not found")
    return decision_to_dict(row)


@app.get("/api/v1/private/autonomy-evaluations", tags=["private"], dependencies=[Depends(require_admin)])
def api_list_autonomy_evaluations(
    agent_decision_id: str | None = None,
    effective_mode: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    query = select(AutonomyEvaluationRow).order_by(AutonomyEvaluationRow.created_at.desc()).limit(100)
    if agent_decision_id:
        query = query.where(AutonomyEvaluationRow.agent_decision_id == agent_decision_id)
    if effective_mode:
        query = query.where(AutonomyEvaluationRow.effective_mode == effective_mode)
    return {"items": [autonomy.evaluation_to_dict(row) for row in session.scalars(query).all()]}


@app.get("/api/v1/private/autonomy-evaluations/{evaluation_id}", tags=["private"], dependencies=[Depends(require_admin)])
def api_get_autonomy_evaluation(evaluation_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = session.get(AutonomyEvaluationRow, evaluation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="autonomy evaluation not found")
    return autonomy.evaluation_to_dict(row)


@app.get("/api/v1/private/response-reviews", tags=["private"], dependencies=[Depends(require_admin)])
def api_list_response_reviews(
    review_status: str | None = None,
    agent_decision_id: str | None = None,
    interaction_id: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rows = response_review.list_reviews(session, review_status, agent_decision_id, interaction_id)
    return {"items": [response_review.review_to_dict(session, row) for row in rows]}


@app.post("/api/v1/private/response-reviews/from-decision/{decision_id}", tags=["private"], dependencies=[Depends(require_admin)])
def api_create_response_review(decision_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        return response_review.review_to_dict(session, response_review.create_review_for_decision(session, decision_id))
    except response_review.ReviewNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except response_review.ReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/private/response-reviews/{review_id}", tags=["private"], dependencies=[Depends(require_admin)])
def api_get_response_review(review_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = session.get(response_review.AgentResponseReviewRow, review_id)
    if row is None:
        raise HTTPException(status_code=404, detail="review not found")
    return response_review.review_to_dict(session, row)


@app.patch("/api/v1/private/response-reviews/{review_id}", tags=["private"], dependencies=[Depends(require_admin)])
def api_edit_response_review(review_id: str, payload: ResponseReviewEdit, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        row = response_review.edit_review(session, review_id, payload.text)
        return response_review.review_to_dict(session, row)
    except response_review.ReviewNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except response_review.ReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/private/response-reviews/{review_id}/approve", tags=["private"], dependencies=[Depends(require_admin)])
def api_approve_response_review(review_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        row, intent = response_review.approve_review(session, review_id)
        result = response_review.review_to_dict(session, row)
        result["execution_intent"] = response_review.intent_to_dict(intent)
        return result
    except response_review.ReviewNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except response_review.ReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/private/response-reviews/{review_id}/reject", tags=["private"], dependencies=[Depends(require_admin)])
def api_reject_response_review(review_id: str, payload: ResponseReviewReject | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        row = response_review.reject_review(session, review_id, (payload or ResponseReviewReject()).reason)
        return response_review.review_to_dict(session, row)
    except response_review.ReviewNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except response_review.ReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/private/execution-intents", tags=["private"], dependencies=[Depends(require_admin)])
def api_list_execution_intents(intent_status: str | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
    query = select(AgentExecutionIntentRow).order_by(AgentExecutionIntentRow.created_at.desc()).limit(100)
    if intent_status:
        query = query.where(AgentExecutionIntentRow.status == intent_status)
    return {"items": [execution.intent_to_dict(session, row) for row in session.scalars(query).all()]}


@app.get("/api/v1/private/execution-intents/{intent_id}", tags=["private"], dependencies=[Depends(require_admin)])
def api_get_execution_intent(intent_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = session.get(AgentExecutionIntentRow, intent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="execution intent not found")
    return execution.intent_to_dict(session, row)


@app.post("/api/v1/private/execution-intents/{intent_id}/release", tags=["private"], dependencies=[Depends(require_admin)])
def api_release_execution_intent(intent_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        transport_ready = execution.probe_transport_ready()
        return execution.intent_to_dict(
            session,
            execution.release_intent(
                session,
                intent_id,
                transport_ready=transport_ready,
            ),
        )
    except execution.ExecutionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except execution.ExecutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/private/execution-intents/{intent_id}/cancel", tags=["private"], dependencies=[Depends(require_admin)])
def api_cancel_execution_intent(intent_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        return execution.intent_to_dict(session, execution.cancel_intent(session, intent_id))
    except execution.ExecutionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# Deprecated compatibility routes. They remain local-only and admin-protected while clients migrate
# to /api/v1/admin and /api/v1/ingress.
app.add_api_route("/api/v1/policies", api_policies, methods=["GET"], tags=["admin"], dependencies=[Depends(require_admin)], deprecated=True)
app.add_api_route("/api/v1/policies/{identifier}", api_update_policy, methods=["PATCH"], tags=["admin"], dependencies=[Depends(require_admin)], deprecated=True)
app.add_api_route("/api/v1/policies", api_create_policy, methods=["POST"], tags=["admin"], dependencies=[Depends(require_admin)], deprecated=True)
app.add_api_route("/api/v1/policies/{identifier}/versions", api_policy_versions, methods=["GET"], tags=["admin"], dependencies=[Depends(require_admin)], deprecated=True)
app.add_api_route("/api/v1/policies/{identifier}/versions/{version_id}/activate", api_activate_policy_version, methods=["POST"], tags=["admin"], dependencies=[Depends(require_admin)], deprecated=True)
app.add_api_route("/api/v1/policies/{identifier}/deactivate", api_deactivate_policy, methods=["POST"], tags=["admin"], dependencies=[Depends(require_admin)], deprecated=True)
app.add_api_route("/api/v1/interactions", api_create_interaction, methods=["POST"], tags=["admin"], dependencies=[Depends(require_admin)], deprecated=True)
app.add_api_route("/api/v1/interactions/{interaction_id}", api_get_interaction, methods=["GET"], tags=["admin"], dependencies=[Depends(require_admin)], deprecated=True)
app.add_api_route("/api/v1/interactions/{interaction_id}/human-reply", api_human_reply, methods=["POST"], tags=["admin"], dependencies=[Depends(require_admin)], deprecated=True)
app.add_api_route("/api/v1/actions/{action_id}/executed", api_action_executed, methods=["POST"], tags=["admin"], dependencies=[Depends(require_admin)], deprecated=True)
app.add_api_route("/api/v1/actions/{action_id}/failed", api_action_failed, methods=["POST"], tags=["admin"], dependencies=[Depends(require_admin)], deprecated=True)
app.add_api_route("/api/v1/actions/{action_id}/acknowledge", api_action_ack, methods=["POST"], tags=["admin"], dependencies=[Depends(require_admin)], deprecated=True)
app.add_api_route("/api/v1/test-clock/advance-next", api_advance_clock, methods=["POST"], tags=["admin"], dependencies=[Depends(require_admin)], deprecated=True)


def _platform_dashboard_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    readiness = list(snapshot.get("readiness", []))
    blocking_states = {"BLOCKED", "NOT_READY", "STALE", "UNKNOWN"}
    blocking_readiness = [item for item in readiness if item.get("state") in blocking_states]
    if snapshot.get("stale") or blocking_readiness:
        overall_state = "BLOCKED"
    elif readiness and all(item.get("state") == "READY" for item in readiness):
        overall_state = "READY"
    elif readiness:
        overall_state = "DEGRADED"
    else:
        overall_state = "UNKNOWN"

    readiness_by_subject = {item.get("subject_key"): item for item in readiness}
    components = []
    for dependency in snapshot.get("dependencies", []):
        current = readiness_by_subject.get(dependency.get("canonical_key"), {})
        components.append(
            {
                "name": dependency.get("canonical_key", "unknown"),
                "state": current.get("state", "UNKNOWN"),
                "reason_code": ", ".join(current.get("reason_codes", []))
                or "READINESS_NOT_RECORDED",
                "freshness": current.get("evidence_fresh_until", "freshness unknown"),
            }
        )

    next_blocker = None
    findings = list(snapshot.get("findings", []))
    if findings:
        finding = findings[0]
        next_blocker = {
            "code": f"FINDING:{finding.get('id', 'unknown')}",
            "summary": finding.get("summary", "Active finding requires review."),
        }
    elif blocking_readiness:
        blocked = blocking_readiness[0]
        next_blocker = {
            "code": f"READINESS:{blocked.get('subject_key', 'unknown')}",
            "summary": ", ".join(blocked.get("reason_codes", []))
            or "Required readiness evidence is unavailable or stale.",
        }

    summary = {
        "READY": "All persisted readiness dimensions are current and ready.",
        "BLOCKED": "At least one persisted readiness or provenance condition blocks operation.",
        "DEGRADED": "The platform is observable but at least one advisory condition is degraded.",
        "UNKNOWN": "No current readiness result has been persisted.",
    }[overall_state]
    return snapshot | {
        "overall_state": overall_state,
        "summary": summary,
        "provenance": snapshot.get("runtime_provenance", {}),
        "gates": [
            {"name": name, **gate} for name, gate in snapshot.get("gates", {}).items()
        ],
        "components": components,
        "next_blocker": next_blocker,
    }


@app.get(
    "/ops/platform",
    response_class=HTMLResponse,
    tags=["platform-operations"],
    dependencies=[Depends(require_admin)],
)
def platform_dashboard(request: Request, session: Session = Depends(get_session)):
    snapshot = read_operations_snapshot(session, tenant_id=DEFAULT_TENANT_ID)
    return templates.TemplateResponse(
        request,
        "platform_dashboard.html",
        {"snapshot": _platform_dashboard_view(snapshot)},
    )


@app.get("/", response_class=HTMLResponse, tags=["simulation"], dependencies=[Depends(require_admin)])
def home(request: Request, session: Session = Depends(get_session)):
    latest = session.query(InteractionRow).order_by(InteractionRow.created_at.desc()).limit(8).all()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "policies": [policy.__dict__ for policy in list_policies(session)],
            "latest": [interaction_to_dict(session, row) for row in latest],
        },
    )


@app.post("/sim/interactions", tags=["simulation"], dependencies=[Depends(require_admin)])
def sim_create(
    event_type: str = Form(...),
    contact_id: str = Form(...),
    contact_name: str = Form(...),
    relationship_category: str = Form(...),
    active_context: str = Form(""),
    inbound_text: str = Form(...),
    session: Session = Depends(get_session),
):
    result = services.create_interaction(
        session,
        event_type,
        contact_id,
        contact_name,
        relationship_category,
        active_context or None,
        inbound_text,
    )
    return RedirectResponse(f"/interactions/{result['id']}", status_code=303)


@app.get("/interactions/{interaction_id}", response_class=HTMLResponse, tags=["simulation"], dependencies=[Depends(require_admin)])
def detail(interaction_id: str, request: Request, session: Session = Depends(get_session)):
    row = session.get(InteractionRow, interaction_id)
    if row is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        "detail.html",
        {"request": request, "item": interaction_to_dict(session, row), "policies": list_policies(session)},
    )


@app.post("/sim/policies/{identifier}", tags=["simulation"], dependencies=[Depends(require_admin)])
def sim_policy_update(
    identifier: str,
    tone: str = Form(...),
    initial_wait_seconds: int = Form(...),
    escalation_steps: str = Form(...),
    session: Session = Depends(get_session),
):
    update_policy(
        session,
        identifier,
        {
            "tone": tone,
            "initial_wait_seconds": initial_wait_seconds,
            "escalation_steps": [item.strip() for item in escalation_steps.split(",") if item.strip()],
        },
    )
    return RedirectResponse("/", status_code=303)
