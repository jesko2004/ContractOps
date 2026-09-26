DROP TRIGGER IF EXISTS contract_versions_core_immutable ON contract_versions;
DROP FUNCTION IF EXISTS reject_contract_version_core_mutation();

DROP INDEX IF EXISTS contracts_owner_lookup_idx;
DROP INDEX IF EXISTS contracts_department_lookup_idx;

ALTER TABLE contracts
    DROP CONSTRAINT IF EXISTS contracts_amount_currency_check,
    DROP CONSTRAINT IF EXISTS contracts_validity_check,
    DROP COLUMN IF EXISTS valid_until,
    DROP COLUMN IF EXISTS valid_from,
    DROP COLUMN IF EXISTS currency,
    DROP COLUMN IF EXISTS amount,
    DROP COLUMN IF EXISTS department_id;
