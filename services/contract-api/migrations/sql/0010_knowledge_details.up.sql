ALTER TABLE knowledge_documents
    ADD COLUMN root_id uuid,
    ADD COLUMN version_number integer NOT NULL DEFAULT 1 CHECK (version_number > 0),
    ADD COLUMN is_current boolean NOT NULL DEFAULT true,
    ADD COLUMN parse_details json;
UPDATE knowledge_documents SET root_id = id;
CREATE UNIQUE INDEX knowledge_documents_current_idx
    ON knowledge_documents(tenant_id, root_id) WHERE is_current;
CREATE UNIQUE INDEX knowledge_documents_version_idx
    ON knowledge_documents(tenant_id, root_id, version_number);
ALTER TABLE knowledge_document_chunks
    ADD COLUMN source_pages json,
    ADD COLUMN source_id varchar(100),
    ADD COLUMN kind varchar(30);
CREATE TABLE knowledge_inspector_jobs (
    document_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    endpoint varchar(500) NOT NULL,
    profile_id varchar(100) NOT NULL,
    request_key varchar(100) NOT NULL,
    task_id varchar(100),
    status varchar(30) NOT NULL,
    progress integer NOT NULL CHECK (progress BETWEEN 0 AND 100),
    error_code varchar(100),
    updated_at timestamptz NOT NULL,
    UNIQUE (tenant_id, request_key),
    FOREIGN KEY (tenant_id, document_id) REFERENCES knowledge_documents(tenant_id, id)
);
ALTER TABLE knowledge_inspector_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_inspector_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON knowledge_inspector_jobs
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
