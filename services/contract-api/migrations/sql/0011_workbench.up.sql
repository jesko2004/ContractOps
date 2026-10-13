CREATE TABLE workspace_contract_documents (
    contract_version_id uuid PRIMARY KEY REFERENCES contract_versions(id),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    contract_id uuid NOT NULL REFERENCES contracts(id),
    document_id uuid NOT NULL,
    request_key varchar(200) NOT NULL,
    request_hash varchar(64) NOT NULL,
    confirmation json NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (tenant_id, contract_id, request_key),
    FOREIGN KEY (tenant_id, document_id) REFERENCES knowledge_documents(tenant_id,id)
);
CREATE TABLE workspace_review_reports (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    contract_id uuid NOT NULL REFERENCES contracts(id),
    document_id uuid NOT NULL,
    policy_base_id uuid NOT NULL,
    fingerprint varchar(64) NOT NULL,
    payload json NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL,
    FOREIGN KEY (tenant_id, document_id) REFERENCES knowledge_documents(tenant_id,id),
    FOREIGN KEY (tenant_id, policy_base_id) REFERENCES knowledge_bases(tenant_id,id)
);
CREATE INDEX workspace_reports_contract_idx ON workspace_review_reports(tenant_id,contract_id,created_at DESC);
ALTER TABLE workspace_contract_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_contract_documents FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON workspace_contract_documents
    USING (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid);
ALTER TABLE workspace_review_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_review_reports FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON workspace_review_reports
    USING (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid);
