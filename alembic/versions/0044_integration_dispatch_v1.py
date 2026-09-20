"""Add durable integration inbox dispatch lifecycle."""

from alembic import op
import sqlalchemy as sa


revision = "0044_integration_dispatch_v1"
down_revision = "0043_pending_intents_v1"
branch_labels = None
depends_on = None


def _replace_guard_function(*, dispatch_enabled: bool) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    if dispatch_enabled:
        op.execute(
            """
            CREATE OR REPLACE FUNCTION guard_integration_identity()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'INTEGRATION_IDENTITY_IMMUTABLE';
              END IF;

              IF TG_TABLE_NAME = 'integration_bindings' THEN
                IF (to_jsonb(NEW) - 'active' - 'scopes') IS DISTINCT FROM
                   (to_jsonb(OLD) - 'active' - 'scopes') THEN
                  RAISE EXCEPTION 'INTEGRATION_IDENTITY_IMMUTABLE';
                END IF;
              ELSIF TG_TABLE_NAME = 'integration_credentials' THEN
                IF (to_jsonb(NEW) - 'revoked' - 'scopes') IS DISTINCT FROM
                   (to_jsonb(OLD) - 'revoked' - 'scopes')
                   OR (OLD.revoked AND NOT NEW.revoked) THEN
                  RAISE EXCEPTION 'INTEGRATION_IDENTITY_IMMUTABLE';
                END IF;
              ELSE
                IF OLD.state <> 'PENDING' OR NEW.state NOT IN ('PROCESSED', 'BLOCKED') THEN
                  RAISE EXCEPTION 'INTEGRATION_INBOX_IMMUTABLE';
                END IF;
                IF (
                    to_jsonb(NEW)
                    - 'state'
                    - 'canonical_event_id'
                    - 'processed_at'
                    - 'dispatch_reason'
                ) IS DISTINCT FROM (
                    to_jsonb(OLD)
                    - 'state'
                    - 'canonical_event_id'
                    - 'processed_at'
                    - 'dispatch_reason'
                ) THEN
                  RAISE EXCEPTION 'INTEGRATION_INBOX_IMMUTABLE';
                END IF;
              END IF;
              RETURN NEW;
            END $$
            """
        )
    else:
        op.execute(
            """
            CREATE OR REPLACE FUNCTION guard_integration_identity()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'INTEGRATION_IDENTITY_IMMUTABLE';
              END IF;
              IF TG_TABLE_NAME = 'integration_bindings' THEN
                IF (to_jsonb(NEW) - 'active' - 'scopes') IS DISTINCT FROM
                   (to_jsonb(OLD) - 'active' - 'scopes') THEN
                  RAISE EXCEPTION 'INTEGRATION_IDENTITY_IMMUTABLE';
                END IF;
              ELSIF TG_TABLE_NAME = 'integration_credentials' THEN
                IF (to_jsonb(NEW) - 'revoked' - 'scopes') IS DISTINCT FROM
                   (to_jsonb(OLD) - 'revoked' - 'scopes')
                   OR (OLD.revoked AND NOT NEW.revoked) THEN
                  RAISE EXCEPTION 'INTEGRATION_IDENTITY_IMMUTABLE';
                END IF;
              ELSE
                RAISE EXCEPTION 'INTEGRATION_INBOX_IMMUTABLE';
              END IF;
              RETURN NEW;
            END $$
            """
        )


def upgrade() -> None:
    op.drop_constraint(
        "ck_integration_inbox_state",
        "integration_inbox",
        type_="check",
    )
    op.add_column(
        "integration_inbox",
        sa.Column(
            "canonical_event_id",
            sa.String(64),
            sa.ForeignKey("canonical_events.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "integration_inbox",
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "integration_inbox",
        sa.Column("dispatch_reason", sa.String(120), nullable=True),
    )
    op.create_unique_constraint(
        "uq_integration_inbox_canonical_event",
        "integration_inbox",
        ["canonical_event_id"],
    )
    op.create_check_constraint(
        "ck_integration_inbox_dispatch_state",
        "integration_inbox",
        """
        (
          state = 'PENDING'
          AND canonical_event_id IS NULL
          AND processed_at IS NULL
          AND dispatch_reason IS NULL
        )
        OR
        (
          state = 'PROCESSED'
          AND canonical_event_id IS NOT NULL
          AND processed_at IS NOT NULL
          AND dispatch_reason IS NULL
        )
        OR
        (
          state = 'BLOCKED'
          AND canonical_event_id IS NULL
          AND processed_at IS NOT NULL
          AND dispatch_reason IS NOT NULL
        )
        """,
    )
    _replace_guard_function(dispatch_enabled=True)


def downgrade() -> None:
    bind = op.get_bind()
    populated = bind.execute(
        sa.text(
            """
            SELECT 1
            FROM integration_inbox
            WHERE state <> 'PENDING'
               OR canonical_event_id IS NOT NULL
               OR processed_at IS NOT NULL
               OR dispatch_reason IS NOT NULL
            LIMIT 1
            """
        )
    ).first()
    if populated:
        raise RuntimeError(
            "INTEGRATION_DISPATCH_DOWNGRADE_REQUIRES_DATA_EXPORT"
        )

    _replace_guard_function(dispatch_enabled=False)
    op.drop_constraint(
        "ck_integration_inbox_dispatch_state",
        "integration_inbox",
        type_="check",
    )
    op.drop_constraint(
        "uq_integration_inbox_canonical_event",
        "integration_inbox",
        type_="unique",
    )
    op.drop_column("integration_inbox", "dispatch_reason")
    op.drop_column("integration_inbox", "processed_at")
    op.drop_column("integration_inbox", "canonical_event_id")
    op.create_check_constraint(
        "ck_integration_inbox_state",
        "integration_inbox",
        "state = 'PENDING'",
    )
