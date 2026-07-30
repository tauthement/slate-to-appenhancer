#!/usr/bin/env python3

import os
import re
import sys
import fcntl
import json
import shutil
import logging
import requests
from requests.auth import HTTPBasicAuth
import sqlite3
import smtplib
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
import argparse

# --------------------------------------------------------------
# Load Config
# --------------------------------------------------------------
def load_config():
    with open("config.json", "r") as f:
        return json.load(f)

config = load_config()

WORK_DIR = config["work_dir"]
DOWNLOAD_DIR = os.path.join(WORK_DIR, "downloads")
ARCHIVE_DIR = os.path.join(WORK_DIR, "archive")
DB_PATH = os.path.join(WORK_DIR, "records.db")
LOCK_FILE_PATH = os.path.join(WORK_DIR, ".slatetoappenhancer.lock")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(ARCHIVE_DIR, exist_ok=True)

# --------------------------------------------------------------
# Log File Path (Year/Month with Timestamped Filename)
# --------------------------------------------------------------
now = datetime.now()
log_dir = os.path.join(
    config["log_dir"],
    now.strftime("%Y"),
    now.strftime("%m")
)
os.makedirs(log_dir, exist_ok=True)

LOG_FILE = os.path.join(
    log_dir,
    f"slatetoappenhancer_{now.strftime('%Y%m%d_%H%M%S')}.log"
)

