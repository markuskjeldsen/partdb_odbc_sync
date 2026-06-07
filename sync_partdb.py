"""
Sync Part-DB server to a local SQLite database for KiCad database library use.

Usage:
    python sync_partdb.py [--config partdb_sync.json]

Uses the full Part-DB REST API (/api/parts/) to get:
  - Part metadata, EDA info (symbol, footprint, reference)
  - Parameters (electrical specs)
  - Pricing at configurable quantity break (default: 1000 pcs)
  - Stock levels
  - Supplier part numbers and URLs
  - Datasheet URLs

Updates the database in-place (WAL mode) so KiCad doesn't need to be closed.
Just re-open the symbol chooser to see updated data.
"""

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
import urllib.error

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(SCRIPT_DIR, "partdb_sync.json")
DB_PATH = os.path.join(SCRIPT_DIR, "output", "partdb.sqlite")
DBL_PATH = os.path.join(SCRIPT_DIR, "partdb.kicad_dbl")

# Key column used as the part identifier in KiCad's symbol chooser.
# Uses IPN (Internal Part Number) from Part-DB, falls back to MPN if empty.
KEY_COLUMN = "IPN"

# Standard columns always present in every table
STANDARD_COLUMNS = [
    "IPN",
    "Name",
    "Description",
    "Keywords",
    "Symbols",
    "Footprints",
    "Value",
    "Reference",
    "Category",
    "Manufacturer",
    "MPN",
    "Datasheet",
    "PartDbUrl",
    "Stock",
    "Price_USD",
    "Supplier",
    "Supplier_PN",
    "Supplier_URL",
]


def load_config(path):
    with open(path, "r") as f:
        return json.load(f)


