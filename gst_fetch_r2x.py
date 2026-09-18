"""
gst_fetch_r2x.py - pull a client's GSTR-2X ("TDS and TCS credit received") off
the portal into the landing folder, using an already-logged-in Chromium.

    python gst_fetch_r2x.py --fy 2024-25
    python gst_fetch_r2x.py --fy 2024-25 --status-only
    python gst_fetch_r2x.py --fy 2024-25 --refresh

Prerequisite - a Chromium started with remote debugging on the dedicated
profile, and YOU logged in to the portal in it. Chrome OR Edge; both speak CDP
and Playwright's connect_over_cdp does not care which:

    chrome.exe  --remote-debugging-port=9222 --user-data-dir=%LOCALAPPDATA%\\gst-portal-tools\\gst_chrome  --disable-extensions
    msedge.exe  --remote-debugging-port=9222 --user-data-dir=%LOCALAPPDATA%\\gst-portal-tools\\gst_edge    --disable-extensions

--disable-extensions is NOT optional hygiene. Edge's first-run import copies the
default browser's extensions into a brand-new --user-data-dir (--no-first-run
suppresses the first-run SCREEN, not the import), and a fresh Chrome profile
still receives registry-installed ones. An extension holding `cookies` and
`<all_urls>` can read the portal session and every invoice row fetched here.

Output, alongside the GSTR-1/2B/3B tree that gst_fetch.py writes, under --root,
or $GST_LANDING_ROOT, or ./GSTN Portal Data:

    <root>\\FY 2024-25\\<GSTIN>\\
        GSTR2X\\  2X_summary_<MMYYYY>.json
                  2X_tds_<MMYYYY>.json   (+ _tdsa, _tcs, _tcsa when non-empty)
        _status\\ 2xrolestatus_<MMYYYY>.json
        _pull_r2x_log.json

The folder is GSTR2X, not GSTR7. GSTR-7 is the DEDUCTOR's return; what we can
see as the deductee is GSTR-2X. The handover primer said GSTR7; the portal's own
naming wins and the primer is a pointer, not an authority.

Five differences from the GSTR-1/3B family. Every one of them is a silent-wrong
answer if missed:

1. NAMESPACE. Most GSTR-2X calls live under returns2/auth/api/, not
   returns/auth/api/. formdetails is the exception and stays on returns/.
2. PARAMETER NAME. 2xrolestatus takes return_prd=; the summary and the record
   endpoints take rtn_prd=. Same MMYYYY value, two spellings.
3. ERROR ENVELOPE. This family reports success as {"status": 1, ...}. It does
   NOT use status_cd, so gst_fetch.py's portal_error() - which treats a missing
   status_cd as fine and a status_cd of "0" as empty - reads it wrongly in both
   directions. Hence r2x_error() below.
4. NF IS NOT ARREARS. The portal's own help text on the comptds page: "You can't
   file nil return if there are no auto populated TDS/TCS details from GSTR 7/8
   (Filing of nil return is not required)." A month in which nobody deducted
   carries NO OBLIGATION. Reporting every NF as an overdue return would have KJ
   chasing a client over a return that never existed - the same shape of mistake
   as treating a QRMP filer's non-filing months as defaults.

   Note the help text's first clause is not literally true: two wholly nil
   periods (Oct and Nov 2025, totalRec 0, nothing accepted or rejected) were
   filed by hand and the portal issued ARNs for both. So a nil GSTR-2X CAN be
   filed - it simply need not be. Do not treat the absence of a nil filing as a
   default, and do not treat its presence as an error either.
5. THE INVOICE NUMBER IS PERIOD-DEPENDENT. Before the portal's invoice-wise
   cutover a record is {ctin, ctin_name, amt_ded, iamt, camt, samt, flag, month,
   chksum, editable} - deductor-wise monthly totals with no invoice number at
   all. From the cutover onward it also carries `inum` and `idt`; the portal
   gates this on the RETURN PERIOD, not a feature flag (gstr2xctrl.js:17
   compares the period as YYYYMM against a server-configured cutover). A
   consumer must branch on the period, never assume a shape - one written
   against old data silently drops `inum`, one written against new data reports
   every old row as unmatched. Do not feed a blank `inv` downstream: it
   reconciles to nothing and reads as a problem rather than an absent join.
6. SKIP-IF-EXISTS APPLIES ONLY TO A FILED PERIOD. A filed GSTR-2X carries an ARN
   and is closed; an unfiled one changes the moment anyone files it. Skipping on
   file-presence alone leaves a stored NF saying NF forever - and it would keep
   reporting a period as unclaimed cash after the money had been claimed. Seen
   live: a Dec-2024 return sat unfiled for 21 months, was filed by hand, and the
   landing folder would never have noticed.

As with gst_fetch.py: this writes no .dsb, so it is safe to run while the DSB
app has a book open (rule G5), and no payload passes through stdout - only byte
counts and paths, so client rows stay out of transcripts and logs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

def _playwright():
    """Imported lazily, and ONLY by the fetching path.

    A module-level `sys.exit` on a missing playwright made this file unsafe to
    import: DSB reuses the parsers below (section_rows, r2x_error) to read the
    landed JSON, and an import that can call sys.exit would take the whole app
    down on a machine without playwright. Fetching still fails exactly as before
    - just at the moment of fetching rather than at import - and the parsers,
    which need no browser at all, stay importable. Rule G1: one definition of
    the GSTR-2X envelope and record shape, not a second copy inside the app.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright not importable")
    return sync_playwright