# --------------------------------------------------------------
# Logging Setup
# --------------------------------------------------------------
logging.basicConfig(
    filename=LOG_FILE,
    filemode="a",
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().addHandler(console)

# --------------------------------------------------------------
# Single-Instance Lock
# --------------------------------------------------------------
def acquire_lock():
    """
    Acquire an exclusive, non-blocking OS-level file lock so overlapping
    runs (e.g. a scheduled run firing while a prior run is still in
    progress) can't hit the SQLite DB or downloads/archive dirs at once.
    The lock is automatically released by the OS if this process dies,
    so no stale-lock cleanup is needed.
    """
    lock_fd = open(LOCK_FILE_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logging.error("Another instance of this script appears to be running. Exiting.")
        lock_fd.close()
        sys.exit(1)

    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    return lock_fd

def release_lock(lock_fd):
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        lock_fd.close()

# --------------------------------------------------------------
# SQLite Setup & Dynamic Schema
# --------------------------------------------------------------
@contextmanager
def db_connection():
    """
    Yield a SQLite connection, committing on clean exit and always
    closing the connection, even if an exception is raised.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

# Reserved so a metadata_mapping db_column can't silently shadow a core column.
RESERVED_COLUMNS = {
    "id", "filename", "status", "document_id", "attempts",
    "last_error", "first_seen_at", "last_attempt_at", "uploaded_at", "raw_json"
}
VALID_COLUMN_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

def validate_metadata_mapping():
    """
    Validate that every configured db_column is a safe SQL identifier and
    doesn't collide with a core records column, before it's used to build
    ALTER TABLE / INSERT statements via string formatting.
    """
    for entry in config.get("metadata_mapping", []):
        col = entry.get("db_column", "")
        if not VALID_COLUMN_PATTERN.match(col):
            raise ValueError(
                f"Invalid metadata_mapping db_column {col!r}: must contain only "
                "letters, digits, and underscores, and not start with a digit."
            )
        if col.lower() in RESERVED_COLUMNS:
            raise ValueError(
                f"Invalid metadata_mapping db_column {col!r}: collides with a "
                "reserved records table column."
            )

def init_db():
    """
    Initialize the SQLite database and ensure all metadata columns from config exist.
    """
    validate_metadata_mapping()

    with db_connection() as conn:
        cursor = conn.cursor()

        # Create base table with ID as Primary Key and filename as UNIQUE
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT UNIQUE,
                status TEXT DEFAULT 'PENDING',
                document_id TEXT,
                attempts INTEGER DEFAULT 0,
                last_error TEXT,
                first_seen_at DATETIME,
                last_attempt_at DATETIME,
                uploaded_at DATETIME,
                raw_json TEXT
            )
        """)

        # Check for existing columns to avoid redundant ALTER TABLE calls
        cursor.execute("PRAGMA table_info(records)")
        existing_columns = [row[1] for row in cursor.fetchall()]

        # Add metadata columns defined in config if they don't exist
        mapping = config.get("metadata_mapping", [])
        for entry in mapping:
            col = entry["db_column"]
            if col not in existing_columns:
                logging.info(f"Adding new metadata column to DB: {col}")
                cursor.execute(f"ALTER TABLE records ADD COLUMN {col} TEXT")

def clean_value(val):
    """
    Helper to clean and convert metadata values to strings or None,
    handling nested structures (like dicts and lists) returned by Slate API.
    """
    if val is None:
        return None
    if isinstance(val, dict):
        if "#cdata-section" in val:
            return clean_value(val["#cdata-section"])
        if "#whitespace" in val:
            return clean_value(val["#whitespace"])
        return json.dumps(val)
    if isinstance(val, list):
        return json.dumps(val)
    return str(val)

def sanitize_filename(filename):
    """
    Reduce a Slate-provided filename to a bare basename so it can't be used
    to write outside DOWNLOAD_DIR/ARCHIVE_DIR (e.g. via '../' or an absolute path).
    """
    return os.path.basename(filename) if filename else filename

def upsert_slate_records(rows):
    """
    Insert or Update discovered records from Slate.
    Only inserts if the filename is new to avoid resetting status/attempts.
    """
    now_ts = datetime.now(timezone.utc).isoformat()
    mapping = config.get("metadata_mapping", [])
    slate_file_name_field = config.get("slate_file_name_field", "FileName")

    with db_connection() as conn:
        cursor = conn.cursor()

        for row in rows:
            filename = sanitize_filename(row.get(slate_file_name_field))
            if not filename:
                continue

            # Core columns for insertion
            cols = ["filename", "first_seen_at", "raw_json"]
            vals = [filename, now_ts, json.dumps(row)]

            # Add metadata columns from the row
            for entry in mapping:
                cols.append(entry["db_column"])
                vals.append(clean_value(row.get(entry["slate_key"])))

            placeholders = ", ".join(["?"] * len(cols))
            query = f"INSERT OR IGNORE INTO records ({', '.join(cols)}) VALUES ({placeholders})"

            cursor.execute(query, vals)

def update_record_status(filename, status, document_id=None, error=None, increment_attempt=False):
    """
    Update the processing status of a record.
    """
    now_ts = datetime.now(timezone.utc).isoformat()

    updates = ["status = ?", "last_attempt_at = ?"]
    params = [status, now_ts]

    if document_id:
        updates.append("document_id = ?")
        params.append(str(document_id))

    if status == "SUCCESS":
        updates.append("uploaded_at = ?")
        params.append(now_ts)

    if error:
        updates.append("last_error = ?")
        params.append(str(error))

    if increment_attempt:
        updates.append("attempts = attempts + 1")

    params.append(filename)
    query = f"UPDATE records SET {', '.join(updates)} WHERE filename = ?"

    with db_connection() as conn:
        conn.execute(query, params)

def resolve_failure_status(record, fallback_status):
    """
    Return fallback_status normally, or FAILED_PERMANENT if this failure would
    push the record's attempt count to/past the configured max_attempts.
    max_attempts defaults to 0 (unlimited retries).
    """
    max_attempts = config.get("max_attempts", 0)
    if max_attempts > 0 and record.get("attempts", 0) + 1 >= max_attempts:
        return "FAILED_PERMANENT"
    return fallback_status

def get_processable_records():
    """
    Retrieve records that are not yet successfully uploaded and haven't
    permanently failed.
    """
    with db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM records WHERE status NOT IN ('SUCCESS', 'DUPLICATE', 'FAILED_PERMANENT')")
        return [dict(row) for row in cursor.fetchall()]

def is_already_successful(filename):
    """
    Check if a filename is already recorded as SUCCESS or DUPLICATE in the DB.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM records WHERE filename = ? AND status IN ('SUCCESS', 'DUPLICATE')", (filename,))
        return cursor.fetchone() is not None

# --------------------------------------------------------------
# Email Notification
# --------------------------------------------------------------
SUMMARY_EMAIL_FIELDS = ["student_id", "first_name", "last_name", "term_code", "admissions_req"]
SUMMARY_EMAIL_HEADERS = ["Filename", "Student ID", "First Name", "Last Name", "Term Code", "Admissions Req"]

def get_summary_metadata(record):
    """
    Extract the fields listed in SUMMARY_EMAIL_FIELDS from a record's raw Slate JSON,
    using the configured metadata_mapping to resolve db_column -> slate_key.
    """
    mapping = config.get("metadata_mapping", [])
    slate_data = json.loads(record["raw_json"])

    metadata = {}
    for col in SUMMARY_EMAIL_FIELDS:
        entry = next((m for m in mapping if m["db_column"] == col), None)
        val = clean_value(slate_data.get(entry["slate_key"])) if entry else None
        metadata[col] = val if val is not None else ""

    return metadata

def format_table(headers, rows):
    """
    Render a simple aligned, plain-text table from a list of header strings
    and a list of rows (each row a list of cell values, same length as headers).
    """
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def fmt_row(cells):
        return " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells))

    lines = [fmt_row(headers), "-+-".join("-" * w for w in widths)]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)

def send_summary_email(success_count, fail_count, duplicate_count, success_list, fail_list):
    """
    Send a summary email at the end of the run with stats and details.
    """
    if success_count == 0 and fail_count == 0 and duplicate_count == 0:
        logging.info("No records processed; skipping summary email.")
        return

    # Build Subject: [Success: X][Fail: Y] or [Success: X]
    if fail_count > 0:
        subject = f"Slate to AE: [Success: {success_count}][Fail: {fail_count}]"
    else:
        subject = f"Slate to AE: [Success: {success_count}]"

    # Build Body
    body_lines = [
        f"Slate to AE Import Summary",
        f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"--------------------------------------------------",
        f"Total Successes:  {success_count}",
        f"Total Duplicates: {duplicate_count}",
        f"Total Failures:   {fail_count}",
        f"--------------------------------------------------\n"
    ]

    if success_list:
        body_lines.append("SUCCESSFUL UPLOADS:")
        rows = [
            [item["filename"]] + [item.get(col, "") for col in SUMMARY_EMAIL_FIELDS]
            for item in success_list
        ]
        body_lines.append(format_table(SUMMARY_EMAIL_HEADERS, rows))
        body_lines.append("")

    if fail_list:
        body_lines.append("FAILED UPLOADS:")
        rows = [
            [item["filename"]] + [item.get(col, "") for col in SUMMARY_EMAIL_FIELDS] + [item.get("error", "")]
            for item in fail_list
        ]
        body_lines.append(format_table(SUMMARY_EMAIL_HEADERS + ["Error"], rows))
        body_lines.append("")

    msg_text = "\n".join(body_lines)
    _send_email(subject, msg_text, log_label="Summary email")

def send_crash_alert(error_text):
    """
    Best-effort notification for a fatal, unhandled error that aborted
    the run before it could reach send_summary_email().
    """
    subject = "Slate to AE: CRITICAL FAILURE - Run Did Not Complete"
    body = "\n".join([
        "The Slate to AppEnhancer import script crashed and did not complete.",
        f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "--------------------------------------------------",
        "Check the log file for the full traceback. Error:",
        "",
        error_text,
    ])
    _send_email(subject, body, log_label="Crash alert email")

def _send_email(subject, body, log_label="Email"):
    try:
        message = MIMEText(body)
        message["Subject"] = subject
        message["From"] = config["smtp_from"]
        message["To"] = config["smtp_to"]

        # If smtp_to contains commas, split into a list for the SMTP send call
        recipients = [r.strip() for r in config["smtp_to"].split(",")]

        with smtplib.SMTP(config["smtp_server"], config["smtp_port"], timeout=30) as server:
            server.starttls()
            server.login(config["smtp_user"], config["smtp_pass"])
            server.send_message(message)

        logging.info(f"{log_label} sent to {len(recipients)} recipient(s).")
    except Exception as e:
        logging.error(f"Failed to send {log_label.lower()}: {e}")

# --------------------------------------------------------------
# Build AppEnhancer Metadata
# --------------------------------------------------------------
def build_appenhancer_metadata(record_row):
    """
    Build the AppEnhancer metadata JSON payload dynamically from config mapping.
    record_row is a dict representing a DB row.
    """
    # If the record_row is from the DB, we might need to parse the raw_json 
    # if the DB columns don't have exactly what we need, but since we 
    # save everything to DB columns, we use those or the raw_json.
    slate_data = json.loads(record_row["raw_json"])
    
    mapping = config.get("metadata_mapping", [])
    index_values = []
    
    for entry in mapping:
        val = slate_data.get(entry["slate_key"])
        cleaned_val = clean_value(val)
        if cleaned_val is not None:
            index_values.append({
                "FieldID": entry["field_id"],
                "FieldValue": cleaned_val
            })

    return {
        "TargetDoc": None,
        "NewIndex": {
            "indexid": 0,
            "values": index_values,
            "links": []
        },
        "NativeNewIndex": None,
        "FromBatch": None,
        "BatchPageNum": 0,
        "PageRange": None,
        "MergeDuplicateIndex": False,
        "IgnoreDuplicateIndex": False,
        "IgnoreDlsViolation": False,
        "SubmitFullText": False
    }

# --------------------------------------------------------------
# AppEnhancer Upload
# --------------------------------------------------------------
def upload_to_appenhancer(record, local_file, upload_url, dry_run=False, log_prefix=""):
    attempts = config.get("appenhancer_retry_attempts", 3)
    delay = config.get("appenhancer_retry_delay", 5) 
    filename = record["filename"]

    if dry_run:
        logging.info(f"{log_prefix}DRY RUN: Would upload {filename} to AppEnhancer at {upload_url}")
        return {"success": True, "document_id": None}

    metadata = build_appenhancer_metadata(record)
    last_error = None

    for attempt in range(1, attempts + 1):
        logging.info(f"{log_prefix}AppEnhancer upload attempt {attempt}/{attempts} for {filename}")

        try:
            with open(local_file, "rb") as fh:
                files = {
                    "data": (
                        None,
                        json.dumps(metadata),
                        "application/vnd.emc.ax+json; charset=utf-8"
                    ),
                    "bin": (
                        os.path.basename(local_file),
                        fh,
                        "application/pdf"
                    )
                }

                response = requests.post(
                    upload_url,
                    files=files, 
                    auth=HTTPBasicAuth(
                        config["appenhancer_user"],
                        config["appenhancer_pass"]
                    ),
                    timeout=60
                )

            if response.status_code in (200, 201):
                doc_id = None
                try:
                    doc_id = response.json().get("ID")
                except Exception:
                    pass

                logging.info(f"{log_prefix}AppEnhancer upload success: {filename} | ID={doc_id}")
                return {"success": True, "document_id": doc_id}

            # Check for Duplicate Index (Error 125)
            resp_json = {}
            try:
                resp_json = response.json()
            except Exception:
                pass

            error_code = resp_json.get("ErrorCode")
            if error_code == 125 or "duplicate index" in str(resp_json.get("Message", "")).lower():
                logging.warning(f"{log_prefix}Duplicate detected for {filename}. Treating as success.")
                return {"success": True, "document_id": None, "duplicate": True}

            last_error = f"HTTP {response.status_code}: {resp_json.get('Message', response.text)}"
            logging.warning(f"{log_prefix}Upload failed ({response.status_code}): {response.text}")

        except Exception as e:
            last_error = str(e)
            logging.error(f"{log_prefix}Upload exception for {filename}: {e}")

        if attempt < attempts:
            time.sleep(delay)

    return {"success": False, "error": last_error or "Upload failed after retries"}

# --------------------------------------------------------------
# Fetch Slate Data
# --------------------------------------------------------------
def fetch_slate_results():
    url = f"{config['slate_api_url']}?id={config['slate_query_id']}&cmd=service&output=json"
    headers = {"Authorization": f"Bearer {config['slate_bearer_token']}"}

    try:
        logging.info("Requesting data from Slate API...")
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code != 200:
            raise Exception(f"Slate returned status {response.status_code}")
        
        data = response.json()
        rows = data.get("row", [])
        logging.info(f"Retrieved {len(rows)} records from Slate.")
        return rows
    except Exception as e:
        logging.error(f"Slate API request failed: {e}")
        return []

# --------------------------------------------------------------
# Download File
# --------------------------------------------------------------
def validate_downloaded_file(filepath):
    """
    Basic sanity check that a downloaded file is actually a PDF and not,
    say, an HTML error/login page served with a 200 status. Returns an
    error string if the file looks invalid, or None if it looks fine.
    """
    try:
        if os.path.getsize(filepath) == 0:
            return "Downloaded file is empty"
        with open(filepath, "rb") as f:
            header = f.read(5)
    except OSError as e:
        return f"Could not read downloaded file: {e}"

    if header != b"%PDF-":
        return f"Downloaded file does not look like a PDF (header: {header!r})"

    return None

def download_file(url, filename):
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(filepath):
        return {"success": True, "path": filepath, "error": None}

    attempts = config.get("download_retry_attempts", 5)
    delay = config.get("download_retry_delay", 5)
    last_error = None
    tmp_path = filepath + ".part"

    for attempt in range(1, attempts + 1):
        try:
            r = requests.get(url, stream=True, timeout=30)
            if r.status_code != 200:
                last_error = f"HTTP {r.status_code}"
            else:
                # Write to a temp path and only rename into place once the
                # download is complete and validated, so a killed process
                # can never leave a truncated file that a later run mistakes
                # for a good, already-downloaded one.
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)

                validation_error = validate_downloaded_file(tmp_path)
                if validation_error:
                    last_error = validation_error
                else:
                    os.replace(tmp_path, filepath)
                    return {"success": True, "path": filepath, "error": None}
        except Exception as e:
            last_error = str(e)
            logging.warning(f"Download error for {filename}: {e}")
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        if attempt < attempts:
            time.sleep(delay)

    return {"success": False, "path": None, "error": last_error or "Download failed after retries"}

# --------------------------------------------------------------
# Archive & Cleanup
# --------------------------------------------------------------
def archive_file(local_file, raw_json):
    """
    Move the document to archive and save a matching .json metadata file.
    If a same-named file already exists in today's archive folder, the new
    one is suffixed with a counter instead of silently overwriting it.
    """
    dst_dir = os.path.join(ARCHIVE_DIR, datetime.now().strftime("%Y/%m/%d"))
    os.makedirs(dst_dir, exist_ok=True)

    base_name = os.path.basename(local_file)
    dst_path = os.path.join(dst_dir, base_name)

    if os.path.exists(dst_path):
        original_name = base_name
        stem, ext = os.path.splitext(base_name)
        counter = 1
        while os.path.exists(dst_path):
            base_name = f"{stem}_{counter}{ext}"
            dst_path = os.path.join(dst_dir, base_name)
            counter += 1
        logging.warning(f"Archive collision for {original_name}; archiving as {base_name} instead of overwriting.")

    # Archive Document
    shutil.move(local_file, dst_path)

    # Save matching JSON file
    json_name = os.path.splitext(base_name)[0] + ".json"
    with open(os.path.join(dst_dir, json_name), "w") as f:
        f.write(raw_json)

    logging.info(f"Archived document and metadata: {base_name}")

def clear_downloads():
    """
    Remove all files from the downloads directory.
    """
    logging.info("Clearing downloads directory...")
    for filename in os.listdir(DOWNLOAD_DIR):
        file_path = os.path.join(DOWNLOAD_DIR, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            logging.error(f"Failed to delete {file_path}. Reason: {e}")

def cleanup_archives(days_override=None):
    days = days_override if days_override is not None else config.get("days_to_keep_archive", 0)
    if days <= 0:
        logging.info("days_to_keep_archive is 0 (or unset); archive cleanup is disabled and archives will accumulate indefinitely.")
        return
    cutoff = datetime.now() - timedelta(days=days)
    for root, dirs, _ in os.walk(ARCHIVE_DIR):
        for dirname in dirs:
            full = os.path.join(root, dirname)
            if datetime.fromtimestamp(os.path.getmtime(full)) < cutoff:
                shutil.rmtree(full)
                logging.info(f"Removed old archive: {full}")

def cleanup_logs(days_override=None):
    """
    Remove log files older than days_to_keep_logs.
    """
    days = days_override if days_override is not None else config.get("days_to_keep_logs", 0)
    if days <= 0:
        logging.info("days_to_keep_logs is 0 (or unset); log cleanup is disabled and logs will accumulate indefinitely.")
        return
    cutoff = datetime.now() - timedelta(days=days)
    log_base_dir = config["log_dir"]
    
    for root, dirs, files in os.walk(log_base_dir):
        for filename in files:
            file_path = os.path.join(root, filename)
            if datetime.fromtimestamp(os.path.getmtime(file_path)) < cutoff:
                try:
                    os.unlink(file_path)
                    logging.info(f"Removed old log file: {file_path}")
                except Exception as e:
                    logging.error(f"Failed to delete log file {file_path}: {e}")
        
        # Cleanup empty directories
        for dirname in dirs:
            dir_path = os.path.join(root, dirname)
            if os.path.isdir(dir_path) and not os.listdir(dir_path):
                if datetime.fromtimestamp(os.path.getmtime(dir_path)) < cutoff:
                    try:
                        os.rmdir(dir_path)
                        logging.info(f"Removed empty log directory: {dir_path}")
                    except Exception as e:
                        pass # Directory might not be empty now

# --------------------------------------------------------------
# Main
# --------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Slate to AppEnhancer Import Script")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")

    # AppEnhancer Overrides
    parser.add_argument("--baseurl", help="Override AppEnhancer Base URL")
    parser.add_argument("--datasource", help="Override AppEnhancer Datasource")
    parser.add_argument("--appid", help="Override AppEnhancer App ID")
    parser.add_argument("--urlparams", help="Override AppEnhancer URL Parameters")

    # Cleanup
    parser.add_argument("--cleanup", action="store_true",
                         help="Run only the archive/log cleanup and exit, skipping the Slate/AppEnhancer import.")
    parser.add_argument("--cleanup-days", type=int, metavar="N",
                         help="Override days_to_keep_archive and days_to_keep_logs with N for this invocation "
                              "(applies to --cleanup or to the automatic end-of-run cleanup).")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    lock_fd = acquire_lock()

    try:
        if args.cleanup:
            logging.info("Running in cleanup-only mode (--cleanup); skipping the Slate/AppEnhancer import.")
            cleanup_archives(days_override=args.cleanup_days)
            cleanup_logs(days_override=args.cleanup_days)
            return

        # Resolve AppEnhancer URL components
        ae_base_url = args.baseurl or config.get("appenhancer_base_url")
        ae_datasource = args.datasource or config.get("appenhancer_datasource")
        ae_appid = args.appid or config.get("appenhancer_appid")
        ae_urlparams = args.urlparams or config.get("appenhancer_urlparams")

        # Resolve Slate material field names
        slate_file_url_field = config.get("slate_file_url_field", "FileURL")
        slate_file_name_field = config.get("slate_file_name_field", "FileName")

        # Construct AppEnhancer URL
        # Format: {base_url}/AXDataSources/{datasource}/AXDocs/{appid}?{params}
        upload_url = f"{ae_base_url}/AXDataSources/{ae_datasource}/AXDocs/{ae_appid}"
        if ae_urlparams:
            upload_url += f"?{ae_urlparams}"

        logging.info(f"Using AppEnhancer URL: {upload_url}")

        init_db()

        # 1. Discover New Records
        slate_rows = fetch_slate_results()
        if slate_rows and not args.dry_run:
            upsert_slate_records(slate_rows)

        # 2. Collect Records to Process
        # Start with existing PENDING/FAILED from DB
        to_process = get_processable_records()

        # If dry run, also include new results from Slate (not in DB yet)
        if args.dry_run and slate_rows:
            existing_filenames = {r["filename"] for r in to_process}
            for row in slate_rows:
                fname = sanitize_filename(row.get(slate_file_name_field))
                if fname and fname not in existing_filenames:
                    # Only simulate if NOT already SUCCESS in DB
                    if not is_already_successful(fname):
                        to_process.append({
                            "filename": fname,
                            "raw_json": json.dumps(row),
                            "status": "PENDING"
                        })

        if not to_process:
            logging.info("No records to process.")
            return

        success_count = 0
        fail_count = 0
        duplicate_count = 0
        success_list = []
        fail_list = []
        total = len(to_process)

        for idx, record in enumerate(to_process, 1):
            filename = record.get("filename", "UNKNOWN")
            prefix = f"[{idx}/{total}] "

            try:
                slate_data = json.loads(record["raw_json"])
                file_url = slate_data.get(slate_file_url_field)

                logging.info(f"{prefix}Processing {filename}...")

                metadata = get_summary_metadata(record)

                # Step A: Download
                download_result = download_file(file_url, filename)
                if not download_result["success"]:
                    download_error = download_result["error"]
                    logging.error(f"{prefix}Download failed for {filename}: {download_error}")
                    if not args.dry_run:
                        final_status = resolve_failure_status(record, "DOWNLOAD_FAILED")
                        update_record_status(filename, final_status, error=download_error, increment_attempt=True)
                        if final_status == "FAILED_PERMANENT":
                            logging.error(f"{prefix}{filename} reached max_attempts; marking as permanently failed.")
                    fail_count += 1
                    fail_list.append({"filename": filename, "error": download_error, **metadata})
                    continue

                local_path = download_result["path"]

                # Step B: Upload
                result = upload_to_appenhancer(record, local_path, upload_url, dry_run=args.dry_run, log_prefix=prefix)

                if result["success"]:
                    status = "DUPLICATE" if result.get("duplicate") else "SUCCESS"

                    if not args.dry_run:
                        # Archive before recording SUCCESS/DUPLICATE: if the archive
                        # move fails, we want this record to end up as a single
                        # failure (via the outer except below), not double-counted
                        # as both a success and a failure with a stale DB status.
                        archive_file(local_path, record["raw_json"])
                        update_record_status(filename, status, document_id=result.get("document_id"))
                        logging.info(f"{prefix}Processed successfully")
                    else:
                        logging.info(f"{prefix}DRY RUN: {filename} processed successfully")

                    if result.get("duplicate"):
                        duplicate_count += 1
                    else:
                        success_count += 1
                        success_list.append({"filename": filename, **metadata})
                else:
                    fail_count += 1
                    fail_list.append({"filename": filename, "error": result.get("error"), **metadata})
                    logging.error(f"{prefix}Upload failed for {filename}: {result.get('error')}")
                    if not args.dry_run:
                        final_status = resolve_failure_status(record, "UPLOAD_FAILED")
                        update_record_status(filename, final_status, error=result.get("error"), increment_attempt=True)
                        if final_status == "FAILED_PERMANENT":
                            logging.error(f"{prefix}{filename} reached max_attempts; marking as permanently failed.")

            except Exception as e:
                # Catch anything unexpected (malformed record, DB error, archive
                # failure, etc.) so one bad record can't abort the rest of the batch.
                logging.error(f"{prefix}Unexpected error processing {filename}: {e}", exc_info=True)
                fail_count += 1
                fail_list.append({"filename": filename, "error": f"Unexpected error: {e}"})
                if not args.dry_run:
                    try:
                        final_status = resolve_failure_status(record, "ERROR")
                        update_record_status(filename, final_status, error=str(e), increment_attempt=True)
                        if final_status == "FAILED_PERMANENT":
                            logging.error(f"{prefix}{filename} reached max_attempts; marking as permanently failed.")
                    except Exception:
                        logging.error(f"{prefix}Failed to record error status for {filename}", exc_info=True)

        send_summary_email(success_count, fail_count, duplicate_count, success_list, fail_list)

    except Exception as e:
        # Catch anything fatal outside the per-record loop (config/DB init issues,
        # Slate discovery, etc.) so the run always tries to notify someone and
        # always reaches the cleanup step below instead of dying silently.
        logging.critical(f"Fatal error during run: {e}", exc_info=True)
        send_crash_alert(f"{type(e).__name__}: {e}")

    finally:
        if not args.cleanup:
            cleanup_archives(days_override=args.cleanup_days)
            cleanup_logs(days_override=args.cleanup_days)
            clear_downloads()
        release_lock(lock_fd)

if __name__ == "__main__":
    main()