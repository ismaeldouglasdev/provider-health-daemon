import json
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture(scope="function")
def fixture_db():
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        conn = sqlite3.connect(tmp.name)

        tables = {
            "message": "CREATE TABLE message (id TEXT PRIMARY KEY, data TEXT, time_created INTEGER)",
            "session": "CREATE TABLE session (id TEXT PRIMARY KEY, model TEXT, time_created INTEGER)",
            "part": "CREATE TABLE part (id TEXT PRIMARY KEY, data TEXT, time_created INTEGER)",
            "session_message": "CREATE TABLE session_message (id TEXT PRIMARY KEY, data TEXT, time_created INTEGER)",
            "event": "CREATE TABLE event (id TEXT PRIMARY KEY, data TEXT, time_created INTEGER)"
        }

        for create_sql in tables.values():
            conn.execute(create_sql)

        aged_epoch = (time.time() - 2 * 3600) * 1000  # > 1 hour old
        fresh_epoch = time.time() * 1000  # Current epoch

        ghost_data = json.dumps({"providerID": "opencode", "variant": "test"})
        # Formato real do opencode.db: JSON compacto, sem espaço após ":"
        ghost_data_compact = '{"role":"user","model":{"providerID":"opencode","modelID":"kimi-k3.","variant":"default"}}'

        conn.execute("INSERT INTO message VALUES ('aged1', ?, ?)", (ghost_data, aged_epoch))
        conn.execute("INSERT INTO message VALUES ('fresh1', ?, ?)", (ghost_data, fresh_epoch))
        conn.execute("INSERT INTO message VALUES ('aged_compact', ?, ?)", (ghost_data_compact, aged_epoch))
        conn.commit()
        
        yield tmp.name

def test_sanitizer(fixture_db):
    script_path = Path(__file__).parent.parent / "scripts/opencode_ghost_sanitizer.py"

    result = subprocess.run([
        "python3", script_path, "--db", fixture_db, "--dry-run"
    ], capture_output=True, check=False)
    assert result.returncode == 0

    with sqlite3.connect(fixture_db) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM message WHERE id='aged1'")
        aged_data = json.loads(cursor.fetchone()[0])
        assert aged_data["providerID"] == "opencode"  # Dry-run shouldn't modify

    result = subprocess.run([
        "python3", script_path, "--db", fixture_db
    ], capture_output=True, check=False)
    assert result.returncode == 0

    with sqlite3.connect(fixture_db) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM message WHERE id='aged1'")
        aged_data = json.loads(cursor.fetchone()[0])
        assert aged_data["providerID"] == "9router"
        assert "variant" not in aged_data

        cursor.execute("SELECT data FROM message WHERE id='aged_compact'")
        compact_data = json.loads(cursor.fetchone()[0])
        assert compact_data["model"]["providerID"] == "9router"
        assert compact_data["model"]["modelID"] == "ollama/gpt-oss:120b"
        assert "variant" not in compact_data["model"]

        cursor.execute("SELECT data FROM message WHERE id='fresh1'")
        fresh_data = json.loads(cursor.fetchone()[0])
        assert fresh_data["providerID"] == "opencode"  # Fresh untouched

    result = subprocess.run([
        "python3", script_path, "--db", fixture_db
    ], capture_output=True, check=False)
    assert result.returncode == 0  # Idempotency check

    with sqlite3.connect(fixture_db) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM message WHERE id='aged1'")
        aged_data = json.loads(cursor.fetchone()[0])
        assert aged_data["providerID"] == "9router"


def test_missing_column(fixture_db):
    script_path = Path(__file__).parent.parent / "scripts/opencode_ghost_sanitizer.py"
    with sqlite3.connect(fixture_db) as conn:
        conn.execute("ALTER TABLE message DROP COLUMN data")

    result = subprocess.run([
        "python3", script_path, "--db", fixture_db
    ], capture_output=True, check=False)
    assert result.returncode == 2