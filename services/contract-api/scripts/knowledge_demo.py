"""Run the local SQLite knowledge, contract and human approval workspace."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn


def main() -> None:
    # Run the checkout, even when the virtual environment contains an older wheel.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from contractops.demo.knowledge import create_demo_app

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--inspector-url")
    parser.add_argument("--inspector-key-file", type=Path)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(__file__).resolve().parents[3] / "data" / "knowledge-demo.sqlite3",
    )
    options = parser.parse_args()
    if not 1 <= options.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if options.inspector_url:
        os.environ["CONTRACTOPS_INSPECTOR_URL"] = options.inspector_url
    if options.inspector_key_file:
        os.environ["CONTRACTOPS_INSPECTOR_API_KEY"] = options.inspector_key_file.read_text(
            encoding="utf-8"
        ).strip()
    application = create_demo_app(options.database, token=os.getenv("CONTRACTOPS_LOCAL_TOKEN"))
    print(f"ContractOps workspace (local SQLite): http://127.0.0.1:{options.port}")
    print("Paste this temporary bearer token into the page (changes on restart):")
    print(application.state.demo_token)
    print(
        "Shared main application/API, SQLite local adapter. "
        "Local owner/reviewer switching is for demonstrations, not enterprise authentication."
    )
    uvicorn.run(application, host="127.0.0.1", port=options.port, access_log=False)


if __name__ == "__main__":
    main()
