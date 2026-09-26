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
