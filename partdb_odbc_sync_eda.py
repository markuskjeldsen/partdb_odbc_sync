#!/usr/bin/env python3
"""
partdb_odbc_sync_eda.py - Extended sync with EDA library support
Handles custom IC symbols/footprints stored as eda.pdf (zip) attachments
"""

import os
import sys
import json
import zipfile
import shutil
import sqlite3
import requests
import logging
from pathlib import Path
from io import BytesIO
import pyodbc

# ─── Configuration ────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "partdb_sync.json"

def load_config(config_path: Path = CONFIG_FILE) -> dict:
    """Load configuration from JSON file, with fallback to env vars."""
    if not config_path.exists():
        logging.warning("Config file %s not found, falling back to environment variables", config_path)
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    logging.debug("Loaded config from %s", config_path)
    return config

# Load JSON config first
_cfg = load_config()

# Resolve each setting: JSON → env var → default
PARTDB_URL      = _cfg.get("server_url",  os.getenv("PARTDB_URL",     "http://localhost:8080"))
PARTDB_API_KEY  = _cfg.get("token",       os.getenv("PARTDB_API_KEY", "your_api_key_here"))
PRICE_QTY       = _cfg.get("price_qty",   int(os.getenv("PRICE_QTY",  "1")))
GROUP_BY        = _cfg.get("group_by",    os.getenv("GROUP_BY",       "category"))
EXCLUDE_FIELDS  = _cfg.get("exclude_fields", [])

KICAD_LIB_DIR   = Path(os.getenv("KICAD_LIB_DIR", str(Path(__file__).parent / "output")))
ODBC_DSN        = os.getenv("ODBC_DSN", "partdb")

# KiCad library names to consolidate into
LIB_SYMBOLS     = KICAD_LIB_DIR / "partdb_custom.kicad_sym"
LIB_FOOTPRINTS  = KICAD_LIB_DIR / "partdb_custom.pretty"
LIB_3D          = KICAD_LIB_DIR / "partdb_custom.3dshapes"

# ─── Logging ──────────────────────────────────────────────────────────────────

# Replace your basicConfig block with:
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
for _h in [logging.StreamHandler(), logging.FileHandler("partdb_eda_sync.log")]:
    _h.setFormatter(_fmt)
    log.addHandler(_h)


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

            if isinstance(data, list):
                parts.extend(data)
                break
            elif isinstance(data, dict):
                batch = data.get("member", [])
                parts.extend(batch)
                if len(parts) >= data.get("totalItems", len(parts)):
                    break
                params["page"] += 1
            else:
                log.error("Unexpected API response type: %s", type(data))
                break

        log.info("Fetched %d parts from PartDB API", len(parts))
        return parts

    def get_part(self, part_id: int) -> dict:
        """Fetch full part details including embedded attachments."""
        url = f"{self.base}/api/parts/{part_id}"
        resp = self.session.get(url)
        resp.raise_for_status()
        return resp.json()

    def get_attachment(self, attachment_id: int) -> dict:
        """Fetch metadata for a single attachment."""
        url = f"{self.base}/api/attachments/{attachment_id}"
        resp = self.session.get(url)
        resp.raise_for_status()
        return resp.json()

    def get_attachments(self, part_id: int) -> list[dict]:
        """
        Return all attachments for a part.
        Attachments are embedded in the full part response.
        """
        part = self.get_part(part_id)
        return part.get("attachments", [])

    def download_attachment(self, attachment_url: str) -> bytes:
        """Download raw attachment bytes."""
        if not attachment_url.startswith("http"):
            attachment_url = self.base + attachment_url
        resp = self.session.get(attachment_url)
        resp.raise_for_status()
        return resp.content

    def find_eda_attachment(self, part: dict) -> dict | None:
        """
        Find the EDA zip attachment from an already-fetched full part dict.
        Looks for internal attachments named 'eda' or with .zip internal_path.
        """
        attachments = part.get("attachments", [])
        for a in attachments:
            name       = a.get("name", "").lower()
            int_path   = a.get("internal_path", "") or ""
            media_url  = a.get("media_url", "") or ""

            if (
                name == "eda"
                or int_path.lower().endswith(".zip")
                or "eda" in Path(int_path).stem.lower()
            ):
                log.debug("  Found EDA attachment: %s → %s", name, int_path or media_url)
                return a
        return None


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

    def extract_symbol(self) -> bytes | None:
        """Return content of the .kicad_sym file if present."""
        hits = self._find((".kicad_sym",))
        if hits:
            return self._zip.read(hits[0])
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

    def list_all(self) -> list[str]:
        with zipfile.ZipFile(BytesIO(self.zip_bytes)) as zf:
            return zf.namelist()

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

    def _read_sym_lib(self) -> str:
        if self.sym_lib.exists():
            return self.sym_lib.read_text(encoding="utf-8")
        return '(kicad_symbol_lib (version 20211014) (generator partdb_sync)\n)'

    def upsert_symbol(self, part_name: str, sym_bytes: bytes) -> bool:
        try:
            new_sym = sym_bytes.decode("utf-8")
        except UnicodeDecodeError:
            log.warning("%s: symbol is not UTF-8, skipping", part_name)
            return False

        inner = self._extract_symbol_blocks(new_sym)
        if not inner:
            log.warning("%s: no symbol block found in file", part_name)
            return False

        lib_content = self._read_sym_lib()
        lib_content = self._remove_symbol_block(lib_content, part_name)

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

    def upsert_footprint(self, part_name: str, mod_bytes: bytes) -> bool:
        dest = self.fp_dir / f"{part_name}.kicad_mod"
        dest.write_bytes(mod_bytes)
        log.info("  ✔ Footprint written: %s", dest)
        return True

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
]

