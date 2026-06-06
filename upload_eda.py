#!/usr/bin/env python3
"""
upload_eda.py - Upload a SamacSys zip as 'eda' attachment to a PartDB part
"""

import sys
import json
import requests
import base64
from pathlib import Path

config = json.loads(Path("partdb_sync.json").read_text())
PARTDB_URL     = config["server_url"].rstrip("/")
PARTDB_API_KEY = config["token"]


def get_session() -> requests.Session:
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {PARTDB_API_KEY}"
    return session


def find_part_id(session: requests.Session, part_name: str) -> int | None:
    resp = session.get(f"{PARTDB_URL}/api/parts", params={"filter[name]": part_name})
    resp.raise_for_status()
    for part in resp.json().get("hydra:member", []):
        if part["name"] == part_name:
            return part["id"]
    return None


def find_existing_eda_attachment(session: requests.Session, part_id: int) -> int | None:
    """Return attachment id if an 'eda' attachment already exists for this part."""
    resp = session.get(f"{PARTDB_URL}/api/attachments", params={"filter[name]": "eda"})
    resp.raise_for_status()
    for att in resp.json().get("hydra:member", []):
        if att.get("name") == "eda" and att.get("element", {}).get("id") == part_id:
            return att["id"]
    return None


def upload_eda_zip(part_name: str, zip_path: Path):
    session = get_session()

    part_id = find_part_id(session, part_name)
    if part_id is None:
        print(f"ERROR: part '{part_name}' not found in PartDB")
        sys.exit(1)

    print(f"Uploading {zip_path} → part #{part_id} ({part_name})")

    file_b64 = base64.b64encode(zip_path.read_bytes()).decode()
    upload_payload = {
        "upload": {
            "data": file_b64,
            "filename": zip_path.name,
        }
    }

    existing_id = find_existing_eda_attachment(session, part_id)

    if existing_id:
        print(f"  Replacing existing eda attachment #{existing_id}")
        session.headers["Content-Type"] = "application/merge-patch+json"
        resp = session.patch(
            f"{PARTDB_URL}/api/attachments/{existing_id}",
            json=upload_payload,
        )
    else:
        resp = session.post(
            f"{PARTDB_URL}/api/attachments",
            json={
                "name":            "eda",
                "attachment_type": "/api/attachment_types/2",
                "element":         f"/api/parts/{part_id}",
                **upload_payload,
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

