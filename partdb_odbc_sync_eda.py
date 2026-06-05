#!/usr/bin/env python3
"""
partdb_odbc_sync_eda.py - Extended sync with EDA library support
Handles custom IC symbols/footprints stored as eda.pdf (zip) attachments
"""

import os
import sys
import zipfile
import shutil
import sqlite3
import requests
import logging
from pathlib import Path
from io import BytesIO
import pyodbc

# ─── Configuration ────────────────────────────────────────────────────────────

PARTDB_URL      = os.getenv("PARTDB_URL",      "http://localhost:8080")
PARTDB_API_KEY  = os.getenv("PARTDB_API_KEY",  "your_api_key_here")
KICAD_LIB_DIR   = Path(os.getenv("KICAD_LIB_DIR", "~/kicad/libraries")).expanduser()
ODBC_DSN        = os.getenv("ODBC_DSN",        "partdb")

# KiCad library names to consolidate into
LIB_SYMBOLS     = KICAD_LIB_DIR / "partdb_custom.kicad_sym"
LIB_FOOTPRINTS  = KICAD_LIB_DIR / "partdb_custom.pretty"
LIB_3D          = KICAD_LIB_DIR / "partdb_custom.3dshapes"

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("partdb_eda_sync.log"),
    ],
)
log = logging.getLogger(__name__)

# ─── PartDB API helpers ───────────────────────────────────────────────────────

class PartDBApi:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        })

    def get_parts(self, category: str = None) -> list[dict]:
        """Fetch all parts, optionally filtered by category name."""
        url = f"{self.base}/api/parts"
        params = {"limit": 500, "page": 1}
        parts = []
        while True:
            resp = self.session.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            parts.extend(data.get("member", []))
            if len(parts) >= data.get("totalItems", 0):
                break
            params["page"] += 1
        return parts

    def get_attachments(self, part_id: int) -> list[dict]:
        """Return all attachments for a given part ID."""
        url = f"{self.base}/api/parts/{part_id}/attachments"
        resp = self.session.get(url)
        resp.raise_for_status()
        return resp.json().get("member", [])

    def download_attachment(self, attachment_url: str) -> bytes:
        """Download raw attachment bytes."""
        # Attachment URL may be relative
        if not attachment_url.startswith("http"):
            attachment_url = self.base + attachment_url
        resp = self.session.get(attachment_url)
        resp.raise_for_status()
        return resp.content

# ─── EDA zip extraction ───────────────────────────────────────────────────────

class EdaLibExtractor:
    """
    Extracts KiCad assets from a SamacSys-style zip that contains:
      PARTNAME/
        KiCad/
          *.kicad_sym
          *.kicad_mod  (or *.mod)
          *.lib
          *.dcm
        3D/
          *.stp
    """

    def __init__(self, zip_bytes: bytes, part_name: str):
        self.zip_bytes = zip_bytes
        self.part_name = part_name
        self._zip = zipfile.ZipFile(BytesIO(zip_bytes))

    def _find(self, extensions: tuple) -> list[str]:
        return [
            n for n in self._zip.namelist()
            if n.lower().endswith(extensions)
        ]

    # ── Public extract methods ─────────────────────────────────────────────

    def extract_symbol(self) -> bytes | None:
        """Return content of the .kicad_sym file if present."""
        hits = self._find((".kicad_sym",))
        if hits:
            return self._zip.read(hits[0])
        # Fall back to legacy .lib
        hits = self._find((".lib",))
        if hits:
            return self._zip.read(hits[0])
        return None

    def extract_footprint(self) -> bytes | None:
        """Return content of the .kicad_mod file if present."""
        hits = self._find((".kicad_mod", ".mod"))
        if hits:
            return self._zip.read(hits[0])
        return None

    def extract_3d(self) -> tuple[str, bytes] | tuple[None, None]:
        """Return (filename, content) for the .stp file if present."""
        hits = self._find((".stp", ".step"))
        if hits:
            fname = Path(hits[0]).name
            return fname, self._zip.read(hits[0])
        return None, None

    def list_all(self) -> list[str]:
        return self._zip.namelist()

# ─── KiCad library writers ────────────────────────────────────────────────────

