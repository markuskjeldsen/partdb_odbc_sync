#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Config - edit these or export them before running
export PARTDB_URL="${PARTDB_URL:-http://localhost:8080}"
export PARTDB_API_KEY="${PARTDB_API_KEY:-your_api_key_here}"
export KICAD_LIB_DIR="${KICAD_LIB_DIR:-$HOME/kicad/libraries}"

case "${1:-sync}" in
    sync)
        uv run python sync_partdb.py
        ;;
    eda)
        uv run python partdb_odbc_sync_eda.py --sqlite partdb.sqlite
        ;;
    upload)
        # Usage: ./run.sh upload POWERSTEP01TR ~/Downloads/LIB_POWERSTEP01TR.zip
        uv run python upload_eda.py "${2}" "${3}"
        ;;
    all)
        uv run python sync_partdb.py
        uv run python partdb_odbc_sync_eda.py --sqlite partdb.sqlite
        ;;
    *)
        echo "Usage: $0 {sync|eda|upload <PART_NAME> <zip_path>|all}"
        exit 1
        ;;
esac
