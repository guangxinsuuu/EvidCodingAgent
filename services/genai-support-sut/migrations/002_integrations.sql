-- 真实来源快照与原有Job/outbox同事务受理；外部ID和本地身份明确分列。
CREATE TABLE IF NOT EXISTS egap_support.external_inputs (
    tenant_id text NOT NULL,
    ticket_id text NOT NULL,
    ticket_revision bigint NOT NULL,
    input_sha256 text NOT NULL,
    context jsonb NOT NULL,
    source_receipts jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id,ticket_id,ticket_revision),
    FOREIGN KEY (tenant_id,ticket_id) REFERENCES egap_support.support_tickets(tenant_id,ticket_id)
);
CREATE TABLE IF NOT EXISTS egap_support.external_operations (
    tenant_id text NOT NULL,
    operation_key text NOT NULL,
    request_sha256 text NOT NULL,
    job_id text NOT NULL UNIQUE REFERENCES egap_support.support_jobs(job_id),
    subject text NOT NULL,
    account_id bigint NOT NULL,
    conversation_id bigint NOT NULL,
    contact_id bigint NOT NULL,
    invoice_id text NOT NULL,
    message_sha256 text NOT NULL,
    delivery_state text NOT NULL DEFAULT 'PENDING'
        CHECK (delivery_state IN ('PENDING','DISPATCHING','COMPLETE','UNKNOWN','STALE','FAILED')),
    external_message_id bigint,
    delivery_error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id,operation_key)
);
CREATE TABLE IF NOT EXISTS egap_support.external_api_observations (
    observation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    correlation_id text NOT NULL,
    event jsonb NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
-- 多个重复事件键可对应同一业务输入，但交付主记录始终只有一条。
CREATE TABLE IF NOT EXISTS egap_support.external_request_keys (
    tenant_id text NOT NULL,
    operation_key text NOT NULL,
    request_sha256 text NOT NULL,
    job_id text NOT NULL REFERENCES egap_support.support_jobs(job_id),
    PRIMARY KEY (tenant_id,operation_key)
);
CREATE INDEX IF NOT EXISTS external_observations_correlation
    ON egap_support.external_api_observations(correlation_id,observation_id);