def api_get(server_url, token, endpoint):
    """GET a JSON endpoint from Part-DB REST API."""
    url = server_url.rstrip("/") + "/" + endpoint.lstrip("/")
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "PartDB-KiCad-Sync/1.0")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} fetching {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  Error fetching {url}: {e}", file=sys.stderr)
        return None


def sanitize_column_name(name):
    """Convert a string to a valid SQLite column name."""
    name = name.lower()
    name = name.replace("/", "_").replace("\\", "_").replace("-", "_")
    name = name.replace(" ", "_")
    name = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
    while "__" in name:
        name = name.replace("__", "_")
    return name.strip("_")


def sanitize_table_name(name):
    """Convert a category path like 'ICs -> ICs Power' to a valid table name."""
    if "\u2192" in name:
        name = name.split("\u2192")[-1].strip()
    elif "/" in name:
        name = name.split("/")[-1].strip()
    return sanitize_column_name(name)


def find_price_at_qty(orderdetails, target_qty):
    """Find the best price at the target quantity break.

    Walks all suppliers' price breaks and picks the one whose
    min_discount_quantity is closest to (but not exceeding) target_qty.
    """
    best_price = None
    best_supplier = {}

    for od in orderdetails:
        if od.get("obsolete"):
            continue
        matched_pd = None
        for pd in sorted(od.get("pricedetails", []),
                         key=lambda x: x.get("min_discount_quantity", 0)):
            if pd.get("min_discount_quantity", 0) <= target_qty:
                matched_pd = pd
            else:
                break

        if matched_pd:
            price = float(matched_pd.get("price", 0))
            if best_price is None or price < best_price:
                best_price = price
                best_supplier = {
                    "price": matched_pd.get("price", ""),
                    "name": (od.get("supplier") or {}).get("name", ""),
                    "pn": od.get("supplierpartnr", ""),
                    "url": od.get("supplier_product_url", ""),
                }

    return best_supplier


def extract_part(part, server_url, param_names, price_qty):
    """Extract a flat row dict from a full /api/parts/{id} response."""
    eda = part.get("eda_info") or {}
    cat = part.get("category") or {}
    mfr = part.get("manufacturer") or {}

    supplier = find_price_at_qty(part.get("orderdetails", []), price_qty)

    # Find the first datasheet attachment
    datasheet_url = ""
    for att in part.get("attachments", []):
        if not att.get("picture") and not att.get("3d_model"):
            datasheet_url = att.get("external_path") or att.get("media_url") or ""
            if datasheet_url:
                break

    mpn = part.get("manufacturer_product_number", "") or part.get("name", "")
    ipn = part.get("ipn", "")

    # Fall back to MPN if IPN is not set in Part-DB
    if not ipn:
        ipn = mpn

    row = {
        "IPN": ipn,
        "Name": part.get("name", ""),
        "Description": part.get("description", ""),
        "Keywords": "",
        "Symbols": eda.get("kicad_symbol", ""),
        "Footprints": eda.get("kicad_footprint", ""),
        "Value": eda.get("value", "") or part.get("name", ""),
        "Reference": eda.get("reference_prefix", ""),
        "Category": cat.get("full_path", cat.get("name", "")),
        "Manufacturer": mfr.get("name", ""),
        "MPN": mpn,
        "Datasheet": datasheet_url,
        "PartDbUrl": f"{server_url.rstrip('/')}/en/part/{part['id']}/info",
        "Stock": str(int(part.get("total_instock", 0))),
        "Price_USD": supplier.get("price", ""),
        "Supplier": supplier.get("name", ""),
        "Supplier_PN": supplier.get("pn", ""),
        "Supplier_URL": supplier.get("url", ""),
    }

    # Extract parameters as additional columns
    for p in part.get("parameters", []):
        col = sanitize_column_name(p["name"])
        if col in param_names:
            row[col] = p.get("formatted", p.get("value_text", ""))

    return row


def fetch_all_parts(server_url, token):
    """Fetch all parts using pagination from /api/parts."""
    all_parts = []
    page = 1
    while True:
        data = api_get(server_url, token, f"api/parts?page={page}&itemsPerPage=50")
        if not data:
            break

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("hydra:member", [])
        else:
            break

        if not items:
            break

        all_parts.extend(items)
        print(f"  Page {page}: {len(items)} parts (total: {len(all_parts)})")

        if isinstance(data, dict):
            next_page = (data.get("hydra:view") or {}).get("hydra:next")
            if not next_page:
                break
        else:
            break
        page += 1

    return all_parts


def discover_param_names(parts):
    """Collect all unique parameter names across all parts."""
    names = set()
    for part in parts:
        for p in part.get("parameters", []):
            names.add(sanitize_column_name(p["name"]))
    return sorted(names)


def group_by_category(parts):
    """Group parts by their category into {table_name: [parts]}."""
    groups = {}
    for part in parts:
        cat = part.get("category") or {}
        cat_name = cat.get("name", "Uncategorized")
        table_name = sanitize_table_name(cat_name)
        if table_name not in groups:
            groups[table_name] = []
        groups[table_name].append(part)
    return groups


def all_parts_flat(parts):
    """Put all parts in a single flat table."""
    return {"Parts": parts}


def ensure_table(cur, table_name, all_columns):
    """Create or recreate table. Drops and recreates if schema changed."""
    col_defs = ", ".join(f'"{c}" TEXT' for c in all_columns)
    target_schema = f'CREATE TABLE "{table_name}" ({col_defs}, PRIMARY KEY("{KEY_COLUMN}"))'

    cur.execute(f'SELECT sql FROM sqlite_master WHERE type="table" AND name=?', (table_name,))
    row = cur.fetchone()
    if row:
        existing_sql = row[0]
        if existing_sql != target_schema:
            cur.execute(f'DROP TABLE "{table_name}"')
            cur.execute(target_schema)
    else:
        cur.execute(target_schema)

    cur.execute(f'PRAGMA table_info("{table_name}")')
    existing = {r[1] for r in cur.fetchall()}
    for col in all_columns:
        if col not in existing:
            cur.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{col}" TEXT DEFAULT ""')


def upsert_parts(cur, table_name, columns, rows):
    """Insert or update part rows."""
    placeholders = ", ".join("?" for _ in columns)
    col_names = ", ".join(f'"{c}"' for c in columns)
    sql = f'INSERT OR REPLACE INTO "{table_name}" ({col_names}) VALUES ({placeholders})'
    for row in rows:
        values = [row.get(col, "") for col in columns]
        cur.execute(sql, values)


def generate_kicad_dbl(table_names, param_columns, exclude_fields, price_qty):
    """Generate the .kicad_dbl config file matching the synced tables."""
    libraries = []

    price_label = f"Price_USD@{price_qty}"

    common_fields = [
        ("Value",        "Value",       True,  True,  False),
        ("Manufacturer", "Manufacturer", False, True,  True),
        ("MPN",          "MPN",          False, True,  True),
        ("Category",     "Category",     False, True,  True),
        ("Stock",        "Stock",        False, True,  True),
        ("Price_USD",    price_label,    False, True,  True),
        ("Supplier",     "Supplier",     False, True,  True),
        ("Supplier_PN",  "Supplier_PN",  False, True,  True),
        ("Supplier_URL", "Supplier_URL", False, False, False),
        ("Datasheet",    "Datasheet",    False, False, False),
        ("PartDbUrl",    "PartDbUrl",    False, False, False),
    ]

    for table_name in table_names:
        fields = []
        for col, name, vis_add, vis_choose, show_nm in common_fields:
            if col in exclude_fields:
                continue
            entry = {
                "column": col,
                "name": name,
                "visible_on_add": vis_add,
                "visible_in_chooser": vis_choose,
            }
            if show_nm:
                entry["show_name"] = True
            fields.append(entry)

        for col in param_columns:
            if col in exclude_fields:
                continue
            fields.append({
                "column": col,
                "name": col.replace("_", " "),
                "visible_on_add": False,
                "visible_in_chooser": False,
                "show_name": True,
            })

        libraries.append({
            "name": table_name,
            "table": table_name,
            "key": KEY_COLUMN,
            "symbols": "Symbols",
            "footprints": "Footprints",
            "fields": fields,
            "properties": {
                "description": "Description",
                "keywords": "Keywords",
            },
        })

    return {
        "meta": {"version": 0},
        "name": "Part-DB Library",
        "description": "Components synced from Part-DB server",
        "source": {
            "type": "odbc",
            "dsn": "",
            "username": "",
            "password": "",
            "timeout_seconds": 2,
            "connection_string": "Driver={SQLite3};Database=${CWD}/output/partdb.sqlite",
        },
        "globally_unique_keys": True,
        "libraries": libraries,
    }


def resolve_config(args):
    """Build effective config from file + env vars + CLI args.

    Priority (highest wins): CLI args > env vars > config file.
    """
    config = load_config(args.config)

    # Server URL: --server > PARTDB_URL > config file
    server_url = args.server or os.environ.get("PARTDB_URL") or config.get("server_url", "")
    if not server_url:
        print("Error: server_url not set. Use --server, PARTDB_URL env var, or set it in the config file.",
              file=sys.stderr)
        sys.exit(1)

    # Token: --token > PARTDB_TOKEN > config file
    token = args.token or os.environ.get("PARTDB_TOKEN") or config.get("token", "")

    return {
        "server_url": server_url.rstrip("/"),
        "token": token,
        "price_qty": config.get("price_qty", 1000),
        "group_by": config.get("group_by", "category"),
        "exclude_fields": config.get("exclude_fields", []),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Sync Part-DB to local SQLite for KiCad",
        epilog="Token and server URL can also be set via PARTDB_TOKEN and PARTDB_URL environment variables.",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to partdb_sync.json")
    parser.add_argument("--server", help="Part-DB server URL (overrides config file and PARTDB_URL)")
    parser.add_argument("--token", help="Part-DB API token (overrides config file and PARTDB_TOKEN)")
    args = parser.parse_args()

    config = resolve_config(args)
    server_url = config["server_url"]
    token = config["token"]
    exclude_fields = set(config["exclude_fields"])
    price_qty = config["price_qty"]

    print(f"Price quantity break: {price_qty} pcs")

    # 1. Fetch all parts via REST API
    print("Fetching all parts from Part-DB API...")
    parts_list = fetch_all_parts(server_url, token)
    if not parts_list:
        print("No parts found. Check config and API permissions.", file=sys.stderr)
        sys.exit(1)

    # Fetch full details for each part
    print(f"\nFetching full details for {len(parts_list)} parts...")
    full_parts = []
    for p in parts_list:
        pid = p.get("id")
        detail = api_get(server_url, token, f"api/parts/{pid}")
        if detail:
            full_parts.append(detail)
            name = detail.get("name", str(pid))
            cat = (detail.get("category") or {}).get("name", "?")
            print(f"  [{cat}] {name}")

    # 2. Discover all parameter names
    param_columns = discover_param_names(full_parts)
    print(f"\nDiscovered {len(param_columns)} unique parameters")

    # 3. Group parts
    group_mode = config.get("group_by", "category")
    if group_mode == "category":
        grouped = group_by_category(full_parts)
        print(f"Grouped into {len(grouped)} categories")
    else:
        grouped = all_parts_flat(full_parts)
        print("All parts in single table")

    # 4. Build rows
    all_columns = list(STANDARD_COLUMNS) + param_columns
    table_rows = {}
    for table_name, parts in grouped.items():
        rows = [extract_part(p, server_url, set(param_columns), price_qty) for p in parts]
        table_rows[table_name] = rows

    # 5. Write SQLite in-place (WAL mode allows concurrent reads by KiCad)
    print(f"\nUpdating {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()

    table_names = []
    for table_name, rows in sorted(table_rows.items()):
        ensure_table(cur, table_name, all_columns)
        upsert_parts(cur, table_name, all_columns, rows)
        table_names.append(table_name)
        print(f"  {table_name}: {len(rows)} parts")

    conn.commit()
    conn.close()

    # 6. Generate .kicad_dbl
    print(f"Writing {DBL_PATH}...")
    dbl = generate_kicad_dbl(table_names, param_columns, exclude_fields, price_qty)
    with open(DBL_PATH, "w") as f:
        json.dump(dbl, f, indent=4)

    print("\nDone! Re-open the symbol chooser in KiCad to see updates.")


if __name__ == "__main__":
    main()
