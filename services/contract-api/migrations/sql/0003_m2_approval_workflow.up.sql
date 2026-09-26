CREATE TABLE approval_policies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    policy_key text NOT NULL,
    version_number integer NOT NULL CHECK (version_number > 0),
    name text NOT NULL,
    priority integer NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN ('DRAFT', 'PUBLISHED')),
    condition jsonb NOT NULL DEFAULT '{}'::jsonb,
    steps jsonb NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_by uuid,
    published_at timestamptz,
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, policy_key, version_number),
    CHECK (jsonb_typeof(condition) = 'object'),
    CHECK (jsonb_typeof(steps) = 'array' AND jsonb_array_length(steps) > 0),
    CHECK (
        (status = 'DRAFT' AND published_at IS NULL AND published_by IS NULL)
        OR (status = 'PUBLISHED' AND published_at IS NOT NULL AND published_by IS NOT NULL)
    )
);

CREATE UNIQUE INDEX approval_policies_one_draft_idx
    ON approval_policies (tenant_id, policy_key) WHERE status = 'DRAFT';
CREATE INDEX approval_policies_match_idx
    ON approval_policies (tenant_id, status, priority DESC, version_number DESC);

CREATE TABLE approval_instances (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    contract_id uuid NOT NULL,
    contract_version_id uuid NOT NULL,
    policy_id uuid NOT NULL,
    policy_snapshot jsonb NOT NULL,
    status text NOT NULL DEFAULT 'IN_PROGRESS'
        CHECK (status IN ('IN_PROGRESS', 'APPROVED', 'REJECTED', 'CHANGES_REQUESTED')),
    state_version integer NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    requested_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, contract_id) REFERENCES contracts(tenant_id, id),
    FOREIGN KEY (tenant_id, contract_version_id)
        REFERENCES contract_versions(tenant_id, id),
    FOREIGN KEY (tenant_id, policy_id) REFERENCES approval_policies(tenant_id, id),
    CHECK (jsonb_typeof(policy_snapshot) = 'object'),
    CHECK (
        (status = 'IN_PROGRESS' AND completed_at IS NULL)
        OR (status <> 'IN_PROGRESS' AND completed_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX approval_instances_one_active_idx
    ON approval_instances (tenant_id, contract_id, contract_version_id)
    WHERE status = 'IN_PROGRESS';

CREATE TABLE approval_workflow_steps (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    approval_instance_id uuid NOT NULL,
    step_order integer NOT NULL CHECK (step_order > 0),
    name text NOT NULL,
    required_role text NOT NULL CHECK (required_role IN (
        'LEGAL_ADMIN', 'FINANCE_APPROVER', 'BUSINESS_APPROVER'
    )),
    status text NOT NULL DEFAULT 'PENDING' CHECK (status IN (
        'PENDING', 'READY', 'CLAIMED', 'APPROVED', 'REJECTED',
        'CHANGES_REQUESTED', 'SKIPPED'
    )),
    assigned_to uuid,
    state_version integer NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    decided_by uuid,
    decision_comment text,
    decided_at timestamptz,
    transferred_by uuid,
    transfer_comment text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, approval_instance_id, step_order),
    FOREIGN KEY (tenant_id, approval_instance_id)
        REFERENCES approval_instances(tenant_id, id) ON DELETE CASCADE,
    CHECK (
        (status IN ('APPROVED', 'REJECTED', 'CHANGES_REQUESTED')
            AND decided_by IS NOT NULL AND decided_at IS NOT NULL)
        OR (status NOT IN ('APPROVED', 'REJECTED', 'CHANGES_REQUESTED'))
    )
);

CREATE INDEX approval_workflow_steps_task_idx
    ON approval_workflow_steps (tenant_id, status, required_role, created_at)
    WHERE status IN ('READY', 'CLAIMED');
CREATE INDEX approval_workflow_steps_assignee_idx
    ON approval_workflow_steps (tenant_id, assigned_to, status)
    WHERE assigned_to IS NOT NULL AND status IN ('READY', 'CLAIMED');

CREATE OR REPLACE FUNCTION protect_published_approval_policy()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' AND OLD.status = 'PUBLISHED' THEN
        RAISE EXCEPTION 'published approval policies are immutable'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.status = 'PUBLISHED' THEN
        RAISE EXCEPTION 'published approval policies are immutable'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.status = 'PUBLISHED' AND ROW(
        NEW.tenant_id, NEW.policy_key, NEW.version_number, NEW.name,
        NEW.priority, NEW.condition, NEW.steps, NEW.created_by, NEW.created_at
    ) IS DISTINCT FROM ROW(
        OLD.tenant_id, OLD.policy_key, OLD.version_number, OLD.name,
        OLD.priority, OLD.condition, OLD.steps, OLD.created_by, OLD.created_at
    ) THEN
        RAISE EXCEPTION 'policy content cannot change while publishing'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER approval_policies_immutable
    BEFORE UPDATE OR DELETE ON approval_policies
    FOR EACH ROW EXECUTE FUNCTION protect_published_approval_policy();

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'approval_policies', 'approval_instances', 'approval_workflow_steps'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING '
            || '(tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
            || 'WITH CHECK '
            || '(tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)',
            table_name
        );
    END LOOP;
END $$;
