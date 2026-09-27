-- Local-development role bootstrap. Production credentials and roles must be
-- provisioned by the deployment environment rather than committed SQL.
CREATE ROLE contractops_app LOGIN PASSWORD 'contractops-app-dev';
CREATE ROLE contractops_worker LOGIN PASSWORD 'contractops-worker-dev'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
