#!/usr/bin/python3
import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Any


def process_data(data: Any) -> Any:
    if isinstance(data, dict):
        if data.get("providerID") == "opencode":
            data["providerID"] = "9router"
            if "id" in data:
                data["id"] = "ollama/gpt-oss:120b"
            if "modelID" in data:
                data["modelID"] = "ollama/gpt-oss:120b"
            data.pop("variant", None)
        for k, v in list(data.items()):
            data[k] = process_data(v)
    elif isinstance(data, list):
        data = [process_data(x) for x in data]
    return data

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/home/ismaeldev/.local/share/opencode/opencode.db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        conn = sqlite3.connect(args.db)
        conn.execute("PRAGMA busy_timeout = 60000")
    except sqlite3.Error as e:
        sys.stderr.write(f"Error connecting to DB: {e}\n")
        sys.exit(1)

    # event EXCLUÍDO: sem coluna de timestamp no schema real — pela regra
    # conservadora, row sem idade comprovada nunca é limpa (e eventos não
    # são lidos na retomada de sessão).
    tables = {
        "message": ("data", "time_created"),
        "session": ("model", "time_created"),
        "part": ("data", "time_created"),
        "session_message": ("data", "time_created"),
    }

    cursor = conn.cursor()
    # Check schema
    for table, (col, time_col) in tables.items():
        res = cursor.execute(f"PRAGMA table_info({table})").fetchall()
        columns = [row[1] for row in res]
        if col not in columns or time_col not in columns:
            sys.stderr.write(f"Schema drift: table {table} missing {col} or {time_col}\n")
            sys.exit(2)

    now = int(time.time() * 1000)
    cutoff = now - 3600 * 1000
    
    log_file = os.path.expanduser("~/.9router/ghost-sanitizer.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    
    like_ghost = '%"' + 'providerID":"' + 'opencode"' + '%'  # JSON compacto
    like_ghost_sp = '%"providerID": "opencode"%'  # variante com espaço
    for table, (col, time_col) in tables.items():
        try:
            cursor.execute(
                f"SELECT rowid, {col} FROM {table} WHERE "
                f"({col} LIKE ? OR {col} LIKE ?) AND {time_col} < ?",
                (like_ghost, like_ghost_sp, cutoff),
            )
            rows = cursor.fetchall()
            
            for i in range(0, len(rows), 1000):
                batch = rows[i:i+1000]
                if not args.dry_run:
                    conn.execute("BEGIN")
                
                for rowid, raw_data in batch:
                    if not raw_data:
                        continue
                    try:
                        data = json.loads(raw_data)
                        new_data = process_data(data)
                        serialized = json.dumps(new_data, separators=(',', ':'), ensure_ascii=False)

                        if not args.dry_run:
                            conn.execute(f"UPDATE {table} SET {col} = ? WHERE rowid = ?", (serialized, rowid))
                            with open(log_file, "a") as f:
                                f.write(f"{table} {rowid}\n")
                    except json.JSONDecodeError:
                        continue
                
                if not args.dry_run:
                    conn.commit()
        except sqlite3.OperationalError as e:
            sys.stderr.write(f"Database locked or error: {e}\n")
            sys.exit(1)
    
    conn.close()

if __name__ == "__main__":
    main()
