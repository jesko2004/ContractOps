DROP TABLE IF EXISTS document_findings;

ALTER TABLE contract_versions
    DROP COLUMN IF EXISTS object_etag,
    DROP COLUMN IF EXISTS uploaded_at;