CDP = "http://127.0.0.1:9222"

# No office-specific path baked in, so the same file runs here and anywhere it
# is handed on. DSB always passes --root explicitly, so this default only ever
# applies to someone running the script by hand.
DEFAULT_ROOT = Path(os.environ.get("GST_LANDING_ROOT")
                    or (Path.cwd() / "GSTN Portal Data"))

RET = "https://return.gst.gov.in"

# The four auto-drafted sections of GSTR-2X. tds/tcs are the credits themselves;
# tdsa/tcsa are amendments the deductor filed against an earlier month.
SECTIONS = ("tds", "tdsa", "tcs", "tcsa")

# The portal's code for "this section genuinely holds nothing". Anything ELSE
# coming back in the error envelope is a failure, not an empty - RETWEB_001
# ("Unable to connect to server") has appeared transiently on live pulls. The
# two must never collapse into the same answer.
NO_RECORDS_CD = "RETWEB_04"

FETCH_TEXT_JS = """
async (url) => {
  try {
    const r = await fetch(url, { credentials: 'include' });
    return { status: r.status, text: await r.text() };
  } catch (e) { return { error: String(e) }; }
}
"""

CLICKHREF_JS = """
(needle) => {
  const a = [...document.querySelectorAll('a[href]')]
      .find(x => (x.getAttribute('href') || '').includes(needle));
  if (!a) return null;
  a.click();
  return a.getAttribute('href');
}
"""


def fy_periods(fy: str) -> list[str]:
    """'2024-25' -> ['042024'..'122024', '012025'..'032025'] (MMYYYY).

    GSTR-2X is MONTHLY for everyone. A QRMP filer files GSTR-1/3B quarterly but
    still receives TDS credit month by month, so this list is not filtered by
    userPref the way gst_fetch.py's is.
    """
    try:
        y1 = int(fy.split("-")[0])
    except Exception:
        sys.exit(f"bad --fy {fy!r}, expected like 2024-25")
    return ([f"{m:02d}{y1}" for m in range(4, 13)] +
            [f"{m:02d}{y1 + 1}" for m in range(1, 4)])


def period_label(prd: str) -> str:
    m = int(prd[:2])
    return f"{['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][m]}-{prd[2:]}"


def get_json(page, url):
    """Fetch from inside the page - same-origin, so no `at` token, no forged
    headers. Returns (raw_text, parsed)."""
    res = page.evaluate(FETCH_TEXT_JS, url)
    if res.get("error"):
        return None, {"_error": res["error"]}
    if res.get("status") != 200:
        return None, {"_error": f"HTTP {res['status']}"}
    txt = res["text"]
    try:
        return txt, json.loads(txt)
    except Exception as e:
        return txt, {"_error": f"not JSON: {e}"}


