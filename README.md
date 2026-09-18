# GST portal tools

Three single-file scripts that pull a taxpayer's **own** filed GST data off the GST
portal into a folder on their own machine. No vendor, no account, no subscription,
no data goes to any cloud, directly to your machine.

| File | What it does |
|---|---|
| `gst_fetch.py` | GSTR-1 summary, GSTR-3B liability/offset, GSTR-2B — a whole FY |
| `gst_fetch_r2x.py` | GSTR-2X, "TDS and TCS credit received" — the deductee side of GST TDS u/s 51 |
| `gst_cdp.py` | one command per call against the same browser — for exploring, not routine use |

## What these are and are not

They drive **a browser you have already logged into yourself**, over the Chrome
DevTools Protocol on `127.0.0.1`. They are the equivalent of clicking Download on
each page and filing the result tidily.

- **Read-only.** Every call is a GET. Nothing accepts, rejects, submits or files.
- **Nothing is transmitted anywhere.** The scripts talk to `127.0.0.1:9222` and to
  `gst.gov.in`. There is no server, no telemetry, no account.
- **No credentials are handled.** You log in. The script never sees a password, an
  OTP or a DSC PIN, and cannot.
- **Fetched from inside the page**, so it is same-origin — which is why no auth
  token has to be captured or replayed.

Decide for yourself whether automated access suits your engagement terms and your
reading of the portal's terms of use. This is a professional convenience tool; it
is not endorsed by or affiliated with GSTN.

## Setup

```
pip install playwright
```

That is all. Playwright's own browser download (`playwright install`) is **not**
needed — these attach to a browser you start yourself, they never launch one.

## Running

Start a Chromium with remote debugging, on a **dedicated profile**. Edge or Chrome;
both speak CDP and the scripts do not care which:

```
msedge.exe --remote-debugging-port=9222 ^
           --user-data-dir=%LOCALAPPDATA%\gst_browser ^
           --disable-extensions ^
           --disable-component-extensions-with-background-pages ^
           --disable-features=EdgeImportOnFirstRun,ImportBrowsingDataOnFirstRun ^
           https://services.gst.gov.in/services/login
```

Log in. Stop at the welcome page. Then:

```
set GST_LANDING_ROOT=D:\GST data          (optional; default is .\GSTN Portal Data)

python gst_fetch.py     --fy 2024-25
python gst_fetch.py     --fy 2024-25 --status-only      compliance sweep, writes nothing
python gst_fetch_r2x.py --fy 2024-25
```

### `--disable-extensions` is not optional

Edge's first-run import copies your **default browser's extensions** into a
brand-new `--user-data-dir`. `--no-first-run` suppresses the first-run *screen*,
not the import. A fresh Chrome profile still picks up registry-installed ones.

A profile created exactly this way came up carrying seven extensions — three with
`<all_urls>`, one holding `cookies`, and one adware. Any of those can read the
portal session and every invoice row you fetch. Check before you log in:

```
curl http://127.0.0.1:9222/json/list
```

If anything there has an `extension://` URL, close the browser, delete the profile
folder and start again.

### Never type an `/auth/` URL

Navigating straight to a portal `/auth/` address returns "Access Denied" **and kills
the logged-in session behind it**. Recovery costs a fresh login. Always arrive by
clicking a link already on the page. `gst_fetch.py` refuses such a navigation
outright rather than trusting anyone to remember.

## Output

```
<root>/FY 2024-25/<GSTIN>/
    GSTR1/    R1_summary_<MMYYYY>.json      (_IFF suffix for QRMP months 1-2)
    GSTR2B/   2B_<MMYYYY>.json
    GSTR3B/   3B_taxpayble_<MMYYYY>.json
    GSTR2X/   2X_summary_<MMYYYY>.json, 2X_tds_<MMYYYY>.json
    _status/  rolestatus_<MMYYYY>.json, 2xrolestatus_<MMYYYY>.json
    _pull_log.json, _pull_r2x_log.json
```

No payload is ever printed — only byte counts and paths — so client rows stay out
of terminals, transcripts and CI logs.

## Four things that are expensive to rediscover

1. **The portal answers HTTP 200 with an error envelope.**
   `{"status_cd":"0","error":{...}}` parses like data and would import as zero.
   Every response is checked for the envelope, not just the status code — and files
   already on disk are re-checked, because "exists" is not "is good".
