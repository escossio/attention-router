"""Reserved contextual counters, written only while the binding row is locked."""
from sqlalchemy import select

from attention_router.infrastructure.models import ActorBindingRow

RESERVED_FIELD = "context_generations"


def locked_binding(session, binding_id, tenant_id):
    return session.scalar(select(ActorBindingRow).where(
        ActorBindingRow.id == binding_id, ActorBindingRow.tenant_id == tenant_id,
    ).with_for_update().execution_options(populate_existing=True))


def advance_generations(session, binding, scopes, reason, reference=None):
    from attention_router.infrastructure.repository import audit

    scopes = sorted(set(scopes))
    if not scopes:
        return
    counters = dict((binding.binding_metadata or {}).get(RESERVED_FIELD, {}))
    for scope in scopes:
        previous = counters.get(scope, 0)
        if type(previous) is not int or previous < 0:
            raise ValueError("CONTEXT_GENERATION_LEDGER_INVALID")
        counters[scope] = previous + 1
        audit(
            session, None, "CONTEXT_VALIDITY_CHANGED",
            dict(scope=scope, previous_generation=previous, generation=previous + 1,
                 reason=reason, reference=reference),
            tenant_id=binding.tenant_id, origin="context_validity",
        )
    binding.binding_metadata = {**(binding.binding_metadata or {}), RESERVED_FIELD: counters}
    session.flush()
