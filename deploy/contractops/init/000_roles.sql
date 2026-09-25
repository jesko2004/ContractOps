-- Local-development role bootstrap. Production credentials and roles must be
-- provisioned by the deployment environment rather than committed SQL.
CREATE ROLE contractops_app LOGIN PASSWORD 'contractops-app-dev';
