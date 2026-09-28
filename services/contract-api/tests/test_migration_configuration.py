from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]


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
    assert (
        SERVICE_ROOT / "migrations" / "sql" / "0006_m5_audit_observability.down.sql"
    ).is_file()
    assert (
        SERVICE_ROOT / "migrations" / "versions" / "20260928_0007_m6_document_assistance.py"
    ).is_file()
    assert (SERVICE_ROOT / "migrations" / "sql" / "0007_m6_document_assistance.up.sql").is_file()
    assert (
        SERVICE_ROOT / "migrations" / "sql" / "0007_m6_document_assistance.down.sql"
    ).is_file()
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
