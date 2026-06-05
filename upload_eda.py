#!/usr/bin/env python3
"""
upload_eda.py - Upload a SamacSys zip as 'eda.pdf' attachment to a PartDB part
"""

import sys
import zipfile
import requests
from pathlib import Path

PARTDB_URL     = "http://localhost:8080"
PARTDB_API_KEY = "your_api_key_here"

def find_part_id(session: requests.Session, part_name: str) -> int | None:
    resp = session.get(f"{PARTDB_URL}/api/parts", params={"name": part_name})
    resp.raise_for_status()
    members = resp.json().get("member", [])
    for m in members:
        if m["name"] == part_name:
            return m["id"]
    return None

def upload_eda_zip(part_name: str, zip_path: Path):
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {PARTDB_API_KEY}"

    if not zipfile.is_zipfile(zip_path):
        print(f"ERROR: {zip_path} is not a valid zip file")
        sys.exit(1)

    part_id = find_part_id(session, part_name)
    if not part_id:
        print(f"ERROR: part '{part_name}' not found in PartDB")
        sys.exit(1)

    print(f"Uploading {zip_path} → part #{part_id} ({part_name})")

    with open(zip_path, "rb") as fh:
        resp = session.post(
            f"{PARTDB_URL}/api/parts/{part_id}/attachments",
            data={
                "name":         "eda",
                "type":         "other",   # or whatever attachment type ID
            },
            files={
                # Named eda.pdf so PartDB accepts it; content is a zip
                "file": ("eda.pdf", fh, "application/pdf"),
            },
        )

    if resp.status_code in (200, 201):
        print(f"✔ Uploaded successfully: {resp.json().get('@id')}")
    else:
        print(f"✘ Upload failed [{resp.status_code}]: {resp.text}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: upload_eda.py <PART_NAME> <path/to/eda.zip>")
        sys.exit(1)
    upload_eda_zip(sys.argv[1], Path(sys.argv[2]))