class KiCadLibManager:
    """
    Manages a consolidated KiCad 6+ symbol library and .pretty footprint folder.
    Idempotent: re-running will update existing entries.
    """

    def __init__(
        self,
        sym_lib_path: Path,
        footprint_dir: Path,
        model_dir: Path,
    ):
        self.sym_lib  = sym_lib_path
        self.fp_dir   = footprint_dir
        self.mdl_dir  = model_dir
        footprint_dir.mkdir(parents=True, exist_ok=True)
        model_dir.mkdir(parents=True, exist_ok=True)

    # ── Symbol library ────────────────────────────────────────────────────

    def _read_sym_lib(self) -> str:
        if self.sym_lib.exists():
            return self.sym_lib.read_text(encoding="utf-8")
        return '(kicad_symbol_lib (version 20211014) (generator partdb_sync)\n)'

    def upsert_symbol(self, part_name: str, sym_bytes: bytes) -> bool:
        """
        Insert or replace a symbol block inside the consolidated library.
        Returns True if changed.
        """
        try:
            new_sym = sym_bytes.decode("utf-8")
        except UnicodeDecodeError:
            log.warning("%s: symbol is not UTF-8, skipping", part_name)
            return False

        # Extract the inner (symbol ...) block(s) from the downloaded file
        inner = self._extract_symbol_blocks(new_sym)
        if not inner:
            log.warning("%s: no symbol block found in file", part_name)
            return False

        lib_content = self._read_sym_lib()

        # Remove old entry if present
        lib_content = self._remove_symbol_block(lib_content, part_name)

        # Inject before closing paren
        insertion_point = lib_content.rfind(")")
        lib_content = (
            lib_content[:insertion_point]
            + "\n"
            + inner
            + "\n)"
        )
        self.sym_lib.write_text(lib_content, encoding="utf-8")
        log.info("  ✔ Symbol upserted: %s", part_name)
        return True

    def _extract_symbol_blocks(self, content: str) -> str:
        """Pull out (symbol ...) lines that are direct children of kicad_symbol_lib."""
        lines = []
        depth = 0
        capturing = False
        for line in content.splitlines():
            stripped = line.strip()
            if not capturing and stripped.startswith("(symbol "):
                capturing = True
                depth = 0
            if capturing:
                lines.append(line)
                depth += line.count("(") - line.count(")")
                if depth <= 0:
                    capturing = False
        return "\n".join(lines)

    def _remove_symbol_block(self, content: str, part_name: str) -> str:
        """Remove an existing symbol block matching part_name."""
        marker = f'(symbol "{part_name}"'
        if marker not in content:
            return content
        start = content.index(marker)
        depth = 0
        i = start
        while i < len(content):
            if content[i] == "(":
                depth += 1
            elif content[i] == ")":
                depth -= 1
                if depth == 0:
                    return content[:start] + content[i+1:]
            i += 1
        return content

    # ── Footprint ─────────────────────────────────────────────────────────

    def upsert_footprint(self, part_name: str, mod_bytes: bytes) -> bool:
        dest = self.fp_dir / f"{part_name}.kicad_mod"
        dest.write_bytes(mod_bytes)
        log.info("  ✔ Footprint written: %s", dest)
        return True

    # ── 3D model ──────────────────────────────────────────────────────────

    def upsert_3d_model(self, fname: str, model_bytes: bytes) -> Path:
        dest = self.mdl_dir / fname
        dest.write_bytes(model_bytes)
        log.info("  ✔ 3D model written: %s", dest)
        return dest

# ─── ODBC / SQLite helpers ────────────────────────────────────────────────────

NON_STANDARD_TABLES = [
    "amplifiers",
    "gate_drivers",
    "motor_drivers_controllers",
    "voltage_regulators_dc_dc_switching_regulators",
    # add more as needed
]

