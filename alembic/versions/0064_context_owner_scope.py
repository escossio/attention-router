"""Explicit owner-authorized context sharing between proven owner memberships."""
from alembic import op
import sqlalchemy as sa
revision='0064_context_owner_scope'
down_revision='0063_conversation_context'
branch_labels=depends_on=None


def upgrade():
    op.execute('''CREATE TABLE conversation_context_owner_scopes (
      tenant_id varchar(64) NOT NULL REFERENCES tenants(id),
      source_account varchar(180) NOT NULL,
      context_tenant_id varchar(64) NOT NULL REFERENCES tenants(id),
      owner_id varchar(64) NOT NULL REFERENCES human_identities(id),
      source_membership_id varchar(64) NOT NULL REFERENCES client_tenant_memberships(id),
      context_membership_id varchar(64) NOT NULL REFERENCES client_tenant_memberships(id),
      authorization_ref varchar(240) NOT NULL,
      authorized_at timestamptz NOT NULL DEFAULT now(),
      revoked_at timestamptz,
      PRIMARY KEY(tenant_id,source_account,context_tenant_id,owner_id))''')
    op.add_column('conversation_message_context',sa.Column('location_tenant_id',sa.String(64),sa.ForeignKey('tenants.id')))
    op.execute('UPDATE conversation_message_context SET location_tenant_id=tenant_id WHERE observation_id IS NOT NULL')
    # PostgreSQL assigns this deterministic name to the unnamed composite FK in 0063.
    op.execute('ALTER TABLE conversation_message_context DROP CONSTRAINT conversation_message_context_tenant_id_device_id_observati_fkey')
    op.execute('''ALTER TABLE conversation_message_context ADD CONSTRAINT fk_context_location_scope
      FOREIGN KEY(location_tenant_id,device_id,observation_id)
      REFERENCES client_location_observations(tenant_id,device_id,observation_id)''')
    op.execute("ALTER TABLE conversation_message_context ADD CONSTRAINT ck_context_location_tenant CHECK ((observation_id IS NULL AND location_tenant_id IS NULL) OR (observation_id IS NOT NULL AND location_tenant_id IS NOT NULL))")
    definition=op.get_bind().execute(sa.text("SELECT pg_get_viewdef('conversation_context_v1'::regclass,true)")).scalar()
    definition=definition.replace('o.tenant_id::text = c.tenant_id::text','o.tenant_id::text = c.location_tenant_id::text')
    if 'o.tenant_id::text = c.location_tenant_id::text' not in definition:
        raise RuntimeError('CONTEXT_VIEW_SCOPE_REWRITE_FAILED')
    op.execute('CREATE OR REPLACE VIEW conversation_context_v1 AS '+definition)


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM conversation_context_owner_scopes LIMIT 1')).first():
        raise RuntimeError('CONTEXT_SCOPE_NONEMPTY_DOWNGRADE_REQUIRES_ARCHIVE')
    definition=op.get_bind().execute(sa.text("SELECT pg_get_viewdef('conversation_context_v1'::regclass,true)")).scalar()
    op.execute('CREATE OR REPLACE VIEW conversation_context_v1 AS '+definition.replace('o.tenant_id::text = c.location_tenant_id::text','o.tenant_id::text = c.tenant_id::text'))
    op.execute('ALTER TABLE conversation_message_context DROP CONSTRAINT fk_context_location_scope')
    op.execute('ALTER TABLE conversation_message_context DROP CONSTRAINT ck_context_location_tenant')
    op.drop_column('conversation_message_context','location_tenant_id')
    op.execute('''ALTER TABLE conversation_message_context ADD FOREIGN KEY(tenant_id,device_id,observation_id)
      REFERENCES client_location_observations(tenant_id,device_id,observation_id)''')
    op.drop_table('conversation_context_owner_scopes')
