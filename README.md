# RingCentral Fax Automation

Faxes a PDF to every number in a Google Sheet through the RingCentral web app, and
writes the outcome of each send back to the sheet.

![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white) ![Selenium](https://img.shields.io/badge/Selenium-43B02A?logo=selenium&logoColor=white) ![License](https://img.shields.io/badge/License-MIT-2ea44f)

## What it does

Reads a fax list from a Google Sheet, sends your PDF to each number in turn, then
records the result against that row. Because status is written back **as it goes**,
an interrupted run leaves a clear record of exactly which numbers were reached.

Sends are paced with human-like delays rather than fired back to back - bulk
dispatch at machine speed tends to get throttled.

## Requirements

- Python 3.9+
- Google Chrome, signed in to RingCentral
- A Google service account with access to your sheet (`service_account.json`)
- `selenium`, `gspread`, `google-auth`

## Install

```bash
pip install -r requirements.txt
```

## Configure

Edit the `CONFIGURATION` block at the top of `RingCentral_Fax_Auto.py`:

| Setting | What it is |
|---|---|
| `SHEET_URL` | The Google Sheet holding your fax list |
| `MAX_FAX` | Cap on sends per run |
| `DEFAULT_SERVICE_ACCOUNT_PATHS` | Where to find `service_account.json` |
| `DEFAULT_PDF_PATHS` | The PDF to attach |

The simplest setup is to drop `service_account.json` and your PDF next to the
script - the defaults already look there.

## Run

Sign in to RingCentral in Chrome first, then:

```bash
python RingCentral_Fax_Auto.py
```

## Notes on data

This repository contains **no client or patient data**. `.gitignore` already excludes
`.xlsx`, `.pdf` and `.csv` files, `service_account.json`, and chromedriver binaries -
keep it that way if you fork this.

## License

MIT © Muhammad Sharaz Khalid - see [LICENSE](LICENSE).
