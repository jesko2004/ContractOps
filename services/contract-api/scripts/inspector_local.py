"""Run an existing PDF Inspector checkout, keeping its runtime data in this project."""

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument(
        "--data", type=Path, default=Path(__file__).resolve().parents[3] / "data/inspector-local"
    )
    parser.add_argument("--ocr-provider", choices=("none", "rapidocr"), default="none")
    options = parser.parse_args()
    if not (options.repo / "backend/app.py").is_file() or not 1 <= options.port <= 65535:
        parser.error("existing Inspector checkout and valid port required")
    options.data.mkdir(parents=True, exist_ok=True)
    key_file = options.data / "service-key.txt"
    if not key_file.exists():
        with key_file.open("x", encoding="utf-8") as stream:
            stream.write(secrets.token_urlsafe(40))
    key = key_file.read_text(encoding="utf-8").strip()
    if len(key) < 32:
        parser.error("local service key must contain at least 32 characters")
    os.environ["PDF_INSPECTOR_DATA_DIR"] = str(options.data.resolve())
    os.environ["PDF_INSPECTOR_API_KEYS_JSON"] = json.dumps(
        [{"id": "contractops", "key": key, "role": "write"}]
    )
    os.environ["PDF_INSPECTOR_OCR_PROVIDER"] = options.ocr_provider
    sys.path.insert(0, str(options.repo.resolve()))
    print(f"Inspector: http://127.0.0.1:{options.port}; OCR: {options.ocr_provider}")
    print(f"Private service key file: {key_file}")
    uvicorn.run("backend.app:create_app", factory=True, host="127.0.0.1", port=options.port)


if __name__ == "__main__":
    main()
