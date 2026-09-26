ALTER TABLE contracts
    ADD COLUMN department_id uuid,
    ADD COLUMN amount numeric(18,2) CHECK (amount >= 0),
    ADD COLUMN currency char(3) CHECK (currency ~ '^[A-Z]{3}$'),
    ADD COLUMN valid_from date,
    ADD COLUMN valid_until date,
    ADD CONSTRAINT contracts_validity_check
        CHECK (valid_from IS NULL OR valid_until IS NULL OR valid_until >= valid_from),
    ADD CONSTRAINT contracts_amount_currency_check
        CHECK ((amount IS NULL) = (currency IS NULL));

-- M0 environments contain no business rows. The backfill keeps the migration
-- deterministic for developer databases that may already contain draft data.
UPDATE contracts SET department_id = gen_random_uuid() WHERE department_id IS NULL;
ALTER TABLE contracts ALTER COLUMN department_id SET NOT NULL;

CREATE INDEX contracts_department_lookup_idx
    ON contracts (tenant_id, department_id, updated_at DESC);
CREATE INDEX contracts_owner_lookup_idx
    ON contracts (tenant_id, created_by, updated_at DESC);

CREATE OR REPLACE FUNCTION reject_contract_version_core_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF ROW(
        NEW.tenant_id, NEW.contract_id, NEW.version_number, NEW.object_key,
        NEW.file_name, NEW.media_type, NEW.size_bytes, NEW.content_hash,
        NEW.created_by, NEW.created_at
    ) IS DISTINCT FROM ROW(
        OLD.tenant_id, OLD.contract_id, OLD.version_number, OLD.object_key,
        OLD.file_name, OLD.media_type, OLD.size_bytes, OLD.content_hash,
        OLD.created_by, OLD.created_at
    ) THEN
        RAISE EXCEPTION 'contract version core fields are immutable'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER contract_versions_core_immutable
    BEFORE UPDATE ON contract_versions
    FOR EACH ROW
    EXECUTE FUNCTION reject_contract_version_core_mutation();
