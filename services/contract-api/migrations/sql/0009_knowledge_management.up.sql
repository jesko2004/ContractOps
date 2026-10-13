CREATE TABLE knowledge_bases (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    name varchar(100) NOT NULL CHECK (length(trim(name)) > 0),
    description text NOT NULL,
    department_id uuid NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (tenant_id, id), UNIQUE (tenant_id, created_by, name)
);
CREATE TABLE knowledge_documents (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    base_id uuid NOT NULL,
    version_id uuid NOT NULL,
    file_name varchar(255) NOT NULL,
    document_type varchar(20) NOT NULL CHECK (document_type IN ('CONTRACT', 'POLICY', 'TEMPLATE')),
    media_type varchar(150) NOT NULL,
    size_bytes integer NOT NULL CHECK (size_bytes BETWEEN 0 AND 5242880),
    file_hash varchar(64) NOT NULL,
    original bytea CHECK (original IS NULL OR octet_length(original) <= 5242880),
    status varchar(20) NOT NULL CHECK (status IN ('PARSING', 'READY', 'FAILED', 'ARCHIVED')),
    state_version integer NOT NULL CHECK (state_version >= 0),
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    lease_expires_at timestamptz,
    error_code varchar(100), error_message text,
    UNIQUE (tenant_id, id), UNIQUE (tenant_id, base_id, file_hash),
    FOREIGN KEY (tenant_id, base_id) REFERENCES knowledge_bases(tenant_id, id)
);
CREATE TABLE knowledge_document_chunks (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    document_id uuid NOT NULL,
    sequence integer NOT NULL CHECK (sequence >= 0),
    page_number integer CHECK (page_number > 0),
    heading_path json NOT NULL,
    content text NOT NULL,
    content_hash varchar(64) NOT NULL,
    UNIQUE (tenant_id, document_id, sequence),
    FOREIGN KEY (tenant_id, document_id) REFERENCES knowledge_documents(tenant_id, id)
);
CREATE INDEX knowledge_bases_scope_idx ON knowledge_bases(tenant_id, department_id, created_by);
CREATE INDEX knowledge_documents_base_idx ON knowledge_documents(tenant_id, base_id, updated_at DESC);

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['knowledge_bases', 'knowledge_documents', 'knowledge_document_chunks']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('CREATE POLICY tenant_isolation ON %I USING '
            '(tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', table_name);
    END LOOP;
END $$;
