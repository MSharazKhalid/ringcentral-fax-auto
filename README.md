# RingCentral Fax Automation

Reads a fax list from a Google Sheet, sends a PDF to each number
through RingCentral in the browser, and writes the result back to the
sheet. Uses human-like delays between sends.

## Setup

```
pip install -r requirements.txt
```

## Run

Sign in to RingCentral in Chrome first, then:

```
python RingCentral_Fax_Auto.py
```

## What to edit before running

The `CONFIGURATION` block at the top - `SHEET_URL`, `MAX_FAX`, and the
paths for `service_account.json` and the PDF to attach.

## Never commit

Patient or client data (`.xlsx`, `.pdf`, `.csv`), `service_account.json`,
and chromedriver binaries. All are covered by `.gitignore`.