2. **A quarterly (QRMP) filer is not in arrears.** For `userPref == "Q"`, GSTR-3B is
   due only in Jun/Sep/Dec/Mar, and GSTR-1 in between is the optional IFF. Treating
   every `NF` as a default reported a fully compliant taxpayer as eight returns
   overdue.
3. **GSTR-2B is a different origin** (`gstr2b.gst.gov.in`). Its payload cannot be
   fetched from a `return.*` page; the script clicks through once, then fetches
   every period.
4. **`2B_122024` may not be December.** From Oct-2024 QRMP filers stopped getting
   monthly 2Bs, so a quarter-end file can span three months, and a late-filing
   supplier puts an August invoice in September's 2B. Figures stay right because ITC
   keys on `rtnprd` — the period the credit *became available*. Never infer the span
   from the filename; `_pull_log.json` records `gstr2b_kind` and `r1_kind`.

### And two specific to GSTR-2X

5. **`NF` usually means nobody deducted, not a default.** A nil GSTR-2X is not
   required. A period is worth acting on only when it is `NF` *and* carries records —
   that combination is unclaimed cash, because accepting TDS credit moves money into
   the electronic cash ledger. (The portal's help text says a nil return *can't* be
   filed; that is not literally true — nil periods have been filed and given ARNs. It
   simply need not be. Treat neither its absence nor its presence as an error.)
6. **`chksum` is not unique across periods.** It is computed over record content, so
   a deductor who deducts the same amount in two months yields the same chksum.
   Never use it as a key.
7. **An error is not an empty section.** `RETWEB_04` "No invoices found!!" is a
   genuine nil; anything else — `RETWEB_001` "Unable to connect to server" turns up
   transiently — is a failure. Both arrive as HTTP 200 with an envelope. Counting a
   failure as zero lets a real record hide behind a blip, so a failed period is
   reported UNKNOWN and never folded into "nothing to claim". Re-run clears it.
8. **Every period gets a dated `2X_evidence_<MMYYYY>.json`, including nil ones.**
   Silence is what lets an unfiled period sit for years: with nothing written for an
   empty period, a folder cannot distinguish "checked, nothing there" from "never
   looked". The evidence file carries the status, ARN, per-section counts, the exact
   portal message, and a plain verdict line.

## Invoice numbers in GSTR-2X are period-dependent

A record is `{ctin, ctin_name, amt_ded, iamt, camt, samt, flag, month, chksum}` —
deductor-wise monthly totals, with **no invoice number** — for periods before the
portal's invoice-wise cutover. From the cutover onward the records carry `inum`
(invoice no) and `idt` (invoice date); the portal gates this on the return period,
not a feature flag. Any consumer must branch on the period rather than assume a
shape, or it will silently drop `inum` on newer data and report older data as
unmatched.

## The .exe

**Built and working** (2026-09-18, PyInstaller 6.19, Playwright 1.60, Python 3.13):
`gst-portal-tools.exe`, one file, **45.3 MB**, no Python install needed on the
target machine. It still needs a Chromium started with the flags above — it drives
your browser, it does not carry one.

**Double-click it and you get a menu** — no command line needed:

```
  1   Open the GST browser   (do this FIRST, and log in there)
  2   Check what is unfiled  - GST TDS / GSTR-2X, writes nothing
  3   Download GST TDS       - GSTR-2X for a whole year
  4   Download GSTR-1/3B/2B  - for a whole year
```

Press **1**; it opens the browser and then *waits* while you log in. Press Enter
when you're in and it carries straight on to the next step. Every action returns to
the menu, so the window stays open until you choose 0.

**Where your files go:** before any download it shows the folder and lets you change
it. The default is a `GSTN Portal Data` folder **beside the program**, or
`$GST_LANDING_ROOT` if you set it. If that folder cannot be written to — running from
`C:\Program Files`, say — it falls back to your Documents and tells you so, *before*
fetching rather than after.

**Nothing office-specific is baked in.** The browser profile goes to
`%LOCALAPPDATA%\gst-portal-tools\<browser>`, overridable with `$GST_BROWSER_PROFILE`
or `--profile`, and the landing folder is always yours to pick.

