import os
import time
import random
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# =====================================================
# CONFIGURATION & PORTABLE PATH RESOLUTION
# =====================================================

# Add your own paths here, or just drop service_account.json next to this script.
DEFAULT_SERVICE_ACCOUNT_PATHS = [
    os.path.join(os.path.dirname(__file__), "service_account.json"),
]

# Add your own paths here, or just drop the PDF you want to fax next to this script.
DEFAULT_PDF_PATHS = [
    os.path.join(os.path.dirname(__file__), "fax_document.pdf"),
]

def resolve_existing_path(paths_list, name="File"):
    for p in paths_list:
        if os.path.exists(p):
            return p
    return paths_list[0]  # Fallback to default if none exists yet

SERVICE_ACCOUNT_FILE = resolve_existing_path(DEFAULT_SERVICE_ACCOUNT_PATHS, "Service Account JSON")
FILE_TO_ATTACH = resolve_existing_path(DEFAULT_PDF_PATHS, "PDF Attachment")

# Paste the full URL of the Google Sheet holding the fax list.
# The service account must be shared on that sheet as an Editor.
SHEET_URL = "ENTER GOOGLE SHEET URL HERE"

BASE_URL = "https://app.ringcentral.com/fax/fax/all"

MAX_FAX = 100          # Stop after this many SEND ATTEMPTS (dialog-open attempts) in one run
UPLOAD_TIMEOUT = 60    # max seconds to wait for PDF upload to finish
SHEET_RETRIES = 3      # retry attempts for Google Sheet updates


# =====================================================
# HUMAN-LIKE DELAY
# =====================================================

def random_sleep(min_sec=2, max_sec=5):
    """Sleep for a random duration to mimic human behaviour."""
    delay = random.uniform(min_sec, max_sec)
    time.sleep(delay)


# =====================================================
# SELENIUM LOCATORS
# =====================================================

NEW_FAX_BTN = (
    By.CSS_SELECTOR,
    '[data-test-automation-id="open-new-fax-dialog-button"]'
)

FAX_INPUT = (
    By.CSS_SELECTOR,
    'input[placeholder="Enter a fax number or a contact number"]'
)

FILE_INPUT = (
    By.CSS_SELECTOR,
    'input[data-test-automation-id="upload-file-input"]'
)

REMOVE_FILE_BTN = (
    By.CSS_SELECTOR,
    '[data-test-automation-id="attachment-action-button"] button'
)

ATTACHMENT_ITEM = (
    By.CSS_SELECTOR,
    '[data-test-automation-class="faxDialog__attachmentListItem"]'
)

INVALID_CHIP = (
    By.CSS_SELECTOR,
    '[data-test-automation-class="selected-item"][data-is-error="true"]'
)

ALL_CHIPS = (
    By.CSS_SELECTOR,
    '[data-test-automation-class="selected-item"]'
)

CHIP_REMOVE_BTN = (
    By.CSS_SELECTOR,
    '[data-test-automation-id="chip-remove"]'
)

SEND_NOW_BTN = (
    By.CSS_SELECTOR,
    '[data-test-automation-id="faxDialogSendNowButton"]'
)

CANCEL_BTN = (
    By.CSS_SELECTOR,
    '[data-test-automation-id="faxDialogCancelButton"]'
)


# =====================================================
# GOOGLE SHEET HELPERS
# =====================================================

def connect_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=scopes
    )
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_url(SHEET_URL)
    return spreadsheet.sheet1


def ensure_tracking_columns(ws):
    headers = ws.row_values(1)
    required_columns = ["Status", "Sent Date", "Sent Time", "Notes"]
    changed = False

    for col in required_columns:
        if col not in headers:
            headers.append(col)
            changed = True

    if changed:
        ws.update("A1", [headers])

    return headers


def get_column_index(headers, column_name):
    return headers.index(column_name) + 1


def build_row_dict(headers, row_data):
    """Safely build dictionary from row_data handling case and missing keys."""
    row_dict = {}
    for idx, header in enumerate(headers):
        val = row_data[idx] if idx < len(row_data) else ""
        row_dict[header] = val
        row_dict[header.strip().lower()] = val
    return row_dict


