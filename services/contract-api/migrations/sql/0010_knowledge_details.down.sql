DROP TABLE knowledge_inspector_jobs;
DROP INDEX knowledge_documents_version_idx;
DROP INDEX knowledge_documents_current_idx;
ALTER TABLE knowledge_document_chunks DROP COLUMN kind, DROP COLUMN source_id, DROP COLUMN source_pages;
ALTER TABLE knowledge_documents DROP COLUMN parse_details, DROP COLUMN is_current,
    DROP COLUMN version_number, DROP COLUMN root_id;
