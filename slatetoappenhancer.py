#!/usr/bin/env python3

import os
import json
import shutil
import logging
import requests
from requests.auth import HTTPBasicAuth
import sqlite3
import smtplib
import time
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
# SQLite Setup & Dynamic Schema
# --------------------------------------------------------------
def init_db():
    """
    Initialize the SQLite database and ensure all metadata columns from config exist.
    """
    conn = sqlite3.connect(DB_PATH)
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
            
    conn.commit()
    conn.close()

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

def upsert_slate_records(rows):
    """
    Insert or Update discovered records from Slate.
    Only inserts if the filename is new to avoid resetting status/attempts.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now_ts = datetime.now(timezone.utc).isoformat()
    mapping = config.get("metadata_mapping", [])
    slate_file_name_field = config.get("slate_file_name_field", "FileName")
    
    for row in rows:
        filename = row.get(slate_file_name_field)
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
        
    conn.commit()
    conn.close()

def update_record_status(filename, status, document_id=None, error=None, increment_attempt=False):
    """
    Update the processing status of a record.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
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
    
    cursor.execute(query, params)
    conn.commit()
    conn.close()

def get_processable_records():
    """
    Retrieve records that are not yet successfully uploaded.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM records WHERE status NOT IN ('SUCCESS', 'DUPLICATE')")
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def is_already_successful(filename):
    """
    Check if a filename is already recorded as SUCCESS or DUPLICATE in the DB.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM records WHERE filename = ? AND status IN ('SUCCESS', 'DUPLICATE')", (filename,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

# --------------------------------------------------------------
# Email Notification
# --------------------------------------------------------------
def send_summary_email(success_count, fail_count, success_list, fail_list):
    """
    Send a summary email at the end of the run with stats and details.
    """
    if success_count == 0 and fail_count == 0:
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
        f"Total Successes: {success_count}",
        f"Total Failures:  {fail_count}",
        f"--------------------------------------------------\n"
    ]

    if success_list:
        body_lines.append("SUCCESSFUL UPLOADS:")
        for item in success_list:
            body_lines.append(f" - {item}")
        body_lines.append("")

    if fail_list:
        body_lines.append("FAILED UPLOADS:")
        for item in fail_list:
            body_lines.append(f" - {item}")
        body_lines.append("")

    msg_text = "\n".join(body_lines)

    try:
        message = MIMEText(msg_text)
        message["Subject"] = subject
        message["From"] = config["smtp_from"]
        message["To"] = config["smtp_to"]

        # If smtp_to contains commas, split into a list for the SMTP send call
        recipients = [r.strip() for r in config["smtp_to"].split(",")]

        with smtplib.SMTP(config["smtp_server"], config["smtp_port"]) as server:
            server.starttls()
            server.login(config["smtp_user"], config["smtp_pass"])
            server.send_message(message)

        logging.info(f"Summary email sent to {len(recipients)} recipient(s).")
    except Exception as e:
        logging.error(f"Failed to send summary email: {e}")

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
                except: pass
                
                logging.info(f"{log_prefix}AppEnhancer upload success: {filename} | ID={doc_id}")
                return {"success": True, "document_id": doc_id}
            
            # Check for Duplicate Index (Error 125)
            resp_json = {}
            try: resp_json = response.json()
            except: pass

            error_code = resp_json.get("ErrorCode")
            if error_code == 125 or "duplicate index" in str(resp_json.get("Message", "")).lower():
                logging.warning(f"{log_prefix}Duplicate detected for {filename}. Treating as success.")
                return {"success": True, "document_id": None, "duplicate": True}

            logging.warning(f"{log_prefix}Upload failed ({response.status_code}): {response.text}")

        except Exception as e:
            logging.error(f"{log_prefix}Upload exception for {filename}: {e}")

        if attempt < attempts:
            time.sleep(delay)

    return {"success": False, "error": "Upload failed after retries"}