def update_rows_batch(ws, headers, row_updates):
    """
    Performs a single batch cell update to Google Sheets across multiple rows.
    row_updates: list of dicts [{'row_number': int, 'status': str, 'notes': str, ...}]
    Retries up to SHEET_RETRIES times. Returns True on success, False on failure.
    """
    if not row_updates:
        return True

    now = datetime.now()
    default_date_str = now.strftime("%m/%d/%Y")
    default_time_str = now.strftime("%I:%M:%S %p")

    status_col = get_column_index(headers, "Status")
    date_col   = get_column_index(headers, "Sent Date")
    time_col   = get_column_index(headers, "Sent Time")
    notes_col  = get_column_index(headers, "Notes")

    cells = []
    for item in row_updates:
        row_num = item["row_number"]
        status  = item["status"]
        notes   = item["notes"]
        d_str   = item.get("date_str", default_date_str)
        t_str   = item.get("time_str", default_time_str)

        cells.extend([
            gspread.Cell(row_num, status_col, status),
            gspread.Cell(row_num, date_col,   d_str),
            gspread.Cell(row_num, time_col,   t_str),
            gspread.Cell(row_num, notes_col,  notes),
        ])

    for attempt in range(1, SHEET_RETRIES + 1):
        try:
            ws.update_cells(cells)
            return True
        except Exception as e:
            print(f"Sheet batch update failed (attempt {attempt}/{SHEET_RETRIES}): {e}")
            if attempt < SHEET_RETRIES:
                time.sleep(5 * attempt)

    print(f"*** WARNING *** Could not write batch update for {len(row_updates)} row(s).")
    return False


# =====================================================
# UTILITIES & PHONE NORMALIZATION
# =====================================================

