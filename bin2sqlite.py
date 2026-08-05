#!/usr/bin/env python3
"""
bin2sqlite.py — Single-pass ArduPilot .bin log to SQLite converter, with
optional descriptions scraping and a time_step index column added to
every table that has TimeUS, enabling time-based joins across tables.

Usage:
    python3 bin2sqlite.py input.bin output.db
    python3 bin2sqlite.py input.bin output.db --no-descriptions
    python3 bin2sqlite.py input.bin output.db --time-steps 2000
    python3 bin2sqlite.py input.bin output.db --no-time-index
"""

import argparse
import json
import os
import sqlite3
import sys

import requests
from bs4 import BeautifulSoup
from pymavlink import mavutil

DOC_URL = "https://ardupilot.org/copter/docs/logmessages.html"
DEFAULT_TIME_STEPS = 1000


# ------------------------------------------------------------
# Phase 1: log ingestion (single pass, dynamic schema)
# ------------------------------------------------------------

def clean_identifier(name):
    """Sanitize a message type or field name for use as a SQL identifier."""
    return "".join(c for c in name if c.isalnum() or c == "_")


def coerce_value(value):
    """Convert non-scalar field values (lists/tuples/dicts) to JSON text
    so sqlite3 can bind them as parameters."""
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value)
    return value


def get_existing_columns(cursor, table_name):
    cursor.execute(f'PRAGMA table_info("{table_name}")')
    return {row[1] for row in cursor.fetchall()}


def ensure_table(cursor, table_name, fields, known_tables):
    """Create the table if it doesn't exist yet."""
    if table_name in known_tables:
        return known_tables[table_name]

    column_defs = ", ".join(f'"{clean_identifier(f)}"' for f in fields)
    create_sql = f"""
    CREATE TABLE IF NOT EXISTS "{table_name}" (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        {column_defs}
    );
    """
    cursor.execute(create_sql)
    existing_cols = get_existing_columns(cursor, table_name)
    known_tables[table_name] = existing_cols
    return existing_cols


def ensure_columns(cursor, table_name, fields, existing_cols):
    """Add any columns that appear in this message but not yet in the table."""
    for field in fields:
        clean_field = clean_identifier(field)
        if clean_field and clean_field not in existing_cols:
            cursor.execute(
                f'ALTER TABLE "{table_name}" ADD COLUMN "{clean_field}";'
            )
            existing_cols.add(clean_field)


def populate_flight_database(log_path, db_path):
    """Ingest the log in a single pass. Returns:
        message_types   - set of original msg_type strings seen
        known_tables     - dict of table_name -> set of columns
        min_time_us      - lowest TimeUS seen across all messages (or None)
        max_time_us      - highest TimeUS seen across all messages (or None)
    """
    print(f"Opening log file: {log_path}")
    try:
        mlog = mavutil.mavlink_connection(log_path)
    except Exception as e:
        print(f"Error opening log file: {e}", file=sys.stderr)
        sys.exit(1)

    if os.path.exists(db_path):
        print(f"Removing existing database file: {db_path}")
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("PRAGMA synchronous = OFF;")
    cursor.execute("PRAGMA journal_mode = MEMORY;")

    known_tables = {}   # table_name -> set of existing columns
    message_types = set()  # original msg_type strings seen in this log
    min_time_us = None
    max_time_us = None
    count = 0
    error_count = 0
    batch_size = 50000

    print("Ingesting log (single pass)... please wait.")
    cursor.execute("BEGIN TRANSACTION;")

    while True:
        msg = mlog.recv_msg()
        if msg is None:
            break

        msg_type = msg.get_type()
        fields = msg.get_fieldnames()
        if not fields:
            continue

        table_name = clean_identifier(msg_type)
        if not table_name:
            continue

        message_types.add(msg_type)

        existing_cols = ensure_table(cursor, table_name, fields, known_tables)
        ensure_columns(cursor, table_name, fields, existing_cols)

        msg_dict = msg.to_dict()
        clean_fields = [clean_identifier(f) for f in fields]
        values = [coerce_value(msg_dict.get(f)) for f in fields]

        # Track the global TimeUS range while we're already looking at
        # this message's fields — avoids a second file read later.
        if "TimeUS" in fields:
            time_val = msg_dict.get("TimeUS")
            if isinstance(time_val, (int, float)):
                if min_time_us is None or time_val < min_time_us:
                    min_time_us = time_val
                if max_time_us is None or time_val > max_time_us:
                    max_time_us = time_val

        columns_str = ", ".join(f'"{f}"' for f in clean_fields)
        placeholders = ", ".join(["?"] * len(values))
        insert_sql = f'INSERT INTO "{table_name}" ({columns_str}) VALUES ({placeholders})'

        try:
            cursor.execute(insert_sql, values)
            count += 1
        except sqlite3.Error as e:
            error_count += 1
            if error_count <= 5:
                print(f"  Warning: skipped a {msg_type} row ({e})", file=sys.stderr)

        if count % batch_size == 0:
            conn.commit()
            print(f"  Loaded {count:,} messages...")
            cursor.execute("BEGIN TRANSACTION;")

    conn.commit()
    conn.close()

    print(f"\nIngestion complete. Total rows inserted: {count:,}")
    if error_count:
        print(f"Skipped {error_count:,} rows due to insert errors.")

    return message_types, known_tables, min_time_us, max_time_us


# ------------------------------------------------------------
# Phase 2: descriptions scraping (separate, switchable step)
# ------------------------------------------------------------

def check_internet(url="https://ardupilot.org", timeout=5):
    """Quick reachability check before attempting to scrape."""
    try:
        requests.head(url, timeout=timeout)
        return True
    except requests.RequestException:
        return False