def get_non_standard_parts(db_path: str) -> list[dict]:
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
    fp  = part.get("footprint", "") or ""
    sym = part.get("symbol", "")    or ""
    return not (fp.strip() and sym.strip())


def inspect_sqlite(db_path: str):
    """Debug helper - print all tables and columns in the SQLite DB."""
    conn = sqlite3.connect(db_path)
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cur.fetchall()]
    log.info("SQLite tables found: %s", tables)
    for table in tables:
        cur = conn.execute(f"PRAGMA table_info([{table}])")
        cols = [row[1] for row in cur.fetchall()]
        log.info("  %s columns: %s", table, cols)
        cur = conn.execute(f"SELECT * FROM [{table}] LIMIT 2")
        for row in cur.fetchall():
            log.info("    sample row: %s", dict(zip(cols, row)))
    conn.close()

# ─── Main orchestration ───────────────────────────────────────────────────────

def sync_eda_libraries(
    sqlite_path: str,
    api: PartDBApi,
    lib_mgr: KiCadLibManager,
):
    log.info("═══ Starting EDA library sync ═══")

    # Fetch all parts — but we need full detail (attachments embedded)
    # The list endpoint may return partial data, so fetch each part fully
    log.info("Fetching part list from PartDB API...")
    part_list = api.get_parts()
    log.info("Found %d parts total", len(part_list))

    synced  = 0
    skipped = 0
    errors  = 0

    for stub in part_list:
        part_id = stub.get("id")
        name    = (
            stub.get("name")
            or stub.get("ipn")
            or str(part_id)
        )

        log.info("── Processing: %s (id: %s)", name, part_id)

        # Fetch full part to get embedded attachments
        try:
            full_part = api.get_part(part_id)
        except Exception as exc:
            log.error("  ✘ Failed to fetch part %s: %s", part_id, exc)
            errors += 1
            continue

        eda_attachment = api.find_eda_attachment(full_part)

        if not eda_attachment:
            log.info("  ↷ No EDA attachment found for %s", name)
            skipped += 1
            continue

        # Prefer media_url, fall back to internal_path
        media_url  = eda_attachment.get("media_url") or ""
        int_path   = eda_attachment.get("internal_path") or ""
        dl_url     = media_url if media_url else int_path

        if not dl_url:
            log.error("  ✘ No downloadable URL for EDA attachment on %s", name)
            errors += 1
            continue

        log.info("  ↓ Downloading EDA zip: %s", dl_url)
        try:
            raw = api.download_attachment(dl_url)
        except Exception as exc:
            log.error("  ✘ Download failed for %s: %s", name, exc)
            errors += 1
            continue

        if not zipfile.is_zipfile(BytesIO(raw)):
            log.error("  ✘ Downloaded file is not a zip for %s", name)
            errors += 1
            continue

        extractor = EdaLibExtractor(raw, name)
        log.info("  zip contents: %s", extractor.list_all())

        sym = extractor.extract_symbol()
        log.info("  symbol extracted: %s", "YES" if sym else "NO")

        fp = extractor.extract_footprint()
        log.info("  footprint extracted: %s", "YES" if fp else "NO")

        mdl_fname, mdl_bytes = extractor.extract_3d()
        log.info("  3d model extracted: %s", mdl_fname or "NO")

        changed = False

        sym = extractor.extract_symbol()

        if sym:
            changed |= lib_mgr.upsert_symbol(name, sym)
        else:
            log.warning("  ⚠ No symbol found in zip for %s", name)

        fp = extractor.extract_footprint()
        if fp:
            changed |= lib_mgr.upsert_footprint(name, fp)
        else:
            log.warning("  ⚠ No footprint found in zip for %s", name)


        # Update SQLite so KiCad resolves symbol/footprint from partdb_custom lib
        if sym or fp:
            try:
                db_conn = sqlite3.connect(sqlite_path)
                tables = [r[0] for r in db_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                for tbl in tables:
                    try:
                        db_conn.execute(
                            f"UPDATE [{tbl}] SET Symbols=?, Footprints=? WHERE IPN=?",
                            (f"partdb_custom:{name}", f"partdb_custom:{name}", name)
                        )
                    except sqlite3.OperationalError:
                        pass
                db_conn.commit()
                db_conn.close()
                log.info("  ✔ Updated SQLite for %s → partdb_custom:%s", name, name)
            except Exception as exc:
                log.warning("  ⚠ SQLite update failed for %s: %s", name, exc)

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
    parser.add_argument("--config",  default=str(CONFIG_FILE),
                        help="Path to JSON config file (default: partdb_sync.json)")
    parser.add_argument("--sqlite",  default="output/partdb.sqlite",
                        help="Path to local partdb SQLite file")
    parser.add_argument("--lib-dir", default=str(KICAD_LIB_DIR),
                        help="KiCad library output directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="Discover only, do not write files")
    args = parser.parse_args()

    # Allow config override via CLI arg
    cfg = load_config(Path(args.config))
    url     = cfg.get("server_url", PARTDB_URL)
    api_key = cfg.get("token",      PARTDB_API_KEY)

    log.info("Config: %s", args.config)
    log.info("Server: %s", url)

    api = PartDBApi(url, api_key)

    lib_base = Path(args.lib_dir)
    lib_mgr  = KiCadLibManager(
        sym_lib_path  = lib_base / "partdb_custom.kicad_sym",
        footprint_dir = lib_base / "partdb_custom.pretty",
        model_dir     = lib_base / "partdb_custom.3dshapes",
    )

    if args.dry_run:
        log.info("Dry-run mode: discovery only, no files will be written")
        return

    sync_eda_libraries(args.sqlite, api, lib_mgr)


if __name__ == "__main__":
    main()

