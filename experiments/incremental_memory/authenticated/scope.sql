CREATE TABLE memory_environment (
 singleton boolean PRIMARY KEY CHECK(singleton),
 classification text NOT NULL CHECK(classification='SYNTHETIC_M1')
);
INSERT INTO memory_environment VALUES(true,'SYNTHETIC_M1');
CREATE TABLE memory_scopes (
 id text PRIMARY KEY CHECK(id ~ '^ctx_[0-9a-f]{64}$'),
 tenant_id varchar(64) NOT NULL REFERENCES tenants(id),
 owner_id varchar(64) NOT NULL REFERENCES human_identities(id),
 purpose text NOT NULL CHECK(purpose='SYNTHETIC_MEMORY_QUERY'),
 active boolean NOT NULL DEFAULT true,
 expires_at timestamptz NOT NULL,
 revision integer NOT NULL DEFAULT 1 CHECK(revision>0)
);
CREATE TABLE memory_scope_information (
 scope_id text NOT NULL REFERENCES memory_scopes(id),
 information_id uuid NOT NULL REFERENCES information(id),
 PRIMARY KEY(scope_id,information_id)
);
CREATE FUNCTION memory_m1_write_fence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(721479,1);
 RETURN NULL;
END;
$$;