def fetch_documentation():
    response = requests.get(DOC_URL, timeout=15)
    response.encoding = "utf-8"
    return BeautifulSoup(response.text, "html.parser")


def find_message_table(soup, msg_type):
    for h2 in soup.find_all("h2"):
        text = h2.get_text(strip=True)
        if text.startswith(msg_type):
            return h2.find_next("table")
    return None


def is_complex(cell):
    """A description cell counts as COMPLEX if it contains structural
    markup (a nested table, list, etc.) or is otherwise too involved
    to flatten cleanly into a single text field."""
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


def extract_table_rows(table):
    rows = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        cells = tr.find_all(["td", "th"], recursive=False)
        if len(cells) != 3:
            continue

        field_name = cells[0].get_text(strip=True)
        field_units = cells[1].get_text(strip=True).replace("Î¼", "µ")

        desc_cell = cells[2]
        if is_complex(desc_cell):
            field_description = "COMPLEX"
        else:
            field_description = desc_cell.get_text(strip=True)

        rows.append((field_name, field_units, field_description))
    return rows


def build_descriptions_table(db_path, message_types):
    """Scrape descriptions only for the message types actually present
    in this log's database. Skips entirely (with a warning) if there's
    no internet connection."""
    if not check_internet():
        print(
            "Warning: no internet connection detected — skipping "
            "descriptions table.",
            file=sys.stderr,
        )
        return

    print(f"Scraping documentation for {len(message_types)} message types...")
    soup = fetch_documentation()

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS descriptions (
            mavpackettype TEXT,
            field_name TEXT,
            field_units TEXT,
            field_description TEXT,
            UNIQUE(mavpackettype, field_name)
        );
    """)

    found_count = 0
    missing_types = []

    for msg_type in sorted(message_types):
        table = find_message_table(soup, msg_type)
        if table is None:
            missing_types.append(msg_type)
            continue

        rows = extract_table_rows(table)
        for (fname, funits, fdesc) in rows:
            cursor.execute(
                "INSERT OR IGNORE INTO descriptions "
                "(mavpackettype, field_name, field_units, field_description) "
                "VALUES (?, ?, ?, ?)",
                (msg_type, fname, funits, fdesc),
            )
        found_count += 1

    conn.commit()
    conn.close()

    print(f"Descriptions added for {found_count} of {len(message_types)} message types.")
    if missing_types:
        print(f"No documentation found for: {', '.join(missing_types)}")


# ------------------------------------------------------------
# Phase 3: time_step index — enables cross-table time-based joins
# ------------------------------------------------------------

def add_time_index(db_path, known_tables, min_time_us, max_time_us, steps=DEFAULT_TIME_STEPS):
    """Add a time_step column (0..steps-1) to every table that has a
    TimeUS column, based on this log's global TimeUS range. Tables
    sharing the same time_step can then be joined on it."""
    tables_with_timeus = sorted(
        t for t, cols in known_tables.items() if "TimeUS" in cols
    )

    if not tables_with_timeus:
        print("No tables contain TimeUS — skipping time index.")
        return

    if min_time_us is None or max_time_us is None:
        print("No TimeUS values recorded — skipping time index.")
        return

    time_range = max_time_us - min_time_us
    step_size = (time_range / steps) if time_range > 0 else 1

    print(f"Adding time_step index ({steps} steps) to {len(tables_with_timeus)} tables...")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    for table_name in tables_with_timeus:
        cursor.execute(f'ALTER TABLE "{table_name}" ADD COLUMN time_step INTEGER;')

        if time_range > 0:
            # MIN(steps-1, ...) clamps the top edge so the max TimeUS
            # value lands in the last valid step instead of one past it.
            cursor.execute(
                f'''
                UPDATE "{table_name}"
                SET time_step = MIN(?, CAST((TimeUS - ?) / ? AS INTEGER))
                WHERE TimeUS IS NOT NULL;
                ''',
                (steps - 1, min_time_us, step_size),
            )
        else:
            # Every record shares the same TimeUS (degenerate case) —
            # everything falls in step 0.
            cursor.execute(f'UPDATE "{table_name}" SET time_step = 0;')

        cursor.execute(
            f'CREATE INDEX IF NOT EXISTS "idx_{table_name}_time_step" '
            f'ON "{table_name}"(time_step);'
        )

    conn.commit()
    conn.close()
    print("Time index complete.")


# ------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert an ArduPilot .bin log to SQLite, with optional "
                     "scraped field descriptions and a time-based join index."
    )
    parser.add_argument("log_file", help="Path to the ArduPilot .bin log file")
    parser.add_argument("db_file", help="Path to the output SQLite database file")
    parser.add_argument(
        "--no-descriptions",
        action="store_true",
        help="Skip building the descriptions table (no web scraping).",
    )
    parser.add_argument(
        "--no-time-index",
        action="store_true",
        help="Skip adding the time_step index column.",
    )
    parser.add_argument(
        "--time-steps",
        type=int,
        default=DEFAULT_TIME_STEPS,
        help=f"Number of time buckets for the time_step index (default {DEFAULT_TIME_STEPS}).",
    )

    args = parser.parse_args()

    message_types, known_tables, min_time_us, max_time_us = populate_flight_database(
        args.log_file, args.db_file
    )

    if args.no_descriptions:
        print("Skipping descriptions table (--no-descriptions given).")
    else:
        build_descriptions_table(args.db_file, message_types)

    if args.no_time_index:
        print("Skipping time index (--no-time-index given).")
    else:
        add_time_index(args.db_file, known_tables, min_time_us, max_time_us, args.time_steps)

    print(f"\nDone: {args.db_file}")


if __name__ == "__main__":
    main()