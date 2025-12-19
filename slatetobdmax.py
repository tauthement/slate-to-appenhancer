#!/usr/bin/env python3

import os
import json
import csv
import shutil
import logging
import requests
from requests.auth import HTTPBasicAuth
import subprocess
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
import argparse  # ensure already at top; if not, add this import

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
IMPORTED_RECORD_FILE = os.path.join(WORK_DIR, "imported_records.json")

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
    f"slatetobdmax_{now.strftime('%Y%m%d_%H%M%S')}.log"
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
# Email Notification
# --------------------------------------------------------------
def send_failure_email(subject, msg):
    try:
        message = MIMEText(msg)
        message["Subject"] = subject
        message["From"] = config["smtp_from"]
        message["To"] = config["smtp_to"]

        with smtplib.SMTP(config["smtp_server"], config["smtp_port"]) as server:
            server.starttls()
            server.login(config["smtp_user"], config["smtp_pass"])
            server.send_message(message)

        logging.info("Failure email sent.")
    except Exception as e:
        logging.error(f"Failed to send failure notification email: {e}")

# --------------------------------------------------------------
# Structured Upload Logging
# --------------------------------------------------------------
def log_upload_event(event_type, record, **extra):
    payload = {
        "event": event_type,
        "filename": record.get("DocumentFileName"),
        "local_file": record.get("local_file"),
        "ID": record.get('ID'),
        "first name": record.get('FIRSTNAME'),
        "last name": record.get('LASTNAME'),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(extra)
    logging.info(json.dumps(payload))

# --------------------------------------------------------------
# Build AppXtender Metadata
# --------------------------------------------------------------
def build_appxtender_metadata(record):
    """
    Build the AppXtender metadata JSON payload from a Slate record.
    FieldID values must match AppXtender index definitions.
    """

    return {
        "TargetDoc": None,
        "NewIndex": {
            "indexid": 0,
            "values": [
                {"FieldID": "field1", "FieldValue": record.get("ID")},
                # {"FieldID": "field2", "FieldValue": record.get("PIDM")},
                {"FieldID": "field3", "FieldValue": record.get("DOCUMENTTYPE")},
                {"FieldID": "field4", "FieldValue": record.get("LASTNAME")},
                {"FieldID": "field5", "FieldValue": record.get("FIRSTNAME")},
                {"FieldID": "field6", "FieldValue": record.get("SSN")},
                {"FieldID": "field7", "FieldValue": record.get("BIRTHDATE")},
                {"FieldID": "field8", "FieldValue": record.get("TERMCODE")},
                {"FieldID": "field9", "FieldValue": record.get("APPLICATIONNUMBER")},
                {"FieldID": "field10", "FieldValue": record.get("ADMISSIONSREQUIREMENT")},
                {"FieldID": "field11", "FieldValue": record.get("INSTITUTIONNUMBER")},
                {"FieldID": "field12", "FieldValue": record.get("ROUTINGSTATUS")},
                # {"FieldID": "field13", "FieldValue": record.get("ACTIVITYDATE")},
                {"FieldID": "field14", "FieldValue": record.get("COMMENTS")},
                {"FieldID": "field15", "FieldValue": record.get("LASTUPDATE")},
                # {"FieldID": "field16", "FieldValue": record.get("DISPOSITIONDATE")},
                {"FieldID": "field17", "FieldValue": record.get("RECRUITERID")},
                {"FieldID": "field18", "FieldValue": record.get("RECRUITDOCUMENTID")},
                {"FieldID": "field19", "FieldValue": record.get("STUDENTSTATUSPERTRANSCRIPT")},
                {"FieldID": "field20", "FieldValue": record.get("PENDINGADMINISTRATIVEMATTERYN")},
                {"FieldID": "field21", "FieldValue": record.get("CODEOFCONDUCTVIOLATIONYN")},
            ],
            "links": []
        },
        "NativeNewIndex": None,
        "FromBatch": None,
        "BatchPageNum": 0,
        "PageRange": None,
        "MergeDuplicateIndex": False,
        "IgnoreDuplicateIndex": False,
        "IgnoreDlsViolation": False,
        "SubmitFullText": True
    }

# --------------------------------------------------------------
# AppXtender Upload
# --------------------------------------------------------------
def upload_to_appxtender(record, dry_run=False):
    attempts = config.get("appxtender_retry_attempts", 3)
    delay = config.get("appxtender_retry_delay", 5)

    if dry_run:
        log_upload_event(
            "appxtender_upload_dry_run",
            record,
            attempts=attempts
        )
        logging.info(
            f"DRY RUN: Would upload {record['DocumentFileName']} to AppXtender"
        )
        return {"success": True, "document_id": None}

    metadata = build_appxtender_metadata(record)

    for attempt in range(1, attempts + 1):
        log_upload_event(
            "appxtender_upload_attempt",
            record,
            attempt=attempt,
            max_attempts=attempts
        )

        try:
            with open(record["local_file"], "rb") as fh:
                files = {
                    "data": (
                        None,
                        json.dumps(metadata),
                        "application/vnd.emc.ax+json; charset=utf-8"
                    ),
                    "bin": (
                        os.path.basename(record["local_file"]),
                        fh,
                        "application/bin"
                    )
                }

                response = requests.post(
                    config["appxtender_url"],
                    files=files,
                    auth=HTTPBasicAuth(
                        config["appxtender_user"],
                        config["appxtender_pass"]
                    ),
                    timeout=60
                )

            # Successful upload
            if response.status_code in (200, 201):
                doc_id = None
                try:
                    resp_json = response.json()
                    doc_id = resp_json.get("ID")
                except Exception:
                    pass

                log_upload_event(
                    "appxtender_upload_success",
                    record,
                    document_id=doc_id,
                    http_status=response.status_code
                )

                logging.info(
                    f"AppXtender upload success: "
                    f"{record['DocumentFileName']} | DocumentID={doc_id}"
                )
                return {"success": True, "document_id": doc_id}
            
            # --- DUPLICATE INDEX (Error 125) ---
            try:
                resp_json = response.json()
            except Exception:
                resp_json = {}

            error_code = resp_json.get("ErrorCode")
            error_msg = resp_json.get("Message", "")

            if error_code == 125 or "duplicate index" in error_msg.lower():
                log_upload_event(
                    "appxtender_upload_duplicate",
                    record,
                    error_code=error_code,
                    message=error_msg,
                    http_status=response.status_code
                )

                logging.warning(
                    f"Duplicate index detected for {record['DocumentFileName']} "
                    f"(Error 125). Treating as already imported."
                )

                # Treat as success
                return {
                    "success": True,
                    "document_id": None,
                    "duplicate": True
                }

            log_upload_event(
                "appxtender_upload_retry",
                record,
                attempt=attempt,
                http_status=response.status_code,
                response=response.text
            )

            logging.warning(
                f"AppXtender upload failed "
                f"({response.status_code}) for "
                f"{record['DocumentFileName']}: {response.text}"
            )

        except Exception as e:
            log_upload_event(
                "appxtender_upload_exception",
                record,
                attempt=attempt,
                error=str(e)
            )
            logging.warning(
                f"AppXtender upload exception "
                f"({attempt}/{attempts}) for "
                f"{record['DocumentFileName']} "
                f"using URL {config['appxtender_url']}: {e}"
            )

        if attempt < attempts:
            time.sleep(delay)

    log_upload_event(
        "appxtender_upload_failed",
        record,
        attempts=attempts
    )

    # send_failure_email(
    #     "Slate to AppXtender: Upload Failed",
    #     f"{record['DocumentFileName']} failed after {attempts} attempts"
    # )

    return {"success": False, "error": "Upload failed after retries"}

# --------------------------------------------------------------
# Load Imported Records
# --------------------------------------------------------------
def load_imported_records():
    if not os.path.exists(IMPORTED_RECORD_FILE):
        return {}

    try:
        with open(IMPORTED_RECORD_FILE, "r") as f:
            content = f.read().strip()
            if not content:
                return {}
            return json.loads(content)
    except json.JSONDecodeError:
        logging.warning(
            f"Imported records file is empty or corrupt: "
            f"{IMPORTED_RECORD_FILE}. Starting fresh."
        )
        return {}

def save_imported_records(records):
    with open(IMPORTED_RECORD_FILE, "w") as f:
        json.dump(records, f)

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
        # send_failure_email("Slate to BDM: Slate API Failure", str(e))
        return []

# --------------------------------------------------------------
# Download File with Retries
# --------------------------------------------------------------
def download_file(url, filename):
    filepath = os.path.join(DOWNLOAD_DIR, filename)

    if os.path.exists(filepath):
        logging.info(f"File already downloaded: {filename}")
        return filepath

    attempts = config.get("download_retry_attempts", 5)
    delay = config.get("download_retry_delay", 5)

    for attempt in range(1, attempts + 1):
        logging.info(f"Downloading file ({attempt}/{attempts}): {filename}")

        try:
            r = requests.get(url, stream=True, timeout=15)

            if r.status_code == 200:
                with open(filepath, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                return filepath

            logging.warning(f"Download failed (HTTP {r.status_code}): {filename}")

        except Exception as e:
            logging.warning(f"Error downloading file {filename}: {e}")

        if attempt < attempts:
            time.sleep(delay)

    # Failed all retries
    msg = f"Failed to download file after {attempts} attempts: {filename}"
    logging.error(msg)
    #send_failure_email("Slate to BDM: File Download Failure", msg)
    return None

# --------------------------------------------------------------
# Archive Files
# --------------------------------------------------------------
def archive_files(records):
    for r in records:
        src = r["local_file"]
        dst_dir = os.path.join(ARCHIVE_DIR, datetime.now().strftime("%Y/%m/%d"))
        os.makedirs(dst_dir, exist_ok=True)
        shutil.move(src, os.path.join(dst_dir, os.path.basename(src)))
        logging.info(f"Archived file: {os.path.basename(src)}")

# --------------------------------------------------------------
# Cleanup Old Archives
# --------------------------------------------------------------
def cleanup_archives():
    cutoff = datetime.now() - timedelta(days=config["days_to_keep_archive"])

    for root, dirs, _ in os.walk(ARCHIVE_DIR):
        for dirname in dirs:
            full = os.path.join(root, dirname)
            mtime = datetime.fromtimestamp(os.path.getmtime(full))
            if mtime < cutoff:
                shutil.rmtree(full)
                logging.info(f"Removed old archive: {full}")

# --------------------------------------------------------------
# Main Logic
# --------------------------------------------------------------
def main():
    global imported_records

    imported_records = load_imported_records()

    parser = argparse.ArgumentParser(description="Slate to BDM AX Import Script")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose console output")
    parser.add_argument("--dry-run", action="store_true", help="Do not upload or archive files, just simulate")
    args = parser.parse_args()

    dry_run = args.dry_run

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        for handler in logging.getLogger().handlers:
            handler.setLevel(logging.DEBUG)
        logging.debug("Verbose mode enabled.")

    slate_rows = fetch_slate_results()
    if not slate_rows:
        logging.info("No Slate data returned.")
        return

    processed = []

    for row in slate_rows:
        fname = row["DocumentFileName"]

        if fname in imported_records:
            logging.info(f"Skipping previously imported file: {fname}")
            continue

        url = row["FileURL"]
        file_path = download_file(url, fname)

        if not file_path:
            logging.error(f"Skipping record; download failed: {fname}")
            continue

        row["local_file"] = file_path
        processed.append(row)

    if not processed:
        logging.info("No new files to import.")
        return

    successful_records = []
    for r in processed:
        result = upload_to_appxtender(r, dry_run=dry_run)

        if dry_run:
            logging.info(f"DRY RUN: Simulated upload for {r['DocumentFileName']}")

        filename = r["DocumentFileName"]
        now_ts = datetime.now(timezone.utc).isoformat()

        if result.get("success"):
            if not dry_run:
                imported_records[filename] = {
                    "document_id": result.get("document_id"),
                    "uploaded_at": now_ts
                }
                save_imported_records(imported_records)
            else:
                logging.info(f"DRY RUN: {filename} would be recorded as imported")
            successful_records.append(r)
        else:
            logging.warning(
                f"Upload failed for {filename}; not recording in imported_records.json"
            )

    if successful_records and not dry_run:
        archive_files(successful_records)

    cleanup_archives()

if __name__ == "__main__":
    main()