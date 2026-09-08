"""Allow the documented, state-only FROZEN -> RETIRED transition."""

from alembic import op


revision = "0027_frozen_retirement"
down_revision = "0026_production_authority_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS execution_intents_frozen_immutable ON execution_intents")
    op.execute("DROP FUNCTION IF EXISTS reject_frozen_execution_intent_update()")
    op.execute("""
        CREATE FUNCTION reject_frozen_execution_intent_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.state = 'FROZEN' AND NEW.state NOT IN ('FROZEN', 'MATERIALIZED', 'RETIRED') THEN
            RAISE EXCEPTION 'frozen execution intent has an invalid lifecycle transition';
          END IF;
          IF OLD.state IN ('RETIRED', 'MATERIALIZED') AND NEW.state <> OLD.state THEN
            RAISE EXCEPTION 'terminal execution intent is immutable';
          END IF;
          IF (to_jsonb(OLD) - 'status' - 'enabled' - 'created_at'
              - 'state' - 'frozen_at' - 'retired_at') IS DISTINCT FROM
             (to_jsonb(NEW) - 'status' - 'enabled' - 'created_at'
              - 'state' - 'frozen_at' - 'retired_at') THEN
            RAISE EXCEPTION 'versioned authority semantics are immutable';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER execution_intents_frozen_immutable BEFORE UPDATE ON execution_intents FOR EACH ROW EXECUTE FUNCTION reject_frozen_execution_intent_update()")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute("SELECT count(*) FROM execution_intents WHERE state = 'RETIRED'").scalar_one():
        raise RuntimeError("0027 downgrade requires zero retired execution intents")
    op.execute("DROP TRIGGER IF EXISTS execution_intents_frozen_immutable ON execution_intents")
    op.execute("DROP FUNCTION IF EXISTS reject_frozen_execution_intent_update()")
    op.execute("""
        CREATE FUNCTION reject_frozen_execution_intent_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF (to_jsonb(OLD) - 'status' - 'enabled' - 'created_at') IS DISTINCT FROM
             (to_jsonb(NEW) - 'status' - 'enabled' - 'created_at') THEN
            RAISE EXCEPTION 'versioned authority semantics are immutable';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER execution_intents_frozen_immutable BEFORE UPDATE ON execution_intents FOR EACH ROW EXECUTE FUNCTION reject_frozen_execution_intent_update()")
