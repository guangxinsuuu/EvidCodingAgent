-- 显式商业事实重新评估保留旧快照，只新增 revision 与父 Job 关系。
-- 可空列兼容接入商业来源前的历史记录；读取时按原 context 计算旧事实摘要。
ALTER TABLE egap_support.external_inputs
    ADD COLUMN IF NOT EXISTS assessment_facts_sha256 text,
    ADD COLUMN IF NOT EXISTS assessment_source_revision bigint;
ALTER TABLE egap_support.external_operations
    ADD COLUMN IF NOT EXISTS parent_job_id text REFERENCES egap_support.support_jobs(job_id),
    ADD COLUMN IF NOT EXISTS reevaluation_reason text;
CREATE INDEX IF NOT EXISTS external_operations_parent_job
    ON egap_support.external_operations(parent_job_id) WHERE parent_job_id IS NOT NULL;
