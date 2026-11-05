-- 仅由可信初始化入口执行。SUT业务存储与EGAP治理账本、网关记录分域。
CREATE SCHEMA IF NOT EXISTS egap_support;

CREATE TABLE IF NOT EXISTS egap_support.support_fixture_binding (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    schema_version integer NOT NULL CHECK (schema_version = 1),
    fixture_sha256 text NOT NULL CHECK (fixture_sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS egap_support.support_tickets (
    tenant_id text NOT NULL,
    ticket_id text NOT NULL,
    current_revision bigint NOT NULL CHECK (current_revision BETWEEN 1 AND 9007199254740991),
    PRIMARY KEY (tenant_id, ticket_id)
);

CREATE TABLE IF NOT EXISTS egap_support.support_jobs (
    job_id text PRIMARY KEY CHECK (job_id ~ '^[0-9a-f]{32}$'),
    tenant_id text NOT NULL,
    ticket_id text NOT NULL,
    ticket_revision bigint NOT NULL CHECK (ticket_revision BETWEEN 1 AND 9007199254740991),
    status text NOT NULL CHECK (status IN ('QUEUED', 'PROCESSING', 'DONE', 'FAILED')),
    draft jsonb,
    error_code text,
    lease_token bigint NOT NULL DEFAULT 0,
    lease_owner text,
    lease_until timestamptz,
    current_attempt_id text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, ticket_id) REFERENCES egap_support.support_tickets(tenant_id, ticket_id),
    CHECK (COALESCE((
      (status IN ('QUEUED', 'PROCESSING') AND draft IS NULL AND error_code IS NULL)
      OR (status = 'FAILED' AND draft IS NULL AND error_code = 'PROCESSING_FAILED')
      OR (status = 'DONE' AND draft IS NOT NULL AND error_code IS NULL
          AND jsonb_typeof(draft) = 'object'
          AND draft->>'ticket_id' = ticket_id
          AND draft->'ticket_revision' = to_jsonb(ticket_revision))
    ), false)),
    CHECK (status <> 'PROCESSING' OR
           (lease_owner IS NOT NULL AND lease_until IS NOT NULL AND current_attempt_id IS NOT NULL AND lease_token > 0))
);

-- Job和此记录同事务写入；发送Redis后再标记。回执不明允许重复通知同一Job。
CREATE TABLE IF NOT EXISTS egap_support.support_dispatch_outbox (
    outbox_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id text NOT NULL UNIQUE REFERENCES egap_support.support_jobs(job_id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    dispatched_at timestamptz
);
CREATE INDEX IF NOT EXISTS support_outbox_pending ON egap_support.support_dispatch_outbox(outbox_id)
    WHERE dispatched_at IS NULL;

CREATE TABLE IF NOT EXISTS egap_support.support_job_attempts (
    attempt_id text PRIMARY KEY CHECK (attempt_id ~ '^[0-9a-f]{32}$'),
    job_id text NOT NULL REFERENCES egap_support.support_jobs(job_id),
    lease_token bigint NOT NULL,
    owner text NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'UNKNOWN', 'STALE')),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    UNIQUE (job_id, lease_token)
);

CREATE TABLE IF NOT EXISTS egap_support.support_job_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id text NOT NULL REFERENCES egap_support.support_jobs(job_id),
    attempt_id text REFERENCES egap_support.support_job_attempts(attempt_id),
    kind text NOT NULL CHECK (kind IN ('ACCEPTED', 'CLAIMED', 'DONE', 'FAILED', 'UNKNOWN', 'SUPERSEDED')),
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS support_events_job ON egap_support.support_job_events(job_id, event_id);

CREATE TABLE IF NOT EXISTS egap_support.support_service_heartbeats (
    role text NOT NULL CHECK (role IN ('relay', 'worker')),
    owner text NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (role, owner)
);
