# card-dues

Track what you owe on your Indian credit cards, in one place, on your own machine.

It reads statement emails from Gmail (read-only), unlocks the password-protected
PDFs, extracts the total due, minimum due, payment due date and the individual
transactions per card, and shows them in a local dashboard. Anything it cannot
read, you type in yourself. Dates are written and read as dd/mm/yyyy throughout.

![The card-dues dashboard](docs/screenshot.png)

## What this can and cannot tell you

**It shows your billed statement amount, not a live balance.** Spending after the
statement date is not included. Every figure carries its statement date, its
source and an as-of timestamp, and a card whose statement is older than 40 days
is flagged as stale rather than quietly shown as settled.

There is no way around this for a personal project:

- **Account Aggregator** has a credit-card schema (`totalDueAmount`, `minDueAmount`,
  `creditLimit`, `availableCredit`) but no issuer has activated the credit-card
  FI type, and access requires being a regulated FIU.
- **Bharat Connect (BBPS) bill fetch** does return the billed amount, minimum due
  and due date across most large issuers, but only to a registered BBPS agent
  under a Customer Operating Unit. That means business onboarding through a
  provider such as Setu or Cashfree.
- **Issuer APIs** exist but are per-bank partner integrations.

The data model uses ReBIT's field names so a BBPS or AA source can be added later
as just another `source` alongside `statement` and `manual`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m carddues init
```

Optionally install the `carddues` command: `.venv/bin/pip install -e .`

### Connect Gmail

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project
   and enable the **Gmail API**.
2. On the OAuth consent screen, choose **External**, and add your own Gmail
   address under **Test users**. The app stays unverified, which is fine for one
   user.
3. Create an OAuth client. Either type works:
   - **Desktop app** — nothing else to configure. Google allows the loopback
     redirect this app uses.
   - **Web application** — add this authorised redirect URI:

     ```
     http://127.0.0.1:8765/oauth/callback
     ```

4. Download the JSON and save it as `~/.carddues/credentials.json`.
5. Start the dashboard (`.venv/bin/python -m carddues serve`) and click
   **Connect Gmail**. Google's consent screen opens, and the redirect back
   finishes the connection — no terminal step.

If you serve on a different port, the dashboard prints the exact redirect URI to
register. A desktop client can also be authorised from the terminal with
`.venv/bin/python -m carddues auth`.

The scope requested is `gmail.readonly`, so this can list and download mail but
never send, modify or delete it. The token is stored at `~/.carddues/token.json`
with owner-only permissions. **Disconnect Gmail** on the dashboard forgets that
token; to revoke the grant itself, remove the app from your
[Google account permissions](https://myaccount.google.com/permissions).

## Use

```bash
# Register a card. Name and date of birth are used to derive PDF passwords.
# Adding a card also retries the statements still waiting to be opened.
carddues cards add --issuer hdfc --last4 8765 --label "HDFC Infinia" \
                   --limit 500000 --name "Naveen Kumar" --dob 01/07/1990

# Fetch and parse statements from Gmail
carddues ingest --show

# Or parse a PDF you already have
carddues import ~/Downloads/statement.pdf --password mypassword

# Type in anything the parser could not read; manual entries always win
carddues set 8765 --total 45231.50 --min 2270 --due 03/08/2026

# Record a payment (defaults to the full outstanding amount)
carddues paid 8765 --amount 20000

# Open a statement that stayed locked; the password is kept for next month
carddues unlock 'NAVE1504' --file statement.pdf

# Work through the backlog of waiting attachments, a batch at a time
carddues reprocess --issuer hdfc

carddues show          # table in the terminal
carddues show --json   # machine readable
carddues log           # what the last ingest did with each attachment

carddues serve         # dashboard on http://127.0.0.1:8765
```

### Daily refresh and notifications

`carddues notify` sends a macOS notification when a card is overdue or due within
three days. To run it every morning, edit the path in
`scripts/com.carddues.refresh.plist` and load it:

```bash
cp scripts/com.carddues.refresh.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.carddues.refresh.plist
```

## How parsing works

`carddues/parsers/base.py` handles the two layouts statements actually use:

- **Inline** — `Total Amount Due : Rs. 1,12,480.35`
- **Table** — a row of labels with the values in the row beneath, aligned by
  column position so an extra unlabelled column does not shift everything

Issuer parsers in `carddues/parsers/banks.py` only declare which wording that
issuer prefers; the shared extractor does the rest. Amounts understand Indian
digit grouping and a trailing `Cr` (a credit balance, stored as negative).

`carddues/parsers/transactions.py` reads the line items. A statement that rules
its transactions into a grid names its columns, so those are read first, category
column included. The rest print one transaction per line, opening with a date and
closing with an amount, which is what separates a transaction from the summary
rows above it — a trailing reward-points column or a second posting date does not
confuse it. Most Indian statements print no category at all, so the merchant name
is matched against a keyword list and the result is marked **guess** in the table
to keep it apart from a category the issuer itself printed.

To add an issuer, add an entry to `carddues/issuers.py` with its sending domains
and a text marker, then a small class in `banks.py` if its wording is unusual.
Unknown issuers fall through to the generic parser, which handles most formats.

## How locked statements are opened

Issuers state the password rule in the covering mail, so `carddues/passwords.py`
reads it rather than guessing. It looks only at the sentences mentioning the
password, breaks the rule into components — "first 4 letters of your name in
capitals", "date of birth in DDMM", "last 4 digits of your card" — and fills them
from the card you registered. "NAVE1504" comes out of one attempt instead of
roughly 190.

Three things happen when that isn't enough. A rule needing details you never
supplied (five card digits, when only the last four are on file) is refused
rather than half-guessed. Anything unrecognised falls back to the permutations of
name, date of birth, PAN and card digits that the module generated before. And if
none of those open the file, the PDF is kept on disk and listed on the dashboard
with a password box; whatever you type there is remembered on the card, so the
next month's statement opens unattended.

A statement from a card you never registered is the hardest case, because there
are no details to build a password from at all. For those, the dashboard shows
who sent the mail, its subject and date, and quotes the password rule it stated,
so you can work the password out yourself and type it once. Unlocking registers
the card automatically from the statement, so it only happens the first time.
Those details are captured during the fetch, so attachments logged by an earlier
version show none until they are tried again — and they are, since every card you
add sets the waiting attachments going through the same route, re-downloading each
one from its mail when it is no longer on disk. A pass takes 40 attachments and
says how many are left, so **Try these again** on the dashboard (or
`carddues reprocess`) works through a long backlog without a request that never
ends. The pass covers that issuer's attachments plus any whose issuer was never
recorded, which is what older log rows look like.

## What the dashboard shows per card

The latest statement, always. When more than one cycle is stored, **Statements**
lists them newest first and picking one shows that month's figures, with a way
back to the latest. **Show transactions** opens the line items of whichever
statement is on screen — date, merchant, category and amount, credits netted off
in the total — and closes them again.

## Where the data lives

Everything is in `~/.carddues/`: a SQLite database, the OAuth token, and
downloaded PDFs (deleted after parsing, except locked ones kept for a retry, or
all of them with `--keep-files`). Nothing is sent anywhere. Set `CARDDUES_HOME`
to move it.

This project never asks for net banking credentials and does no screen scraping.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The suite covers the parsers against realistic statement layouts, real encrypted
PDF unlocking, password rules read from real issuer wordings, transaction and
category extraction, the statement history picker, retrying a backlog against a
stubbed Gmail, the due-resolution rules, and the dashboard routes.
