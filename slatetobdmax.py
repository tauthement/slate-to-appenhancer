import os
import json
import csv
import shutil
import logging
import requests
import subprocess
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText

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
LOG_FILE = config["log_file"]
IMPORTED_RECORD_FILE = os.path.join(WORK_DIR, "imported_records.json")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(ARCHIVE_DIR, exist_ok=True)
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# --------------------------------------------------------------
# Logging Setup
# --------------------------------------------------------------
logging.basicConfig(
    filename=LOG_FILE,
    filemode="a",
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)

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
# Load Imported Records
# --------------------------------------------------------------
def load_imported_records():
    if not os.path.exists(IMPORTED_RECORD_FILE):
        return set()
    with open(IMPORTED_RECORD_FILE, "r") as f:
        return set(json.load(f))

def save_imported_records(records):
    with open(IMPORTED_RECORD_FILE, "w") as f:
        json.dump(list(records), f)

imported_records = load_imported_records()

# --------------------------------------------------------------
# Fetch Slate Data
# --------------------------------------------------------------
def fetch_slate_results():
    url = f"{config['slate_api_url']}?query_id={config['slate_query_id']}"
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
        send_failure_email("Slate to BDM: Slate API Failure", str(e))
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
    send_failure_email("Slate to BDM: File Download Failure", msg)
    return None

# --------------------------------------------------------------
# Build CSV for Index Importer
# --------------------------------------------------------------
def build_csv(records):
    csv_path = os.path.join(WORK_DIR, "import.csv")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "ID", "PIDM", "DOCUMENT TYPE",
            "LAST NAME", "FIRST NAME", "BIRTH DATE",
            "TERM CODE", "APPLICATION NUMBER", "FILENAME"
        ])

        for r in records:
            line = (
                f"{r['ID']},"
                f"{r['PIDM']},"
                f"\"{r['DOCUMENTTYPE']}\","
                f"\"{r['LASTNAME']}\","
                f"\"{r['FIRSTNAME']}\","
                f"\"{r['BIRTHDATE']}\","
                f"{r['TERMCODE']},"
                f"{r['APPLICATION NUMBER']}@@{r['local_file']}"
            )
            f.write(line + "\n")

    logging.info(f"CSV built: {csv_path}")
    return csv_path

# --------------------------------------------------------------
# Run IndexImageImport.exe
# --------------------------------------------------------------
def run_importer(csv_path):
    cmd = [
        config["importer_path"],
        "/U", config["import_user"],
        "/W", config["import_pass"],
        "/A", config["import_application"],
        "/S", f"\"{config['import_specification']}\"",
        "/N", config["import_datasource"],
        "/K 1", # skip the header row
        "I", # allow others to add documents to application while import is running
        "/F", csv_path
    ]

    logging.info(f"Running Importer: {' '.join(cmd)}")

    proc = subprocess.run(
        " ".join(cmd),
        shell=True,
        capture_output=True,
        text=True
    )

    if proc.returncode == 0:
        logging.info("Importer completed successfully.")
        return True

    msg = f"Importer failed: {proc.stdout}\n{proc.stderr}"
    logging.error(msg)
    send_failure_email("Slate to BDM: Importer Failure", msg)
    return False

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

    csv_path = build_csv(processed)

    success = run_importer(csv_path)

    if success:
        archive_files(processed)

        for r in processed:
            imported_records.add(r["DocumentFileName"])
        save_imported_records(imported_records)

        os.remove(csv_path)

    cleanup_archives()

if __name__ == "__main__":
    main()