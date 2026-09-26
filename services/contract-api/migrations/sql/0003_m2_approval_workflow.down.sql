DROP TRIGGER IF EXISTS approval_policies_immutable ON approval_policies;
DROP FUNCTION IF EXISTS protect_published_approval_policy();

DROP TABLE IF EXISTS approval_workflow_steps;
DROP TABLE IF EXISTS approval_instances;
DROP TABLE IF EXISTS approval_policies;
