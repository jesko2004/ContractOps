ALTER TABLE contract_versions
    ADD COLUMN uploaded_at timestamptz,
    ADD COLUMN object_etag text;

CREATE TABLE document_findings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    contract_version_id uuid NOT NULL,
    source_chunk_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('AMOUNT', 'DATE', 'RISK', 'OBLIGATION')),
    origin text NOT NULL CHECK (origin IN ('RULE', 'MODEL')),
    title text NOT NULL,
    normalized_value text,
    source_page integer CHECK (source_page > 0),
    source_text text NOT NULL,
    confidence numeric(5,4) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    model_name text,
    model_output_hash text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, contract_version_id)
        REFERENCES contract_versions(tenant_id, id),
    FOREIGN KEY (tenant_id, source_chunk_id)
        REFERENCES contract_chunks(tenant_id, id),
    CHECK (
        (origin = 'MODEL' AND model_name IS NOT NULL AND model_output_hash IS NOT NULL)
        OR (origin = 'RULE' AND model_name IS NULL AND model_output_hash IS NULL)
    )
);

CREATE INDEX document_findings_version_idx
    ON document_findings (tenant_id, contract_version_id, kind, created_at);

ALTER TABLE document_findings ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_findings FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON document_findings
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'contractops_worker') THEN
        GRANT SELECT, UPDATE ON contract_versions TO contractops_worker;
        GRANT SELECT, UPDATE ON ingestion_jobs TO contractops_worker;
        GRANT SELECT, INSERT, DELETE ON contract_chunks TO contractops_worker;
        GRANT SELECT, INSERT, DELETE ON document_findings TO contractops_worker;
        GRANT INSERT ON outbox_events TO contractops_worker;
        GRANT INSERT ON audit_events TO contractops_worker;
    END IF;
END $$;
