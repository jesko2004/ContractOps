from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def test_workbench_migration_and_runtime_grants() -> None:
    from contractops.infrastructure.postgres.permissions import API_PRIVILEGES

    sql = (SERVICE_ROOT / "migrations/sql/0011_workbench.up.sql").read_text(encoding="utf-8")
    for table in ("workspace_contract_documents", "workspace_review_reports"):
        assert table in sql
        assert set(API_PRIVILEGES[table]) >= {"SELECT", "INSERT"}
    assert "FORCE ROW LEVEL SECURITY" in sql and "WITH CHECK" in sql
    assert "app.tenant_id" in sql
    assert "UPDATE" not in API_PRIVILEGES["workspace_review_reports"]
    assert (SERVICE_ROOT / "migrations/versions/20261009_0011_workbench.py").is_file()
    assert (SERVICE_ROOT / "migrations/sql/0011_workbench.down.sql").is_file()


def test_knowledge_details_migration_and_runtime_grants() -> None:
    from contractops.infrastructure.postgres.permissions import API_PRIVILEGES, SCHEMA_REVISION

    assert SCHEMA_REVISION == "20261009_0011"
    assert (SERVICE_ROOT / "migrations/versions/20261009_0010_knowledge_details.py").is_file()
    sql = (SERVICE_ROOT / "migrations/sql/0010_knowledge_details.up.sql").read_text(
        encoding="utf-8"
    )
    for clause in (
        "root_id",
        "is_current",
        "version_number",
        "source_pages",
        "knowledge_inspector_jobs",
        "FORCE ROW LEVEL SECURITY",
        "WITH CHECK",
        "app.tenant_id",
    ):
        assert clause in sql
    assert set(API_PRIVILEGES["knowledge_inspector_jobs"]) == {"SELECT", "INSERT", "UPDATE"}
    assert (SERVICE_ROOT / "migrations/sql/0010_knowledge_details.down.sql").is_file()


def test_knowledge_management_migration_and_runtime_grants() -> None:
    from contractops.infrastructure.postgres.permissions import API_PRIVILEGES

    assert (
        SERVICE_ROOT / "migrations" / "versions" / "20261009_0009_knowledge_management.py"
    ).is_file()
    sql = (SERVICE_ROOT / "migrations/sql/0009_knowledge_management.up.sql").read_text(
        encoding="utf-8"
    )
    assert "ENABLE ROW LEVEL SECURITY" in sql and "FORCE ROW LEVEL SECURITY" in sql
    assert "WITH CHECK" in sql and "app.tenant_id" in sql
    for table in ("knowledge_bases", "knowledge_documents", "knowledge_document_chunks"):
        assert table in sql
        assert "SELECT" in API_PRIVILEGES[table]
        assert "INSERT" in API_PRIVILEGES[table]
    assert (SERVICE_ROOT / "migrations/sql/0009_knowledge_management.down.sql").is_file()


def test_alembic_baseline_files_are_present() -> None:
    assert (SERVICE_ROOT / "alembic.ini").is_file()
    assert (SERVICE_ROOT / "migrations" / "versions" / "20260925_0001_initial.py").is_file()
    assert (SERVICE_ROOT / "migrations" / "sql" / "0001_initial.up.sql").is_file()
    assert (SERVICE_ROOT / "migrations" / "sql" / "0001_initial.down.sql").is_file()
    assert (
        SERVICE_ROOT / "migrations" / "versions" / "20260926_0002_m1_contract_ledger.py"
    ).is_file()
    assert (SERVICE_ROOT / "migrations" / "sql" / "0002_m1_contract_ledger.up.sql").is_file()
    assert (SERVICE_ROOT / "migrations" / "sql" / "0002_m1_contract_ledger.down.sql").is_file()
    assert (
        SERVICE_ROOT / "migrations" / "versions" / "20260926_0003_m2_approval_workflow.py"
    ).is_file()
    assert (SERVICE_ROOT / "migrations" / "sql" / "0003_m2_approval_workflow.up.sql").is_file()
    assert (SERVICE_ROOT / "migrations" / "sql" / "0003_m2_approval_workflow.down.sql").is_file()
    assert (
        SERVICE_ROOT / "migrations" / "versions" / "20260926_0004_m3_reliable_events.py"
    ).is_file()
    assert (SERVICE_ROOT / "migrations" / "sql" / "0004_m3_reliable_events.up.sql").is_file()
    assert (SERVICE_ROOT / "migrations" / "sql" / "0004_m3_reliable_events.down.sql").is_file()
    assert (
        SERVICE_ROOT / "migrations" / "versions" / "20260927_0005_m4_obligation_risk.py"
    ).is_file()
    assert (SERVICE_ROOT / "migrations" / "sql" / "0005_m4_obligation_risk.up.sql").is_file()
    assert (SERVICE_ROOT / "migrations" / "sql" / "0005_m4_obligation_risk.down.sql").is_file()
    assert (
        SERVICE_ROOT / "migrations" / "versions" / "20260927_0006_m5_audit_observability.py"
    ).is_file()
    assert (SERVICE_ROOT / "migrations" / "sql" / "0006_m5_audit_observability.up.sql").is_file()
    assert (SERVICE_ROOT / "migrations" / "sql" / "0006_m5_audit_observability.down.sql").is_file()
    assert (
        SERVICE_ROOT / "migrations" / "versions" / "20260928_0007_m6_document_assistance.py"
    ).is_file()
    assert (SERVICE_ROOT / "migrations" / "sql" / "0007_m6_document_assistance.up.sql").is_file()
    assert (SERVICE_ROOT / "migrations" / "sql" / "0007_m6_document_assistance.down.sql").is_file()
    provisioner = (SERVICE_ROOT / "scripts" / "provision_ci_database.py").read_text(
        encoding="utf-8"
    )
    for table in (
        "contracts",
        "contract_versions",
        "ingestion_jobs",
        "contract_chunks",
        "document_findings",
        "obligations",
        "risk_events",
        "obligation_reminders",
        "audit_events",
    ):
        assert f"ON {table} TO contractops_worker" in provisioner