def r2x_error(parsed):
    """The GSTR-2X envelope, which is NOT gst_fetch.py's envelope.

    Success is {"status": 1, "data": {...}}. There is no status_cd, so reusing
    portal_error() here would call every good response "no data key" or wave an
    error through. Kept as a separate function rather than a flag on the old one
    so neither can drift into the other's family.
    """
    if not isinstance(parsed, dict):
        return "not a JSON object"
    if parsed.get("_error"):
        return parsed["_error"]
    err = parsed.get("error")
    if isinstance(err, dict):
        return f"{err.get('error_cd') or err.get('errorCode') or 'ERR'}: " \
               f"{(err.get('message') or '')[:120]}"
    if "data" not in parsed:
        return f"no data key (got {list(parsed)[:4]})"
    if str(parsed.get("status", "")) not in ("1", "True"):
        return f"status={parsed.get('status')!r}"
    return None


def usable_file(path: Path) -> bool:
    """Is a file already on disk real data, or junk to re-fetch?

    Same reasoning as gst_fetch.py: the landing folder is on a sync drive where
    a delete has proved not to be reliably final, so presence is not proof.
    """
    if not path.exists():
        return False
    try:
        j = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return r2x_error(j) is None


def save(path: Path, txt: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(txt, encoding="utf-8")
    return len(txt)


def section_rows(parsed, sec: str) -> list:
    """The record list for one section, or []. Shape is
    data.processed.<sec>[] - `processed` because the portal distinguishes
    records it has processed from ones still in flight."""
    d = (parsed or {}).get("data") or {}
    proc = d.get("processed") or {}
    rows = proc.get(sec)
    return rows if isinstance(rows, list) else []


def stored_is_filed(path: Path) -> bool:
    """Does the summary ALREADY on disk carry an ARN?

    An ARN is the portal's proof the return was filed, so a stored summary that
    has one can never change and is safe to skip. One that lacks it was written
    while the period was unfiled, and is stale the moment anybody files.
    """
    if not path.exists():
        return False
    try:
        j = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool((((j.get("data") or {}).get("summary") or {}).get("arn")))


def verdict_of(status: str, n_rec: int, unknown: bool, arn) -> str:
    """One plain line per period, so a folder can be read without inference.

    UNKNOWN comes first deliberately: a period the portal would not answer for
    must never be reported as nil, because nil is the answer that makes someone
    stop looking.
    """
    if unknown:
        return ("UNKNOWN - the portal failed on at least one section; "
                "re-run before relying on this")
    if status == "FIL":
        return (f"FILED - {n_rec} record(s)"
                + (f", ARN {arn}" if arn else ", ARN not in payload"))
    if n_rec:
        return (f"UNCLAIMED - {n_rec} record(s) auto-drafted, return NOT filed; "
                f"this is cash sitting on the portal")
    return ("NIL - checked, the portal holds no TDS/TCS credit for this period. "
            "Filing is not required; nothing is outstanding.")


def status_of(parsed) -> tuple[str, str]:
    """(GSTR2X status, due date) out of a 2xrolestatus payload."""
    d = (parsed or {}).get("data") or {}
    for grp in d.get("user") or []:
        for r in grp.get("returns") or []:
            if r.get("return_ty") == "GSTR2X":
                return r.get("status") or "-", r.get("due_dt") or "-"
    return "-", "-"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fy", required=True, help="e.g. 2024-25")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--status-only", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()

    periods = fy_periods(a.fy)
    root = Path(a.root)

    with _playwright()() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(CDP)
        except Exception as e:
            sys.exit(f"cannot attach on {CDP}: {e}\n"
                     f"Start Chrome or Edge with --remote-debugging-port=9222 "
                     f"--disable-extensions and log in.")

        pages = [p for c in browser.contexts for p in c.pages]
        page = next((p for p in pages if "gst.gov.in" in (p.url or "")), None)
        if page is None:
            sys.exit("no gst.gov.in tab open - log in to the portal first")

        # Must be on a return.gst.gov.in auth page for the fetches to be
        # same-origin. Get there by CLICKING - typing an /auth/ URL returns
        # Access Denied AND kills the session behind it.
        if "return.gst.gov.in" not in page.url:
            if not page.evaluate(CLICKHREF_JS, "returns/auth/dashboard"):
                sys.exit("no 'Returns Dashboard' link on this page - go to the "
                         "post-login welcome page and retry")
            page.wait_for_timeout(6000)
        if "accessdenied" in page.url or "/login" in page.url:
            sys.exit("session is not live - log in again")

        gstin = None
        log = {"fy": a.fy, "form": "GSTR2X",
               "pulled_at": datetime.now().isoformat(timespec="seconds"),
               "root": str(root), "periods": {}}
        table = []

        for prd in periods:
            raw_rs, rs = get_json(
                page, f"{RET}/returns2/auth/api/2xrolestatus?return_prd={prd}")
            bad = r2x_error(rs)
            if bad:
                table.append((prd, "-", "-", bad))
                continue

            st, due = status_of(rs)
            table.append((prd, st, due, ""))
            entry = {"GSTR2X": st, "due_dt": due, "files": {}, "counts": {}}

            # The summary carries the GSTIN, and is worth keeping in its own
            # right: it holds the ARN and chksum of a filed return.
            raw_sum, summ = get_json(
                page, f"{RET}/returns2/auth/api/gstr2x/summary"
                      f"?rtn_prd={prd}&rtn_typ=GSTR2X")
            sbad = r2x_error(summ)
            if not sbad and gstin is None:
                gstin = (((summ.get("data") or {}).get("summary") or {})
                         .get("gstin"))

            # --status-only writes NOTHING, but it still fetches the record
            # sections, because for GSTR-2X the counts ARE the compliance answer.
            # "NF" on its own is the normal state of a month in which nobody
            # deducted; the only actionable finding is NF *with records*, which
            # is unclaimed cash. A sweep that reported status without counts
            # would say "eight unfiled" about a taxpayer who owes nothing and
            # stay silent about the one month holding real money.
            cdir = root / f"FY {a.fy}" / (gstin or "UNKNOWN_GSTIN")
            writing = not a.status_only

            if raw_rs and writing:
                save(cdir / "_status" / f"2xrolestatus_{prd}.json", raw_rs)

            # Skip-if-exists applies ONLY to a period that is already FILED.
            #
            # A filed GSTR-2X cannot change - it carries an ARN and is closed. An
            # UNFILED one changes the moment anybody files it, and the stored copy
            # then says NF with no ARN forever, because usable_file() sees valid
            # JSON and skips. That is exactly what happened here: a Dec-2024
            # return sat unfiled, was filed by hand, and a re-run would have kept
            # reporting it as unclaimed cash.
            #
            # Not the same as GSTR-2B, where skip-if-exists is right because a 2B
            # is an immutable statement. The question is not "have I got a file"
            # but "can the answer still move".
            # A period is safe to skip only when the STORED copy is itself from a
            # filed return - i.e. it already carries an ARN. Testing the LIVE
            # status is not enough: a file saved while the period was NF, which
            # is then filed, has a live status of FIL and a stale body, so
            # status-alone would skip it and preserve the NF forever. That is
            # the Dec-2024 case exactly.
            #
            # Not the same as GSTR-2B, where skip-if-exists is right because a 2B
            # is an immutable statement. The question is never "have I got a
            # file" but "can the answer still move".
            sumpath = cdir / "GSTR2X" / f"2X_summary_{prd}.json"
            stable = (st == "FIL") and stored_is_filed(sumpath)

            def _skip(p):
                return stable and usable_file(p) and not a.refresh

            if not writing:
                entry["files"]["summary"] = "not written (--status-only)"
            elif raw_sum and not sbad:
                entry["files"]["summary"] = (
                    "skipped (filed, unchanged)" if _skip(sumpath)
                    else {"bytes": save(sumpath, raw_sum)})
            else:
                entry["files"]["summary"] = f"unavailable - {sbad}"

            # The four record sections.
            #
            # An error is NOT an empty section. The portal says RETWEB_04 "No
            # invoices found!!" when a section genuinely holds nothing, and
            # RETWEB_001 "Unable to connect to server" when it simply failed -
            # and it has failed transiently on this client more than once. Both
            # arrive as the same 200-with-envelope. Counting either as zero
            # would let a real record hide behind a blip and report the period
            # as nothing to claim. So a genuine empty is recorded as 0, and a
            # failure as None, which the verdict treats as UNKNOWN, not as nil.
            sections = {}
            unknown = False
            for sec in SECTIONS:
                raw, j = get_json(
                    page, f"{RET}/returns2/auth/api/gstr2x/{sec}?rtn_prd={prd}")
                jbad = r2x_error(j)
                if jbad:
                    if NO_RECORDS_CD in jbad:
                        sections[sec] = {"records": 0, "portal": jbad}
                        entry["counts"][sec] = 0
                    else:
                        sections[sec] = {"records": None, "portal": jbad}
                        entry["counts"][sec] = None
                        unknown = True
                    continue
                rows = section_rows(j, sec)
                sections[sec] = {"records": len(rows), "portal": "ok"}
                entry["counts"][sec] = len(rows)
                if not rows or not writing:
                    continue
                out = cdir / "GSTR2X" / f"2X_{sec}_{prd}.json"
                if _skip(out):
                    entry["files"][sec] = "skipped (filed, unchanged)"
                else:
                    entry["files"][sec] = {"bytes": save(out, raw)}

            arn = (((summ.get("data") or {}).get("summary") or {}).get("arn")
                   if not sbad else None)
            n_rec = sum(v["records"] for v in sections.values()
                        if isinstance(v["records"], int))
            entry["verdict"] = verdict_of(st, n_rec, unknown, arn)
            entry["unknown"] = unknown

            # An evidence file for EVERY period, including the empty ones.
            #
            # Silence is the thing that cost 21 months here: with nothing written
            # for a nil period, the folder cannot distinguish "checked, nothing
            # there" from "never looked", so no one can be confident a period was
            # dealt with. A dated per-period record removes that doubt without
            # anyone having to reason about which files are missing and why.
            if writing:
                ev = {
                    "gstin": gstin, "period": prd, "period_label": period_label(prd),
                    "form": "GSTR2X",
                    "checked_at": datetime.now().isoformat(timespec="seconds"),
                    "status": st, "due_dt": due, "arn": arn,
                    "sections": sections,
                    "records_total": n_rec,
                    "verdict": entry["verdict"],
                }
                save(cdir / "GSTR2X" / f"2X_evidence_{prd}.json",
                     json.dumps(ev, indent=2))

            log["periods"][prd] = entry
            print(f"  {period_label(prd):<9} {st:<4} {entry['verdict']}")

        # ---- the compliance table.
        # "NF" here does NOT mean overdue: a month with no auto-populated
        # detail cannot be filed at all, and a nil return is not required.
        print("\n  period    GSTR-2X   due date     rec  verdict")
        print("  " + "-" * 78)
        actionable, unknowns = [], []
        for prd, st, due, note in table:
            e = log["periods"].get(prd, {})
            n = sum(v for v in (e.get("counts") or {}).values()
                    if isinstance(v, int))
            vd = e.get("verdict") or (note or "")
            print(f"  {period_label(prd):<9} {st:<9} {due:<12} "
                  f"{(n if n else '-'):<4} {vd[:44]}")
            if e.get("unknown"):
                unknowns.append(period_label(prd))
            elif st == "NF" and n:
                actionable.append((period_label(prd), n))

        # UNKNOWN is reported before, and separately from, the clean verdict.
        # Folding it in either direction is the dangerous move: called nil it
        # stops anyone looking, called unclaimed it sends someone chasing a
        # return that may hold nothing.
        if unknowns:
            print("\n  UNKNOWN - the portal failed on these periods; re-run "
                  "before trusting the line above:")
            print("    " + ", ".join(unknowns))

        if actionable:
            print("\n  UNCLAIMED - credit sits auto-drafted but the return is "
                  "not filed:")
            for lbl, n in actionable:
                print(f"    {lbl}: {n} record(s)")
            print("  Accepting these moves money into the electronic CASH "
                  "ledger, so each is cash, not just a compliance gap.")
        elif not unknowns:
            print("\n  nothing unclaimed - every month carrying auto-drafted "
                  "detail is filed")
            print("  (NF months with no records are NOT defaults: a nil GSTR-2X "
                  "is not required)")

        if not a.status_only:
            print("\n  Every period above has a dated 2X_evidence_<MMYYYY>.json "
                  "in GSTR2X\\,")
            print("  including the nil ones - so 'checked, nothing there' is on "
                  "record and")
            print("  is never confused with 'never looked'.")

        if gstin and not a.status_only:
            logp = root / f"FY {a.fy}" / gstin / "_pull_r2x_log.json"
            logp.parent.mkdir(parents=True, exist_ok=True)
            prev = []
            if logp.exists():
                try:
                    old = json.loads(logp.read_text(encoding="utf-8"))
                    prev = old if isinstance(old, list) else [old]
                except Exception:
                    prev = []
            prev.append(log)
            logp.write_text(json.dumps(prev, indent=2), encoding="utf-8")
            print(f"\n  pull log: {logp}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
