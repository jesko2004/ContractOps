from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def test_alembic_baseline_files_are_present() -> None:
    assert (SERVICE_ROOT / "alembic.ini").is_file()
    assert (SERVICE_ROOT / "migrations" / "versions" / "20260925_0001_initial.py").is_file()
    assert (SERVICE_ROOT / "migrations" / "sql" / "0001_initial.up.sql").is_file()
    assert (SERVICE_ROOT / "migrations" / "sql" / "0001_initial.down.sql").is_file()