def clean_fax(fax):
    """
    Strips non-digits and normalizes US 11-digit numbers starting with '1'
    to standard 10-digit format for strict deduplication.
    """
    if not fax:
        return ""
    fax = str(fax).strip()
    if not fax:
        return ""
    
    digits = "".join(ch for ch in fax if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def is_valid_fax(fax):
    """Basic US fax number validation — exactly 10 digits after normalization."""
    return len(fax) == 10


def classify_field(fax_clean, sent_fax_numbers):
    """Classifies ONE field (Primary or Secondary) before any sending happens."""
    if not fax_clean:
        return "empty"
    if not is_valid_fax(fax_clean):
        return "invalid"
    if fax_clean in sent_fax_numbers:
        return "already_sent"
    return "needs_sending"


def field_note_text(label, final_status):
    """Builds the human-readable note fragment for one field."""
    mapping = {
        "empty":         f"{label} Not available.",
        "invalid":       f"{label} Skipped because format is invalid.",
        "already_sent":  f"{label} Already sent.",
        "sent":          f"{label} Sent successfully.",
        "rejected":      f"{label} Rejected by RingCentral.",
        "needs_sending": f"{label} Pending transmission.",
    }
    return mapping.get(final_status, f"{label} Pending.")


def build_notes(primary_final, secondary_final):
    parts = [
        field_note_text("Primary", primary_final),
        field_note_text("Secondary", secondary_final),
    ]
    if primary_final in ("already_sent", "sent") and secondary_final in ("already_sent", "sent"):
        parts.append("All numbers transmitted.")
    elif primary_final == "already_sent" and secondary_final == "already_sent":
        parts.append("No new transmission was required.")
    return " ".join(parts)


# =====================================================
# GLOBAL DEDUPLICATION INDEXING
# =====================================================

def build_fax_to_rows_map(headers, all_rows):
    """
    Scans the whole worksheet ONCE and builds:
        fax_to_rows_map = { "2151111111": [5, 88, 250], ... }
    Links every normalized fax number to all row numbers where it appears.
    """
    fax_map = {}
    for idx, row_data in enumerate(all_rows[1:], start=2):
        row_dict = build_row_dict(headers, row_data)
        row_faxes = set()
        for field in ("Primary Fax", "Secondary Fax", "primary fax", "secondary fax"):
            fax = clean_fax(row_dict.get(field, ""))
            if fax and is_valid_fax(fax):
                row_faxes.add(fax)

        for fax in row_faxes:
            fax_map.setdefault(fax, []).append(idx)

    dup_count = sum(1 for rows in fax_map.values() if len(rows) > 1)
    print(
        f"Fax index built: {len(fax_map)} unique valid fax numbers, "
        f"{dup_count} numbers appear on multiple rows."
    )
    return fax_map


def preload_already_sent(headers, all_rows):
    """
    Seeds sent_fax_numbers from rows already marked 'Sent' in Google Sheets.
    Also captures the original 'Sent Date' and 'Sent Time' for each fax number
    so duplicate rows preserve the exact original transmission timestamp.
    """
    sent_fax_numbers = set()
    fax_sent_info = {}  # fax_number -> {"date_str": str, "time_str": str}

    for row_data in all_rows[1:]:
        row_dict = build_row_dict(headers, row_data)
        if row_dict.get("Status", "").strip().lower() != "sent":
            continue

        notes = row_dict.get("Notes", "").lower()
        d_str = row_dict.get("Sent Date", "") or row_dict.get("sent date", "")
        t_str = row_dict.get("Sent Time", "") or row_dict.get("sent time", "")

        primary_fax = clean_fax(row_dict.get("Primary Fax", ""))
        secondary_fax = clean_fax(row_dict.get("Secondary Fax", ""))

        if primary_fax and is_valid_fax(primary_fax):
            if not any(k in notes for k in ("primary skipped", "primary invalid", "primary rejected")):
                sent_fax_numbers.add(primary_fax)
                if d_str and t_str:
                    fax_sent_info[primary_fax] = {"date_str": d_str, "time_str": t_str}

        if secondary_fax and is_valid_fax(secondary_fax):
            if not any(k in notes for k in ("secondary skipped", "secondary invalid", "secondary rejected", "secondary not available")):
                sent_fax_numbers.add(secondary_fax)
                if d_str and t_str:
                    fax_sent_info[secondary_fax] = {"date_str": d_str, "time_str": t_str}

    print(f"Preloaded {len(sent_fax_numbers)} unique fax number(s) already marked Sent from earlier runs.")
    return sent_fax_numbers, fax_sent_info


def compute_row_status_and_notes(headers, row_data, sent_fax_numbers):
    """Calculates status and notes for any row based on current sent_fax_numbers."""
    row_dict = build_row_dict(headers, row_data)
    primary_raw   = clean_fax(row_dict.get("Primary Fax", ""))
    secondary_raw = clean_fax(row_dict.get("Secondary Fax", ""))

    p_status = classify_field(primary_raw, sent_fax_numbers)
    s_status = classify_field(secondary_raw, sent_fax_numbers)

    present_statuses = [s for s in (p_status, s_status) if s != "empty"]
    both_empty = len(present_statuses) == 0
    all_invalid = len(present_statuses) > 0 and all(s == "invalid" for s in present_statuses)
    success_present = p_status == "already_sent" or s_status == "already_sent"

    if both_empty:
        status_label = "Skipped"
        note = "Fax Not Available"
    elif all_invalid:
        status_label = "Invalid"
        note_parts = []
        if p_status == "invalid":
            note_parts.append("Primary fax invalid.")
        if s_status == "invalid":
            note_parts.append("Secondary fax invalid.")
        note = " ".join(note_parts)
    elif success_present:
        status_label = "Sent"
        note = build_notes(p_status, s_status)
    else:
        status_label = "Pending"
        note = build_notes(p_status, s_status)

    return status_label, note


# =====================================================
# SELENIUM HELPERS & CHIP CLEARING
# =====================================================

def clear_all_recipient_chips(driver):
    """Removes all recipient chips (valid or invalid) currently in the fax dialog."""
    try:
        chips = driver.find_elements(*ALL_CHIPS)
        if chips:
            print(f"Clearing {len(chips)} existing recipient chip(s) from dialog...")
            for chip in chips:
                try:
                    remove_btn = chip.find_element(*CHIP_REMOVE_BTN)
                    driver.execute_script("arguments[0].click();", remove_btn)
                    random_sleep(0.3, 0.8)
                except Exception:
                    pass
            random_sleep(1, 2)
    except Exception as e:
        print(f"Error clearing existing chips: {e}")


def open_new_fax(driver, wait):
    """
    Safely opens a new fax dialog. If a dialog is already open on screen,
    reuses it and clears all old recipient chips.
    """
    dialog_open = False
    try:
        inputs = driver.find_elements(*FAX_INPUT)
        if inputs and inputs[0].is_displayed():
            dialog_open = True
            print("Fax dialog is already open on screen. Reusing existing dialog.")
    except Exception:
        dialog_open = False

    if not dialog_open:
        btn = wait.until(EC.element_to_be_clickable(NEW_FAX_BTN))
        random_sleep(0.5, 1.2)
        driver.execute_script("arguments[0].click();", btn)
        random_sleep(2, 4)

    # Always clear any draft or leftover recipient chips in the dialog
    clear_all_recipient_chips(driver)


def clear_fax_input(field):
    """Fully clear text from the fax input field."""
    field.send_keys(Keys.CONTROL, "a", Keys.BACKSPACE)
    random_sleep(0.5, 1.0)


def count_valid_chips(driver):
    """Count recipient chips that RingCentral accepted as valid."""
    all_chips = driver.find_elements(*ALL_CHIPS)
    valid = [
        c for c in all_chips
        if c.get_attribute("data-is-error") != "true"
    ]
    return len(valid)


def enter_fax_numbers(driver, wait, fax_numbers):
    """
    Enters each number and checks whether RingCentral accepted it for THAT specific number.
    Returns (valid_count, rejected_numbers).
    """
    field = wait.until(
        EC.element_to_be_clickable(FAX_INPUT)
    )
    rejected = []

    for fax in fax_numbers:
        clear_fax_input(field)
        field.send_keys(fax)
        random_sleep(1, 2)
        field.send_keys(Keys.ENTER)
        random_sleep(2, 3)

        # Check for invalid chip specifically associated with this entered number
        invalid_chips = driver.find_elements(*INVALID_CHIP)
        for chip in invalid_chips:
            chip_val = clean_fax(chip.get_attribute("data-test-automation-value") or "")
            if chip_val == fax:
                print(f"RingCentral rejected number: {fax}")
                rejected.append(fax)
                try:
                    remove_btn = chip.find_element(*CHIP_REMOVE_BTN)
                    driver.execute_script("arguments[0].click();", remove_btn)
                    random_sleep(1, 2)
                except Exception as e:
                    print(f"Could not remove invalid chip for {fax}: {e}")

    valid_count = count_valid_chips(driver)
    print(f"Valid recipients in dialog: {valid_count} | Rejected: {rejected}")
    return valid_count, rejected


def remove_existing_attachment(driver, wait):
    """Remove any file already attached in the fax dialog before uploading."""
    try:
        remove_btns = driver.find_elements(*REMOVE_FILE_BTN)
        if remove_btns:
            print(f"Found {len(remove_btns)} existing attachment(s). Removing...")
            for btn in remove_btns:
                random_sleep(0.5, 1.2)
                driver.execute_script("arguments[0].click();", btn)
                random_sleep(1, 2)
            print("Existing attachment(s) removed.")
    except Exception:
        pass


def upload_pdf(driver, wait):
    remove_existing_attachment(driver, wait)
    file_input = wait.until(
        EC.presence_of_element_located(FILE_INPUT)
    )
    random_sleep(1, 3)
    file_input.send_keys(FILE_TO_ATTACH)

    print("Waiting for PDF upload confirmation...")
    upload_wait = WebDriverWait(driver, UPLOAD_TIMEOUT)
    upload_wait.until(
        EC.presence_of_element_located(ATTACHMENT_ITEM)
    )
    print("Upload confirmed.")
    random_sleep(1, 2)


def send_fax(driver, wait):
    send_btn = wait.until(
        EC.presence_of_element_located(SEND_NOW_BTN)
    )
    random_sleep(1, 2)
    driver.execute_script("arguments[0].click();", send_btn)
    print("Fax sent.")
    random_sleep(3, 5)


def close_fax_dialog(driver, wait):
    """Close the fax dialog if it's still open (e.g., after a failure)."""
    try:
        cancel_btns = driver.find_elements(*CANCEL_BTN)
        if cancel_btns:
            random_sleep(0.5, 1.5)
            driver.execute_script("arguments[0].click();", cancel_btns[0])
            print("Dialog closed via Cancel.")
            random_sleep(2, 4)
    except Exception as e:
        print(f"Could not close dialog: {e}")


# =====================================================
# PROCESS ROW & MULTI-ROW PROPAGATION
# =====================================================

def propagate_and_update(ws, headers, all_rows, current_row_num, fax_numbers_to_propagate, fax_to_rows_map, sent_fax_numbers, current_row_update, fax_sent_info=None):
    """
    Finds ALL rows sharing any of fax_numbers_to_propagate, re-computes their status,
    and updates them in Google Sheets alongside current_row_update in a single batch.
    Preserves original Sent Date & Time if fax was sent on a previous run.
    """
    if fax_sent_info is None:
        fax_sent_info = {}

    rows_to_update_map = {current_row_num: current_row_update}

    for fax in fax_numbers_to_propagate:
        matching_rows = fax_to_rows_map.get(fax, [])
        info = fax_sent_info.get(fax, {})
        for r_num in matching_rows:
            if r_num not in rows_to_update_map:
                r_data = all_rows[r_num - 1]
                st_label, note = compute_row_status_and_notes(headers, r_data, sent_fax_numbers)
                item = {
                    "row_number": r_num,
                    "status": st_label,
                    "notes": note
                }
                if "date_str" in info and "time_str" in info:
                    item["date_str"] = info["date_str"]
                    item["time_str"] = info["time_str"]
                rows_to_update_map[r_num] = item

    updates_list = list(rows_to_update_map.values())
    if len(updates_list) > 1:
        print(f"Propagating update to {len(updates_list) - 1} duplicate row(s) sharing sent fax numbers...")

    success = update_rows_batch(ws, headers, updates_list)
    return success


def process_row(
    driver,
    wait,
    ws,
    headers,
    row_data,
    row_number,
    all_rows,
    fax_to_rows_map,
    sent_fax_numbers,
    fax_sent_info
):
    try:
        row_dict = build_row_dict(headers, row_data)
        primary_raw   = clean_fax(row_dict.get("Primary Fax", ""))
        secondary_raw = clean_fax(row_dict.get("Secondary Fax", ""))

        print("=" * 52)
        print(f"Processing Row {row_number}")
        print(f"Primary Fax:   {primary_raw or '(none)'}")
        print(f"Secondary Fax: {secondary_raw or '(none)'}")

        primary_status   = classify_field(primary_raw, sent_fax_numbers)
        secondary_status = classify_field(secondary_raw, sent_fax_numbers)

        needs_sending = []
        for raw, status in ((primary_raw, primary_status), (secondary_raw, secondary_status)):
            if status == "needs_sending" and raw not in needs_sending:
                needs_sending.append(raw)

        present_statuses = [s for s in (primary_status, secondary_status) if s != "empty"]
        both_empty = len(present_statuses) == 0
        all_invalid = len(present_statuses) > 0 and all(s == "invalid" for s in present_statuses)

        rejected_this_row = []
        sent_this_row = []

        now = datetime.now()
        current_date_str = now.strftime("%m/%d/%Y")
        current_time_str = now.strftime("%I:%M:%S %p")

        if needs_sending:
            print(f"Numbers requiring transmission: {needs_sending}")
            print("Opening RingCentral dialog...")

            open_new_fax(driver, wait)

            valid_count, rejected_this_row = enter_fax_numbers(driver, wait, needs_sending)

            if valid_count == 0:
                print(f"Row {row_number}: All numbers rejected by RingCentral.")
                close_fax_dialog(driver, wait)
            else:
                sent_this_row = [n for n in needs_sending if n not in rejected_this_row]
                upload_pdf(driver, wait)
                send_fax(driver, wait)
                sent_fax_numbers.update(sent_this_row)
                for n in sent_this_row:
                    fax_sent_info[n] = {"date_str": current_date_str, "time_str": current_time_str}
                print(f"Successfully sent: {sent_this_row}")
        else:
            print("No new transmission needed for this row.")

        def resolve(raw, status):
            if status != "needs_sending":
                return status
            if raw in sent_this_row:
                return "sent"
            if raw in rejected_this_row:
                return "rejected"
            return status

        primary_final   = resolve(primary_raw, primary_status)
        secondary_final = resolve(secondary_raw, secondary_status)

        success_present = (
            primary_final in ("sent", "already_sent")
            or secondary_final in ("sent", "already_sent")
        )

        if both_empty:
            status_label = "Skipped"
            note = "Fax Not Available"
        elif all_invalid:
            status_label = "Invalid"
            note_parts = []
            if primary_status == "invalid":
                note_parts.append("Primary fax invalid.")
            if secondary_status == "invalid":
                note_parts.append("Secondary fax invalid.")
            note = " ".join(note_parts)
        elif success_present:
            status_label = "Sent"
            note = build_notes(primary_final, secondary_final)
        else:
            status_label = "Invalid"
            note = build_notes(primary_final, secondary_final)

        current_update = {
            "row_number": row_number,
            "status": status_label,
            "notes": note,
            "date_str": current_date_str,
            "time_str": current_time_str
        }

        # Propagate sent numbers to duplicate rows
        faxes_to_propagate = [n for n in (primary_raw, secondary_raw) if n and is_valid_fax(n)]
        write_success = propagate_and_update(
            ws, headers, all_rows, row_number, faxes_to_propagate, fax_to_rows_map, sent_fax_numbers, current_update, fax_sent_info
        )

        if not write_success:
            print(f"*** ERROR *** Row {row_number}: Could not write status to Google Sheet.")
            return "failed"

        if status_label == "Sent":
            return "sent"
        return "skipped"

    except SystemExit:
        raise

    except Exception as e:
        print(f"Row {row_number} Failed with error: {str(e)}")
        close_fax_dialog(driver, wait)

        err_update = [{
            "row_number": row_number,
            "status": "Failed",
            "notes": f"Error during send attempt: {str(e)}"
        }]
        update_rows_batch(ws, headers, err_update)
        return "failed"


# =====================================================
# MAIN ENTRYPOINT
# =====================================================

def main():
    print("\nConnecting Google Sheet...")
    ws = connect_sheet()
    headers = ensure_tracking_columns(ws)
    print("Google Sheet Connected.")

    if not os.path.exists(FILE_TO_ATTACH):
        raise FileNotFoundError(f"PDF attachment not found:\n{FILE_TO_ATTACH}")
    print(f"PDF Found: {FILE_TO_ATTACH}")

    all_rows = ws.get_all_values()

    # Step 1: Scan sheet ONCE, build global fax -> row index map
    fax_to_rows_map = build_fax_to_rows_map(headers, all_rows)

    # Step 2: Seed sent_fax_numbers with prior Sent rows & capture original timestamps
    sent_fax_numbers, fax_sent_info = preload_already_sent(headers, all_rows)

    # Step 3: Startup Sweep — Instantly update any duplicate rows (like Row 400) matching previously sent faxes
    preloaded_updates = []
    for fax in sent_fax_numbers:
        matching_rows = fax_to_rows_map.get(fax, [])
        info = fax_sent_info.get(fax, {})
        for r_num in matching_rows:
            r_data = all_rows[r_num - 1]
            st_val = build_row_dict(headers, r_data).get("status", "").strip().lower()
            if st_val not in ("sent", "skipped", "invalid"):
                st_label, note = compute_row_status_and_notes(headers, r_data, sent_fax_numbers)
                item = {
                    "row_number": r_num,
                    "status": st_label,
                    "notes": note
                }
                if "date_str" in info and "time_str" in info:
                    item["date_str"] = info["date_str"]
                    item["time_str"] = info["time_str"]
                preloaded_updates.append(item)

    if preloaded_updates:
        print(f"Startup Sweep: Instantly updating {len(preloaded_updates)} duplicate row(s) matching previously sent faxes...")
        update_rows_batch(ws, headers, preloaded_updates)
        # Refresh all_rows local cache after batch update
        all_rows = ws.get_all_values()

    chrome_options = Options()
    chrome_options.debugger_address = "localhost:9222"

    try:
        driver = webdriver.Chrome(options=chrome_options)
    except Exception as e:
        print("\n*** ERROR: Could not connect to Chrome on localhost:9222 ***")
        print("Please start Chrome with remote debugging enabled before running this script.")
        print(r'Command: "chrome.exe --remote-debugging-port=9222"')
        raise e

    wait = WebDriverWait(driver, 20)
    driver.get(BASE_URL)
    print("Chrome Attached successfully.")

    sent_count = 0
    failed_count = 0
    skipped_count = 0

    for row_number, row_data in enumerate(all_rows[1:], start=2):
        if (sent_count + failed_count) >= MAX_FAX:
            print(f"\nLimit reached: {MAX_FAX} send attempts in this run. Stopping.")
            break

        row_dict = build_row_dict(headers, row_data)
        status_value = row_dict.get("status", "").strip().lower()

        if status_value in ("sent", "skipped", "invalid"):
            print(f"Row {row_number}: Already processed ({status_value.capitalize()})")
            continue

        result = process_row(
            driver,
            wait,
            ws,
            headers,
            row_data,
            row_number,
            all_rows,
            fax_to_rows_map,
            sent_fax_numbers,
            fax_sent_info
        )

        if result == "sent":
            sent_count += 1
        elif result == "failed":
            failed_count += 1
        elif result == "skipped":
            skipped_count += 1

        if result in ("sent", "failed"):
            print("Pausing before next fax attempt...")
            random_sleep(15, 40)

    print(
        f"\nRun Completed — Sent: {sent_count} | "
        f"Failed: {failed_count} | Skipped: {skipped_count}"
    )
    print(
        f"Total unique fax numbers transmitted in memory: {len(sent_fax_numbers)}"
    )


if __name__ == "__main__":
    main()