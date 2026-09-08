"""allow multiple inbound memberships per lab session

Revision ID: 0013_lab_inbound_membership_pk
Revises: 0012_lab_conversation
"""

from alembic import op

revision = "0013_lab_inbound_membership_pk"
down_revision = "0012_lab_conversation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("lab_conversation_inbounds_pkey", "lab_conversation_inbounds", type_="primary")
    op.create_primary_key("lab_conversation_inbounds_pkey", "lab_conversation_inbounds", ["inbound_event_id"])
    op.create_unique_constraint(
        "uq_lab_inbound_session_ordinal",
        "lab_conversation_inbounds",
        ["session_id", "ordinal"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_lab_inbound_session_ordinal", "lab_conversation_inbounds", type_="unique")
    op.drop_constraint("lab_conversation_inbounds_pkey", "lab_conversation_inbounds", type_="primary")
    op.create_primary_key("lab_conversation_inbounds_pkey", "lab_conversation_inbounds", ["session_id"])
