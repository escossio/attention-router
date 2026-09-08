from fastapi.testclient import TestClient

from attention_router.application import agent_builder
from attention_router.domain.agent_builder import AgentBlueprintSpec
from attention_router.infrastructure.models import AgentBlueprintVersionRow, OutboxMessageRow
from attention_router.web.app import app, get_session
from attention_router.web.ingress_app import app as meta_ingress_app
from attention_router.web.internal_ingress_app import app as internal_ingress_app


def _answer_all(session, row, answers):
    for text in answers:
        agent_builder.record_configuration_answer(session, row.id, text)
    return row


def test_create_configuration_session(session):
    row = agent_builder.create_configuration_session(session)
    assert row.status == "active"
    assert row.current_stage == "objective"
    assert row.answers == {}


def test_record_answer_and_get_next_question_without_repeat(session):
    row = agent_builder.create_configuration_session(session)
    first = agent_builder.get_next_question(session, row.id)
    assert first.stage == "objective"
    agent_builder.record_configuration_answer(session, row.id, "Quero um assistente pessoal.")
    second = agent_builder.get_next_question(session, row.id)
    assert second.stage == "audience"
    assert second.stage != first.stage


def test_generate_blueprint_draft_safe_autonomy_and_versioning(session):
    row = agent_builder.create_configuration_session(session)
    _answer_all(
        session,
        row,
        [
            "Quero um assistente que cuide das minhas mensagens e entenda quem merece minha atencao.",
            "personal_contacts",
            "Mensagens pessoais com prioridade diferente.",
            "calmo e objetivo",
            "triage messages, identify priority, escalate important contacts",
            "agenda, contatos importantes",
            "observar e sugerir prioridade",
            "enviar mensagem e assumir compromisso",
            "responder sozinho, prometer disponibilidade",
            "contato urgente ou pessoa importante",
            "autonomous",
        ],
    )
    version1 = agent_builder.build_blueprint_from_session(session, row.id)
    spec1 = AgentBlueprintSpec.model_validate(version1.spec)
    assert spec1.domain == "personal_attention"
    assert spec1.autonomy.level == "observe"
    assert version1.version == 1

    agent_builder.correct_configuration_answer(session, row.id, "tone", "curto e discreto")
    version2 = agent_builder.build_blueprint_from_session(session, row.id)
    assert version2.version == 2
    assert version2.id != version1.id
    assert session.get(AgentBlueprintVersionRow, version1.id).spec["tone"]["style"] == "calmo e objetivo"


def test_pizzaria_uses_same_schema_and_does_not_invent_menu_or_prices(session):
    row = agent_builder.create_configuration_session(session)
    _answer_all(
        session,
        row,
        [
            "Tenho uma pizzaria e quero um assistente para atender pedidos.",
            "customers",
            "Atendimento de pedidos por entrega ou retirada.",
            "educado e direto",
            "receive orders, collect order details, clarify delivery/pickup, escalate exceptions",
            "area de entrega",
            "montar pedido e tirar duvidas basicas",
            "desconto, reembolso, excecoes de entrega",
            "inventar preco ou prometer entrega sem informacao",
            "quando faltar informacao ou houver reclamacao",
            "observe",
        ],
    )
    version = agent_builder.build_blueprint_from_session(session, row.id)
    spec = AgentBlueprintSpec.model_validate(version.spec)
    assert spec.domain == "food_service"
    assert spec.audiences == ["customers"]
    assert "menu" in spec.missing_information
    assert "prices" in spec.missing_information
    assert "business hours" in spec.missing_information
    assert "calabresa" not in str(spec.model_dump()).lower()
    assert "39.90" not in str(spec.model_dump())


def test_correction_rebuilds_new_version(session):
    row = agent_builder.create_configuration_session(session)
    agent_builder.record_configuration_answer(session, row.id, "Objetivo inicial")
    agent_builder.correct_configuration_answer(session, row.id, "objective", "Objetivo corrigido")
    assert row.answers["objective"] == "Objetivo corrigido"
    version = agent_builder.build_blueprint_from_session(session, row.id)
    assert version.spec["purpose"] == "Objetivo corrigido"


def test_agent_builder_does_not_create_outbox(session):
    row = agent_builder.create_configuration_session(session)
    agent_builder.record_configuration_answer(session, row.id, "Tenho uma pizzaria.")
    agent_builder.build_blueprint_from_session(session, row.id)
    assert session.query(OutboxMessageRow).count() == 0


def test_agent_builder_admin_endpoints_exist_only_on_control_plane(monkeypatch, session):
    monkeypatch.setattr("attention_router.web.app.settings.admin_token", "synthetic-admin-token")
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)
    try:
        response = client.post(
            "/api/v1/admin/agent-builder/sessions",
            headers={"Authorization": "Bearer synthetic-admin-token"},
        )
        assert response.status_code == 200
        assert TestClient(meta_ingress_app).post("/api/v1/admin/agent-builder/sessions").status_code == 404
        assert TestClient(internal_ingress_app).post("/api/v1/admin/agent-builder/sessions").status_code == 404
    finally:
        app.dependency_overrides.clear()