# --------------------------------------------------------------
# Fetch Slate Data
# --------------------------------------------------------------
def fetch_slate_results():
    url = f"{config['slate_api_url']}?id={config['slate_query_id']}&cmd=service&output=json"
    headers = {"Authorization": f"Bearer {config['slate_bearer_token']}"}

    try:
        logging.info("Requesting data from Slate API...")
        response = requests.get(url, headers=headers)
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
def download_file(url, filename):
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(filepath):
        return filepath

    attempts = config.get("download_retry_attempts", 5)
    delay = config.get("download_retry_delay", 5)

    for attempt in range(1, attempts + 1):
        try:
            r = requests.get(url, stream=True, timeout=30)
            if r.status_code == 200:
                with open(filepath, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                return filepath
        except Exception as e:
            logging.warning(f"Download error for {filename}: {e}")
        
        if attempt < attempts:
            time.sleep(delay)
    return None

# --------------------------------------------------------------
# Archive & Cleanup
# --------------------------------------------------------------
def archive_file(local_file, raw_json):
    """
    Move the document to archive and save a matching .json metadata file.
    """
    dst_dir = os.path.join(ARCHIVE_DIR, datetime.now().strftime("%Y/%m/%d"))
    os.makedirs(dst_dir, exist_ok=True)
    
    # Archive Document
    base_name = os.path.basename(local_file)
    shutil.move(local_file, os.path.join(dst_dir, base_name))
    
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

def cleanup_archives():
    days = config.get("days_to_keep_archive", 0)
    if days <= 0: return
    cutoff = datetime.now() - timedelta(days=days)
    for root, dirs, _ in os.walk(ARCHIVE_DIR):
        for dirname in dirs:
            full = os.path.join(root, dirname)
            if datetime.fromtimestamp(os.path.getmtime(full)) < cutoff:
                shutil.rmtree(full)
                logging.info(f"Removed old archive: {full}")

def cleanup_logs():
    """
    Remove log files older than days_to_keep_logs.
    """
    days = config.get("days_to_keep_logs", 0)
    if days <= 0: return
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
    
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Resolve AppEnhancer URL components
    ae_base_url = args.baseurl or config.get("appenhancer_base_url")
    ae_datasource = args.datasource or config.get("appenhancer_datasource")
    ae_appid = args.appid or config.get("appenhancer_appid")
    ae_urlparams = args.urlparams or config.get("appenhancer_urlparams")

    # Resolve Slate material field names
    slate_file_url_field = config.get("slate_file_url_field", "FileURL")
    slate_file_name_field = config.get("slate_file_filename_field", "FileName")
    
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
            fname = row.get(slate_file_name_field)
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
    success_list = []
    fail_list = []
    total = len(to_process)

    for idx, record in enumerate(to_process, 1):
        filename = record["filename"]
        slate_data = json.loads(record["raw_json"])
        file_url = slate_data.get(slate_file_url_field)
        prefix = f"[{idx}/{total}] "

        logging.info(f"{prefix}Processing {filename}...")

        # Step A: Download
        local_path = download_file(file_url, filename)
        if not local_path:
            logging.error(f"{prefix}Download failed for {filename}")
            if not args.dry_run:
                update_record_status(filename, "DOWNLOAD_FAILED", error="Failed to download file", increment_attempt=True)
            fail_count += 1
            fail_list.append(f"{filename} (Download Error)")
            continue

        # Step B: Upload
        result = upload_to_appenhancer(record, local_path, upload_url, dry_run=args.dry_run, log_prefix=prefix)
        
        if result["success"]:
            status = "DUPLICATE" if result.get("duplicate") else "SUCCESS"
            success_count += 1
            success_list.append(filename)

            if not args.dry_run:
                update_record_status(filename, status, document_id=result.get("document_id"))
                archive_file(local_path, record["raw_json"])
                logging.info(f"{prefix}Processed successfully")
            else:
                logging.info(f"{prefix}DRY RUN: {filename} processed successfully")
        else:
            fail_count += 1
            fail_list.append(f"{filename} ({result.get('error')})")
            logging.error(f"{prefix}Upload failed for {filename}: {result.get('error')}")
            if not args.dry_run:
                update_record_status(filename, "UPLOAD_FAILED", error=result.get("error"), increment_attempt=True)

    send_summary_email(success_count, fail_count, success_list, fail_list)
    cleanup_archives()
    cleanup_logs()
    clear_downloads()

if __name__ == "__main__":
    main()