That matters because a console .exe double-clicked from Explorer prints its output
into a window that closes instantly — it looks like a crash. The exe detects that it
owns the console and switches to the menu, then waits. **A one-file exe runs as two
processes** (bootloader + app), so the "am I alone on this console?" test must allow
for two when frozen and one when not; getting that wrong is exactly what made an
earlier build still flash shut.

From a terminal nothing changes — the shell is on the console too, so no menu and no
pause:

```
gst-portal-tools browser
gst-portal-tools fetch --fy 2025-26
gst-portal-tools r2x   --fy 2025-26 --status-only
gst-portal-tools cdp   url
```

One executable with four sub-commands, not four executables, because PyInstaller
bundles Playwright's Node driver into whatever it builds; four exes would be four
copies of the same 107 MB driver.

**Startup takes about 5 seconds.** A one-file build unpacks that driver to a temp
folder on every run. Build `--onedir` instead if that becomes annoying: it starts
immediately, at the cost of a folder rather than a single file.

### Building it yourself

```
pip install pyinstaller playwright
pyinstaller --noconfirm --clean gst_portal_tools.spec
```

Do not build straight onto a mapped or sync-backed drive — see below. Point
`--distpath` / `--workpath` at a local folder if you are on one.

Then **prove the driver made it in**, because a build that omits it looks perfectly
fine until someone runs a real pull. With nothing listening on port 9222:

```
dist\gst-portal-tools.exe r2x --fy 2025-26 --status-only
```

The right answer is a *connection refusal from Playwright* (`connect_over_cdp …
ECONNREFUSED`). A missing-driver error instead means the bundle is incomplete.

**Three things that will bite whoever rebuilds this:**

- **Do not build onto `Z:`.** It gets to the last step and dies with
  `EndUpdateResourceW: The system cannot read from the specified device` — Windows'
  resource-update API is unreliable on a mapped / sync-backed drive. `_build_exe.ps1`
  builds in `%TEMP%` and copies the result back.
- **`collect_all('playwright')` is mandatory**, and is in the .spec. Without it the
  build *succeeds* and then dies at run time with a missing-driver error. The build
  script therefore verifies the driver by running the exe with nothing on port 9222:
  the correct answer is a connection refusal from Playwright, not a driver error.
- **The three tools are imported by name**, so PyInstaller cannot see them from the
  launcher. They are declared as hidden imports in the .spec; drop one and that
  sub-command fails only when somebody runs it.

An earlier note here estimated 100 MB+ and predicted `--onefile` would fail. The
measured size is 45.3 MB — PyInstaller compresses the archive — and the build
succeeds as specified. Corrected rather than left standing.

**Shipping the `.py` files plus `pip install playwright` is still the better default**
for anyone who can run Python: it is smaller, auditable, and lets people see that
nothing is sent anywhere — which, for a tool that sits next to a tax login, is worth
more than convenience. The .exe is for machines where installing Python is the
obstacle.

## Licence

**Apache License 2.0** — see `LICENSE` and `NOTICE`.

Copyright 2026 Deccan StatBooks (DSB).

Apache-2.0 rather than MIT for two reasons that matter for a tool sitting next to a
tax login: it carries an **express patent grant**, and its trademark clause means
using the code grants nobody the right to trade on the DSB name. You may use,
modify and redistribute it, including commercially, provided you keep the licence
and copyright notice and state any changes you made.

The licence text was copied verbatim from an Apache-2.0 file already installed on
the build machine and checked against two further independent copies — the same
rule `dsb_notices_build.py` applies to third-party notices, and for the same
reason: a licence retyped from memory is a licence with a typo in it.

**Playwright is not vendored here.** It is installed separately by the user
(`pip install playwright`) and is itself Apache-2.0; its copyright travels with
that installation. Nothing else is vendored — see `NOTICE`.

### If you build the .exe

PyInstaller bundles Playwright's Node driver, so the binary **does** redistribute
Apache-2.0 material. Ship `LICENSE` and `NOTICE` alongside the .exe (or as bundled
data, the way `dsb_26061909_spine.spec` ships `THIRD_PARTY_NOTICES.txt`).
Attribution is the one obligation every licence in this tree imposes; none asks
for a royalty.