def get_non_standard_parts(db_path: str) -> list[dict]:
    """
    Query local SQLite (partdb.sqlite) to get parts that live in
    non-standard (IC) tables – these are candidates for EDA sync.
    Returns list of {table, name} dicts.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    parts = []
    for table in NON_STANDARD_TABLES:
        try:
            cur = conn.execute(f"SELECT * FROM [{table}]")
            for row in cur:
                parts.append({"table": table, **dict(row)})
        except sqlite3.OperationalError as exc:
            log.debug("Table %s not found: %s", table, exc)
    conn.close()
    return parts


def needs_eda_sync(part: dict) -> bool:
    """
    A part needs EDA sync if:
      - It has no footprint/symbol set in the EDA fields, AND
      - (We'll check for eda.pdf attachment via API)
    """
    fp  = part.get("footprint", "") or ""
    sym = part.get("symbol", "")    or ""
    return not (fp.strip() and sym.strip())

# ─── Main orchestration ───────────────────────────────────────────────────────

def sync_eda_libraries(
    sqlite_path: str,
    api: PartDBApi,
    lib_mgr: KiCadLibManager,
):
    log.info("═══ Starting EDA library sync ═══")
    local_parts = get_non_standard_parts(sqlite_path)
    log.info("Found %d non-standard parts in local DB", len(local_parts))

    # Build a name→id map from PartDB API
    all_api_parts = api.get_parts()
    name_to_api = {p["name"]: p for p in all_api_parts}

    synced  = 0
    skipped = 0
    errors  = 0

    for part in local_parts:
        name = part["name"]
        log.info("── Processing: %s (table: %s)", name, part["table"])

        if not needs_eda_sync(part):
            log.info("  ↷ EDA fields already set, skipping")
            skipped += 1
            continue

        api_part = name_to_api.get(name)
        if not api_part:
            log.warning("  ✘ Not found in PartDB API: %s", name)
            errors += 1
            continue

        part_id = api_part["id"]
        attachments = api.get_attachments(part_id)

        # Find the eda.pdf attachment (our disguised zip)
        eda_attachment = next(
            (
                a for a in attachments
                if Path(a.get("name", "")).stem.lower() == "eda"
                or a.get("filename", "").lower() in ("eda.pdf", "eda.zip")
            ),
            None,
        )

        if not eda_attachment:
            log.info("  ↷ No eda attachment found for %s", name)
            skipped += 1
            continue

        log.info("  ↓ Downloading EDA attachment for %s", name)
        try:
            raw = api.download_attachment(
                eda_attachment.get("url") or eda_attachment.get("@id")
            )
        except Exception as exc:
            log.error("  ✘ Download failed: %s", exc)
            errors += 1
            continue

        # Verify it's actually a zip
        if not zipfile.is_zipfile(BytesIO(raw)):
            log.error("  ✘ Attachment is not a zip file for %s", name)
            errors += 1
            continue

        extractor = EdaLibExtractor(raw, name)
        log.debug("  zip contents: %s", extractor.list_all())

        changed = False

        sym = extractor.extract_symbol()
        if sym:
            changed |= lib_mgr.upsert_symbol(name, sym)
        else:
            log.warning("  ⚠ No symbol found for %s", name)

        fp = extractor.extract_footprint()
        if fp:
            changed |= lib_mgr.upsert_footprint(name, fp)
        else:
            log.warning("  ⚠ No footprint found for %s", name)

        mdl_fname, mdl_bytes = extractor.extract_3d()
        if mdl_fname:
            lib_mgr.upsert_3d_model(mdl_fname, mdl_bytes)

        if changed:
            synced += 1
        else:
            skipped += 1

    log.info("═══ Sync complete: %d synced, %d skipped, %d errors ═══",
             synced, skipped, errors)


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="PartDB EDA library sync")
    parser.add_argument("--sqlite",  default="partdb.sqlite",
                        help="Path to local partdb SQLite file")
    parser.add_argument("--lib-dir", default=str(KICAD_LIB_DIR),
                        help="KiCad library output directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="Discover only, do not write files")
    args = parser.parse_args()

    api = PartDBApi(PARTDB_URL, PARTDB_API_KEY)

    lib_base = Path(args.lib_dir)
    lib_mgr  = KiCadLibManager(
        sym_lib_path  = lib_base / "partdb_custom.kicad_sym",
        footprint_dir = lib_base / "partdb_custom.pretty",
        model_dir     = lib_base / "partdb_custom.3dshapes",
    )

    sync_eda_libraries(args.sqlite, api, lib_mgr)


if __name__ == "__main__":
    main()

