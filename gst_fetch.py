"""
gst_fetch.py - pull a client's filed GST data off the portal into the landing
folder, using an already-logged-in Chrome. No vendor, no account, no cost.

    python gst_fetch.py --fy 2024-25
    python gst_fetch.py --fy 2024-25 --forms R1,3B          (skip 2B)
    python gst_fetch.py --fy 2024-25 --status-only          (compliance sweep)
    python gst_fetch.py --fy 2024-25 --refresh              (re-pull existing)

Prerequisite - Chrome started with remote debugging on the dedicated profile,
and YOU logged in to the portal in it:

    chrome.exe --remote-debugging-port=9222 --disable-extensions \
               --user-data-dir=%LOCALAPPDATA%\\gst-portal-tools\\gst_chrome

Output (year / GSTIN), under --root, or $GST_LANDING_ROOT, or ./GSTN Portal Data:

    <root>\\FY 2024-25\\<GSTIN>\\
        GSTR1\\   R1_summary_<MMYYYY>.json
        GSTR2B\\  2B_<MMYYYY>.json
        GSTR3B\\  3B_taxpayble_<MMYYYY>.json
        _status\\ rolestatus_<MMYYYY>.json
        _pull_log.json

DSB never reads the portal; it reads this folder. That also keeps us the right
side of rule G5 - the fetcher must not touch a .dsb the app may have open.

Three things learned the hard way, all load-bearing:

1. NEVER navigate directly to an /auth/ URL. It returns "Access Denied" AND
   kills the logged-in session behind it. Always click a link that is already
   on the page.
2. Everything is fetched from INSIDE a portal page, so it is same-origin. That
   is why we need neither Octa's `at` token echo nor its Referer/Origin/
   Sec-Fetch-Site rewriting - an extension exists only to fake what we get free.
3. GSTR-2B lives on gstr2b.gst.gov.in, a DIFFERENT origin from return.gst.gov.in.
   Its payload cannot be fetched from a return.* page; we must click through to
   the 2B page once, then fetch every period from there.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright not importable")

CDP = "http://127.0.0.1:9222"

# No office-specific path baked in, so the same file runs here and anywhere it
# is handed on. DSB always passes --root explicitly, so this default only ever
# applies to someone running the script by hand.
DEFAULT_ROOT = Path(os.environ.get("GST_LANDING_ROOT")
                    or (Path.cwd() / "GSTN Portal Data"))

RET = "https://return.gst.gov.in"
R2B = "https://gstr2b.gst.gov.in"

# ---------------------------------------------------------------- in-page JS

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

# Select a financial year on the Returns Dashboard. AngularJS only updates
# ng-model on a dispatched 'change' event.
SET_FY_JS = """
(fy) => {
  const sel = document.querySelector('select[name="fin"]');
  if (!sel) return { error: 'fin select not found' };
  const opt = [...sel.options].find(o => (o.text || '').trim() === fy);
  if (!opt) return { error: 'FY not offered', have: [...sel.options].map(o => o.text.trim()) };
  sel.value = opt.value;
  sel.dispatchEvent(new Event('change', { bubbles: true }));
  return { picked: fy };
}
"""

CLICK_SEARCH_JS = """
() => {
  const b = [...document.querySelectorAll('button')]
      .find(x => (x.innerText || '').trim().toUpperCase() === 'SEARCH');
  if (!b) return { error: 'SEARCH not found' };
  b.click();
  return { clicked: 'SEARCH' };
}
"""

# DOWNLOAD inside the GSTR-2B tile only - pick the smallest panel mentioning 2B
# so we never catch an outer container that also wraps the 2A/3B tiles.
CLICK_2B_JS = """
() => {
  let best = null;
  document.querySelectorAll('div,section').forEach(el => {
    const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
    if (!/GSTR-2B/.test(t)) return;
    if (/GSTR-2A|GSTR-3B|GSTR-1\\b/.test(t)) return;
    const btn = [...el.querySelectorAll('button,a')]
        .find(b => (b.innerText || '').trim().toUpperCase() === 'DOWNLOAD');
    if (!btn) return;
    if (!best || t.length < best.len) best = { btn, len: t.length };
  });
  if (!best) return { error: 'no GSTR-2B tile with DOWNLOAD' };
  best.btn.click();
  return { clicked: 'GSTR-2B DOWNLOAD' };
}
"""

# ------------------------------------------------------------------- helpers


def fy_periods(fy: str) -> list[str]:
    """'2024-25' -> ['042024'..'122024', '012025'..'032025'] (MMYYYY)."""
    try:
        y1 = int(fy.split("-")[0])
    except Exception:
        sys.exit(f"bad --fy {fy!r}, expected like 2024-25")
    out = [f"{m:02d}{y1}" for m in range(4, 13)]
    out += [f"{m:02d}{y1 + 1}" for m in range(1, 4)]
    return out


# Quarter-end months for a QRMP filer. NOT a formatting detail: for userPref
# "Q", GSTR-3B is due ONLY in these months, and GSTR-1 in the other months is
# the OPTIONAL Invoice Furnishing Facility. Treating every "NF" as a default
# reports a compliant quarterly client as eight returns in arrears.
QUARTER_END = {"06", "09", "12", "03"}


def is_due(pref: str, prd: str, form: str) -> bool:
    """Is this form actually DUE for this taxpayer in this period?"""
    mm = prd[:2]
    if pref == "Q":
        return mm in QUARTER_END          # both R1 and 3B are quarterly
    return True                            # monthly filer: every month


def period_label(prd: str) -> str:
    m, y = int(prd[:2]), prd[2:]
    return f"{['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][m]}-{y}"


def get_json(page, url):
    """Fetch a portal endpoint from inside the page. Returns (raw_text, parsed)."""
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


def returns_for(rolestatus: dict) -> dict:
    """Flatten rolestatus into {return_ty: {status, due_dt}}."""
    out = {}
    data = (rolestatus or {}).get("data") or {}
    for grp in data.get("user") or []:
        for r in grp.get("returns") or []:
            ty = r.get("return_ty")
            if ty:
                out[ty] = {"status": r.get("status"), "due_dt": r.get("due_dt")}
    return out


def save(path: Path, txt: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(txt, encoding="utf-8")
    return len(txt)


def usable_file(path: Path) -> bool:
    """Is an already-present file real data, or junk that should be re-fetched?

    "Exists" is not "is good". An earlier version of this script wrote the
    portal's 200-with-error envelope to disk under a data filename, and the
    landing folder sits on a sync drive where a delete has already proved not
    to be reliably final. So skip-if-exists inspects the file rather than
    trusting its presence.
    """
    if not path.exists():
        return False
    try:
        j = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return portal_error(j) is None


def portal_error(parsed):
    """The portal answers HTTP 200 even when it has nothing to give.

    A missing GSTR-2B comes back as {"status_cd":"0","error":{...GTR2B-002...}}
    with a perfectly good 200. Writing that to 2B_<prd>.json would leave a file
    that looks like data and parses like data but holds an error string - so
    every fetch must be checked for the envelope, not just the status code.
    """
    if not isinstance(parsed, dict):
        return "not a JSON object"
    err = parsed.get("error")
    if isinstance(err, dict):
        return f"{err.get('error_cd') or err.get('errorCode') or 'ERR'}: " \
               f"{(err.get('message') or '')[:120]}"
    if str(parsed.get("status_cd", "")) == "0":
        return "status_cd=0 (no record)"
    if "data" not in parsed:
        return f"no data key (got {list(parsed)[:4]})"
    return None


def chksum_of(parsed: dict):
    if not isinstance(parsed, dict):
        return None
    if parsed.get("chksum"):
        return parsed["chksum"]
    d = parsed.get("data")
    if isinstance(d, dict):
        return d.get("chksum")
    return None


# ---------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fy", required=True, help="e.g. 2024-25")
    ap.add_argument("--forms", default="R1,3B,2B",
                    help="comma list of R1,3B,2B (default all)")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--status-only", action="store_true",
                    help="only sweep rolestatus and print the compliance table")
    ap.add_argument("--refresh", action="store_true",
                    help="re-pull periods whose file already exists")
    a = ap.parse_args()

    want = {f.strip().upper() for f in a.forms.split(",") if f.strip()}
    periods = fy_periods(a.fy)
    root = Path(a.root)

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(CDP)
        except Exception as e:
            sys.exit(f"cannot attach to Chrome on {CDP}: {e}\n"
                     f"Start it with --remote-debugging-port=9222 and log in.")

        pages = [p for c in browser.contexts for p in c.pages]
        page = next((p for p in pages if "gst.gov.in" in (p.url or "")), None)
        if page is None:
            sys.exit("no gst.gov.in tab open - log in to the portal first")

        # --- get onto a return.gst.gov.in auth page, by CLICKING, never by URL
        if "return.gst.gov.in" not in page.url:
            href = page.evaluate(CLICKHREF_JS, "returns/auth/dashboard")
            if not href:
                sys.exit("no 'Returns Dashboard' link on the current page - "
                         "go to the post-login welcome page and retry")
            page.wait_for_timeout(6000)
        if "accessdenied" in page.url or "/login" in page.url:
            sys.exit("session is not live - log in again")

        gstin = None
        log = {"fy": a.fy, "pulled_at": datetime.now().isoformat(timespec="seconds"),
               "root": str(root), "periods": {}}
        table = []

        # ---------------- phase 1: rolestatus + R1 + 3B (return.* origin)
        for prd in periods:
            raw, rs = get_json(page, f"{RET}/returns/auth/api/rolestatus?rtn_prd={prd}")
            if rs.get("_error"):
                table.append((prd, "-", "-", rs["_error"]))
                continue

            rets = returns_for(rs)
            pref = ((rs.get("data") or {}).get("userPref") or "?")
            r1 = rets.get("GSTR1", {})
            r3b = rets.get("GSTR3B", {})

            # A period with no GSTR1/GSTR3B tile is not a filing period for this
            # taxpayer (quarterly filers have none in the middle months).
            if not r1 and not r3b:
                continue

            table.append((prd, r1.get("status", "-"), r3b.get("status", "-"), pref))
            # For a QRMP filer the quarter-end month carries the QUARTERLY
            # GSTR-1; months 1-2 carry IFF. Invoices furnished via IFF are not
            # repeated in the quarterly return, so the two are complementary -
            # label them rather than let a consumer guess and double-count.
            r1_kind = ("QUARTERLY" if pref == "Q" and prd[:2] in QUARTER_END
                       else "IFF" if pref == "Q" else "MONTHLY")

            # GSTR2B = monthly statement, GSTR2BQ = quarterly. From Oct-2024
            # QRMP filers stopped getting monthly 2Bs (IMS rollout), so a file
            # named 2B_122024 may actually cover Oct+Nov+Dec. The figures are
            # right either way because ITC keys on rtnprd - the period the
            # credit BECAME AVAILABLE - but the span must not be guessed from
            # the filename.
            b2b_kind = ("QUARTERLY" if "GSTR2BQ" in rets and "GSTR2B" not in rets
                        else "MONTHLY" if "GSTR2B" in rets else None)

            entry = {"userPref": pref, "r1_kind": r1_kind, "gstr2b_kind": b2b_kind,
                     "GSTR1": r1.get("status"), "GSTR1_due": r1.get("due_dt"),
                     "GSTR3B": r3b.get("status"), "GSTR3B_due": r3b.get("due_dt"),
                     "files": {}}

            if gstin is None:
                # cheapest reliable source of the GSTIN for the folder name
                _, ud = get_json(page, f"{RET}/returns/auth/api/gstr1/summary?rtn_prd={prd}")
                gstin = ((ud.get("data") or {}).get("gstin")) if isinstance(ud, dict) else None

            if a.status_only:
                log["periods"][prd] = entry
                continue

            cdir = root / f"FY {a.fy}" / (gstin or "UNKNOWN_GSTIN")

            # rolestatus is itself the compliance evidence - keep it
            if raw:
                save(cdir / "_status" / f"rolestatus_{prd}.json", raw)

            # GSTR-1 summary - only meaningful once filed
            if "R1" in want and r1.get("status") == "FIL":
                suffix = "_IFF" if r1_kind == "IFF" else ""
                out = cdir / "GSTR1" / f"R1_summary_{prd}{suffix}.json"
                if usable_file(out) and not a.refresh:
                    entry["files"]["GSTR1"] = "skipped (exists)"
                else:
                    txt, j = get_json(page, f"{RET}/returns/auth/api/gstr1/summary?rtn_prd={prd}")
                    bad = j.get("_error") or portal_error(j)
                    if txt and not bad:
                        entry["files"]["GSTR1"] = {"bytes": save(out, txt),
                                                   "chksum": chksum_of(j)}
                    else:
                        entry["files"]["GSTR1"] = f"unavailable - {bad}"

            # GSTR-3B liability / offset - the set-off table
            if "3B" in want and r3b.get("status") == "FIL":
                out = cdir / "GSTR3B" / f"3B_taxpayble_{prd}.json"
                if usable_file(out) and not a.refresh:
                    entry["files"]["GSTR3B"] = "skipped (exists)"
                else:
                    txt, j = get_json(page, f"{RET}/returns/auth/api/gstr3b/taxpayble?rtn_prd={prd}")
                    bad = j.get("_error") or portal_error(j)
                    if txt and not bad:
                        entry["files"]["GSTR3B"] = {"bytes": save(out, txt)}
                    else:
                        entry["files"]["GSTR3B"] = f"unavailable - {bad}"

            log["periods"][prd] = entry
            print(f"  {period_label(prd)}  R1={r1.get('status','-'):<3} "
                  f"3B={r3b.get('status','-'):<3}  {entry['files'] or ''}")

        # ---------------- phase 2: GSTR-2B (different origin, needs its page)
        if "2B" in want and not a.status_only and log["periods"]:
            print("\n  navigating to the GSTR-2B page (separate origin)...")
            ok = goto_2b_page(page, a.fy)
            if ok:
                cdir = root / f"FY {a.fy}" / (gstin or "UNKNOWN_GSTIN")
                for prd in log["periods"]:
                    out = cdir / "GSTR2B" / f"2B_{prd}.json"
                    if usable_file(out) and not a.refresh:
                        log["periods"][prd]["files"]["GSTR2B"] = "skipped (exists)"
                        continue
                    txt, j = get_json(
                        page, f"{R2B}/gstr2b/auth/api/gstr2b/getjson?rtnprd={prd}")
                    bad = j.get("_error") or portal_error(j)
                    if txt and not bad:
                        n = save(out, txt)
                        log["periods"][prd]["files"]["GSTR2B"] = {
                            "bytes": n, "chksum": chksum_of(j)}
                        print(f"  2B {period_label(prd)}  {n:,} bytes")
                    else:
                        # The period has no 2B. If a junk file from an earlier
                        # run is sitting there under a data name, remove it -
                        # leaving it would keep offering a file that parses but
                        # holds an error string.
                        if out.exists() and not usable_file(out):
                            try:
                                out.unlink()
                                bad += " (removed stale file)"
                            except Exception:
                                pass
                        log["periods"][prd]["files"]["GSTR2B"] = f"unavailable - {bad}"
                        print(f"  2B {period_label(prd)}  unavailable - {bad}")
            else:
                print("  could not reach the 2B page - skipped (R1/3B unaffected)")

        # ---------------- compliance table + log
        print("\n  period    GSTR-1    GSTR-3B   due?")
        print("  " + "-" * 44)
        unfiled = []
        for prd, s1, s3, pref in table:
            due = is_due(pref, prd, "any")
            mark = "DUE" if due else "opt/IFF"
            print(f"  {period_label(prd):<9} {s1:<9} {s3:<9} {mark}")
            if not due:
                continue
            missing = [n for n, s in (("GSTR-1", s1), ("GSTR-3B", s3)) if s == "NF"]
            if missing:
                unfiled.append((period_label(prd), missing))

        if unfiled:
            print("\n  UNFILED (returns that were actually due):")
            for lbl, forms in unfiled:
                print(f"    {lbl}: {', '.join(forms)}")
        else:
            print("\n  nothing unfiled - every return that was DUE is filed")
            print("  (blank months above are a quarterly filer's non-filing months,")
            print("   where GSTR-1 is the optional IFF - not a default)")

        if gstin and not a.status_only:
            logp = root / f"FY {a.fy}" / gstin / "_pull_log.json"
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


def goto_2b_page(page, fy: str) -> bool:
    """Click through dashboard -> FY -> SEARCH -> GSTR-2B tile -> DOWNLOAD.

    Direct navigation is not an option (it kills the session), so the 2B page
    can only be reached the way a human reaches it.
    """
    if "return.gst.gov.in/returns/auth/dashboard" not in page.url:
        if not page.evaluate(CLICKHREF_JS, "returns/auth/dashboard"):
            return False
        page.wait_for_timeout(6000)

    # the FY dropdown is populated asynchronously; retry rather than assume
    for _ in range(6):
        got = page.evaluate(SET_FY_JS, fy)
        if not got.get("error"):
            break
        page.wait_for_timeout(2000)
    else:
        return False

    page.wait_for_timeout(2000)
    if page.evaluate(CLICK_SEARCH_JS).get("error"):
        return False
    page.wait_for_timeout(6000)

    if page.evaluate(CLICK_2B_JS).get("error"):
        return False
    page.wait_for_timeout(8000)
    return "gstr2b.gst.gov.in" in page.url


if __name__ == "__main__":
    raise SystemExit(main())
