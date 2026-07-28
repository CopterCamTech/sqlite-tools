#!/usr/bin/env python3

import argparse
import os
import sqlite3
import json
import requests
from bs4 import BeautifulSoup
from pymavlink import mavutil

# ------------------------------------------------------------
# Fetch and parse ArduPilot documentation page
# ------------------------------------------------------------

DOC_URL = "https://ardupilot.org/copter/docs/logmessages.html"

def fetch_documentation():
    response = requests.get(DOC_URL)

    # Force UTF‑8 decoding (Option 3)
    response.encoding = "utf-8"

    soup = BeautifulSoup(response.text, "html.parser")
    return soup

def is_complex(cell):
    structural_tags = {"table", "tr", "td", "th", "ul", "ol", "li", "div"}

    for tag in cell.find_all():
        if tag.name in structural_tags:
            return True

    text = cell.get_text(strip=True)

    if "\n" in text:
        return True
    if len(text) > 200:
        return True
    if "|" in text:
        return True

    return False

def find_message_table(soup, msg_type):
    for h2 in soup.find_all("h2"):
        text = h2.get_text(strip=True)
        if text.startswith(msg_type):
            return h2.find_next("table")
    return None

def extract_table_rows(table):
    rows = []

    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue

        # Only count top-level cells
        cells = tr.find_all(["td", "th"], recursive=False)
        if len(cells) != 3:
            continue

        field_name = cells[0].get_text(strip=True)

        # Extract units
        field_units = cells[1].get_text(strip=True)

        # Replace mojibake "Î¼" with proper "µ" (Option 2)
        field_units = field_units.replace("Î¼", "µ")

        desc_cell = cells[2]
        if is_complex(desc_cell):
            field_description = "COMPLEX"
        else:
            field_description = desc_cell.get_text(strip=True)

        rows.append((field_name, field_units, field_description))

    return rows

# ------------------------------------------------------------
# Existing validation and ingestion helpers
# ------------------------------------------------------------

def validate_filenames(input_file, output_file):
    if not input_file.lower().endswith(".bin"):
        return False, "Input file must have .bin extension."
    if not output_file.lower().endswith(".db"):
        return False, "Output file must have .db extension."
    if not os.path.isfile(input_file):
        return False, "Input .bin file does not exist."
    return True, None

def validate_log_file(path):
    try:
        log = mavutil.mavlink_connection(path)
        msg = log.recv_match()
        if msg is None:
            return False, "File exists but contains no log records."
        return True, None
    except Exception as e:
        return False, f"Failed to open as ArduPilot log: {e}"

def map_sqlite_type(field_name, value_sample):
    if field_name == "TimeUS":
        return "INTEGER"
    if field_name == "mavpackettype":
        return "TEXT"
    if field_name in ("x", "y", "z"):
        return "REAL"
    if isinstance(value_sample, int):
        return "INTEGER"
    if isinstance(value_sample, float):
        return "REAL"
    if isinstance(value_sample, (list, tuple, dict)):
        return "TEXT"
    if isinstance(value_sample, str):
        return "TEXT"
    return "REAL"

def table_exists(cur, table_name):
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    return cur.fetchone() is not None

def get_existing_columns(cur, table_name):
    cur.execute(f'PRAGMA table_info("{table_name}")')
    cols = [row[1] for row in cur.fetchall()]
    return set(cols)

def create_table(cur, table_name, msg_dict):
    columns = []
    for field, value in msg_dict.items():
        col_type = map_sqlite_type(field, value)
        columns.append(f'"{field}" {col_type}')
    if not columns:
        columns.append('"dummy" INTEGER')
    column_sql = ", ".join(columns)
    create_sql = f'CREATE TABLE "{table_name}" ({column_sql});'
    cur.execute(create_sql)

def add_missing_columns(cur, table_name, existing_cols, msg_dict):
    for field, value in msg_dict.items():
        if field in existing_cols:
            continue
        col_type = map_sqlite_type(field, value)
        alter_sql = f'ALTER TABLE "{table_name}" ADD COLUMN "{field}" {col_type};'
        cur.execute(alter_sql)
        existing_cols.add(field)

def insert_record(cur, table, msg_dict, existing_cols):
    fields = sorted(existing_cols)
    row = []
    for field in fields:
        value = msg_dict.get(field, None)
        if isinstance(value, (list, tuple, dict)):
            value = json.dumps(value)
        row.append(value)
    placeholders = ", ".join(["?"] * len(fields))
    column_list = ", ".join([f'"{f}"' for f in fields])
    sql = f'INSERT INTO "{table}" ({column_list}) VALUES ({placeholders})'
    cur.execute(sql, row)

# ------------------------------------------------------------
# Main ingestion with unlimited scraping
# ------------------------------------------------------------

def ingest_log_dynamic(db_path, log_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    log = mavutil.mavlink_connection(log_path)

    cur.execute("""
        CREATE TABLE descriptions (
            mavpackettype TEXT,
            field_name TEXT,
            field_units TEXT,
            field_description TEXT,
            UNIQUE(mavpackettype, field_name)
        );
    """)

    soup = fetch_documentation()

    table_columns = {}
    scraped_types = set()
    count = 0
    batch_size = 1000

    while True:
        msg = log.recv_match()
        if msg is None:
            break

        msg_type = msg.get_type()
        msg_dict = msg.to_dict()

        if msg_type not in table_columns:
            if not table_exists(cur, msg_type):
                create_table(cur, msg_type, msg_dict)
            existing_cols = get_existing_columns(cur, msg_type)
            table_columns[msg_type] = existing_cols
        else:
            existing_cols = table_columns[msg_type]

        add_missing_columns(cur, msg_type, existing_cols, msg_dict)
        insert_record(cur, msg_type, msg_dict, existing_cols)

        if msg_type not in scraped_types:
            table = find_message_table(soup, msg_type)
            if table:
                rows = extract_table_rows(table)
                for (fname, funits, fdesc) in rows:
                    cur.execute(
                        "INSERT OR IGNORE INTO descriptions (mavpackettype, field_name, field_units, field_description) VALUES (?, ?, ?, ?)",
                        (msg_type, fname, funits, fdesc)
                    )
                conn.commit()
            scraped_types.add(msg_type)

        count += 1

        if count % batch_size == 0:
            conn.commit()

    conn.commit()
    conn.close()

    print(f"\nFinished ingestion. Total records: {count}")

# ------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert ArduPilot .bin log to SQLite .db with dynamic schema and documentation scraping"
    )
    parser.add_argument("input_bin", help="Input .bin log file")
    parser.add_argument("output_db", help="Output .db SQLite file")
    args = parser.parse_args()

    valid, error = validate_filenames(args.input_bin, args.output_db)
    if not valid:
        print(f"ERROR: {error}")
        return

    valid, error = validate_log_file(args.input_bin)
    if not valid:
        print(f"ERROR: {error}")
        return

    if os.path.exists(args.output_db):
        os.remove(args.output_db)

    print("Ingesting log with dynamic schema and unlimited scraping...")
    ingest_log_dynamic(args.output_db, args.input_bin)

    print(f"Conversion complete: {args.output_db}")

if __name__ == "__main__":
    main()
