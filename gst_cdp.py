"""
gst_cdp.py - drive an already-running Chrome over CDP, one command per call.

Chrome must be started with remote debugging, on a dedicated profile:

    chrome.exe --remote-debugging-port=9222 --disable-extensions \
               --user-data-dir=%LOCALAPPDATA%\\gst-portal-tools\\gst_chrome

The human logs in once in that window; this script attaches to the SAME session,
so there is no cookie-jar mismatch. Nothing is sent anywhere: it talks only to
127.0.0.1:9222.

Deliberately text/DOM-first rather than screenshot-first - the office DPDP setup
blocks reading image files, and for mapping the portal's API the network log and
page text are better evidence than a picture.

Commands
--------
    url                     current page(s): index, title, url
    goto <url> [--page N]   navigate
    text [--page N] [--max N]
                            visible text of the page
    html [--page N] [--sel CSS] [--max N]
                            outerHTML (optionally of one element)
    net [--page N] [--filter SUBSTR] [--max N]
                            XHR/fetch URLs the page has made (performance API)
    eval <js> [--page N]    evaluate JS, print the JSON result
    click <CSS> [--page N]
    clicktext <TEXT> [--page N]
                            click the first element whose text contains TEXT
    downloads               list files in the Chrome download dir for this profile
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright not importable")

CDP = "http://127.0.0.1:9222"

# Only the `downloads` command uses this. It was
#     Path(os.environ["LOCALAPPDATA"]) / "KSJ" / "gst_chrome"
# which raised KeyError AT IMPORT on any machine without LOCALAPPDATA - i.e. on
# macOS and Linux this file could not be imported at all, let alone run - and
# hardcoded one office's folder name into a tool meant to be handed on.
PROFILE = Path(os.environ.get("GST_BROWSER_PROFILE")
               or (Path(os.environ.get("LOCALAPPDATA")
                        or (Path.home() / ".local" / "share"))
                   / "gst-portal-tools" / "gst_chrome"))

# performance.getEntriesByType('resource') gives us every URL the SPA fetched,
# without needing a live listener attached at the time of the call.
NET_JS = """
() => performance.getEntriesByType('resource')
    .filter(e => ['xmlhttprequest','fetch'].includes(e.initiatorType))
    .map(e => ({ t: Math.round(e.startTime), type: e.initiatorType,
                 size: e.transferSize, name: e.name }))
"""

FETCH_TEXT_JS = """
async (url) => {
  try {
    const r = await fetch(url, { credentials: 'include' });
    return { status: r.status, text: await r.text() };
  } catch (e) {
    return { error: String(e) };
  }
}
"""

CLICKHREF_JS = """
(needle) => {
  const a = [...document.querySelectorAll('a[href]')]
      .find(x => (x.getAttribute('href') || '').includes(needle));
  if (!a) return null;
  const label = (a.innerText || a.getAttribute('title') || '').replace(/\\s+/g, ' ').trim();
  a.click();
  return { label: label.slice(0, 80), href: a.getAttribute('href') };
}
"""

CLICKTEXT_JS = """
(needle) => {
  const els = [...document.querySelectorAll('a,button,input[type=button],input[type=submit],span,td,div')];
  const hit = els.find(e => (e.innerText || e.value || '').trim().toLowerCase().includes(needle.toLowerCase()));
  if (!hit) return null;
  hit.scrollIntoView({block:'center'});
  hit.click();
  return (hit.innerText || hit.value || '').trim().slice(0,120);
}
"""


def pick(pages, n):
    if not pages:
        sys.exit("no pages open in that Chrome")
    if n is None:
        return pages[0]
    if n >= len(pages):
        sys.exit(f"page {n} out of range (0..{len(pages)-1})")
    return pages[n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("arg", nargs="?", default=None)
    ap.add_argument("--page", type=int, default=None)
    ap.add_argument("--sel", default=None)
    ap.add_argument("--filter", default=None)
    ap.add_argument("--max", type=int, default=6000)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--seconds", type=int, default=15)
    ap.add_argument("--out", default=None,
                    help="save target; may contain {gstin} and {prd}")
    ap.add_argument("--prd", default=None,
                    help="fallback period for {prd} when the payload omits it")
    a = ap.parse_args()

    if a.cmd == "downloads":
        d = PROFILE / "Default" / "Downloads"
        alt = Path.home() / "Downloads"
        for p in (d, alt):
            print(f"--- {p} ---")
            if p.exists():
                for f in sorted(p.iterdir(), key=lambda x: x.stat().st_mtime)[-25:]:
                    print(f"  {f.stat().st_size:>12,}  {f.name}")
            else:
                print("  <none>")
        return 0

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(CDP)
        except Exception as e:
            sys.exit(f"cannot attach to {CDP}: {e}")

        ctxs = browser.contexts
        pages = [p for c in ctxs for p in c.pages]

        if a.cmd == "url":
            for i, p in enumerate(pages):
                try:
                    print(f"[{i}] {p.title()[:70]:<70} {p.url}")
                except Exception as e:
                    print(f"[{i}] <unreadable: {e}>")
            return 0

        page = pick(pages, a.page)

        if a.cmd == "goto":
            # HARD LESSON: typing a URL straight at an /auth/ route returns
            # "Access Denied" AND kills the logged-in session behind it - the
            # portal wants you to arrive by clicking its own links. Recovering
            # costs a fresh human login, so refuse it outright. Use `clickhref`.
            if "/auth/" in a.arg and not a.force:
                sys.exit("REFUSED: direct navigation to an /auth/ URL kills the "
                         "session. Use `clickhref <substring>` instead, or pass "
                         "--force if you genuinely mean it.")
            page.goto(a.arg, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            print(f"{page.title()}  |  {page.url}")

        elif a.cmd == "text":
            t = page.inner_text("body")
            print(t[:a.max])
            if len(t) > a.max:
                print(f"\n... [{len(t) - a.max} more chars]")

        elif a.cmd == "html":
            if a.sel:
                el = page.query_selector(a.sel)
                if not el:
                    sys.exit(f"selector not found: {a.sel}")
                h = el.evaluate("e => e.outerHTML")
            else:
                h = page.content()
            print(h[:a.max])
            if len(h) > a.max:
                print(f"\n... [{len(h) - a.max} more chars]")

        elif a.cmd == "net":
            rows = page.evaluate(NET_JS)
            if a.filter:
                rows = [r for r in rows if a.filter.lower() in r["name"].lower()]
            print(f"{len(rows)} xhr/fetch entries")
            for r in rows[-a.max if a.max < len(rows) else 0:]:
                print(f"  {r['t']:>7}ms  {r['size']:>9}  {r['name']}")

        elif a.cmd == "eval":
            out = page.evaluate(a.arg)
            print(json.dumps(out, indent=2, default=str)[:a.max])

        elif a.cmd == "click":
            page.click(a.arg, timeout=15000)
            page.wait_for_timeout(2000)
            print(f"clicked {a.arg}  ->  {page.url}")

        elif a.cmd == "clicktext":
            got = page.evaluate(CLICKTEXT_JS, a.arg)
            page.wait_for_timeout(2000)
            print(f"clicked: {got!r}  ->  {page.url}")

        elif a.cmd == "clickhref":
            # Click a real anchor by href substring. Works for links inside
            # collapsed menus, which clicktext misses because hidden elements
            # report no innerText. This is the ONLY safe way to move between
            # portal auth pages.
            got = page.evaluate(CLICKHREF_JS, a.arg)
            if got is None:
                sys.exit(f"no anchor with href containing: {a.arg}")
            page.wait_for_timeout(3000)
            print(f"clicked: {got!r}  ->  {page.url}")

        elif a.cmd in ("flow", "flowjs"):
            # Click an in-page link and record every gst.gov.in XHR it triggers,
            # with request/response headers. This is how we learn the portal's
            # own API surface and the rotating `at` token, without ever typing
            # a URL at an /auth/ route.
            seen = []

            def on_req(r):
                if "gst.gov.in" in r.url and r.resource_type in ("xhr", "fetch"):
                    h = r.headers
                    seen.append(("REQ", r.method, r.url,
                                 {k: v for k, v in h.items()
                                  if k.lower() in ("at", "referer", "origin",
                                                   "sec-fetch-site", "content-type")}))

            def on_res(r):
                if "gst.gov.in" in r.url and r.request.resource_type in ("xhr", "fetch"):
                    h = r.headers
                    keep = {k: v for k, v in h.items()
                            if k.lower() in ("at", "content-type", "content-disposition")}
                    seen.append(("RES", str(r.status), r.url, keep))

            page.on("request", on_req)
            page.on("response", on_res)

            if a.cmd == "flowjs":
                got = page.evaluate(Path(a.arg).read_text(encoding="utf-8"))
                print(f"action: {json.dumps(got, default=str)[:400]}")
            elif a.arg:
                got = page.evaluate(CLICKHREF_JS, a.arg)
                print(f"clicked: {got!r}")
            page.wait_for_timeout(int(a.seconds) * 1000)

            print(f"\n{len(seen)} xhr events\n")
            for kind, meta, url, hdrs in seen:
                bits = []
                for k, v in hdrs.items():
                    v = str(v)
                    if k.lower() == "at":
                        v = f"<{len(v)} chars> {v[:14]}…"
                    bits.append(f"{k}={v}")
                print(f"  {kind} {meta:<6} {url[:120]}")
                if bits:
                    print(f"        {'  '.join(bits)}")
            print(f"\nnow at: {page.url}")

        elif a.cmd == "save":
            # Fetch a portal endpoint FROM INSIDE the page (same-origin, so no
            # header forging and no `at` token needed) and write the body to
            # disk. The payload itself never passes through stdout - only the
            # byte count and the path - so client invoice data stays out of
            # transcripts and logs.
            #
            # --out may contain {gstin} and {prd}, filled from the payload, so
            # the file lands in the right client folder without the caller
            # having to know or handle the GSTIN.
            body = page.evaluate(FETCH_TEXT_JS, a.arg)
            if body.get("error"):
                sys.exit(f"fetch failed: {body['error']}")
            if body["status"] != 200:
                sys.exit(f"HTTP {body['status']} from {a.arg}")

            txt = body["text"]
            gstin, prd = "unknown", "unknown"
            def dig(obj, keys, depth=0):
                """First value for any of `keys`, searched a few levels deep.
                3B's payload nests gstin under data.bal, R1's has it at data."""
                if depth > 3 or not isinstance(obj, dict):
                    return None
                for k in keys:
                    v = obj.get(k)
                    if isinstance(v, str) and v:
                        return v
                for v in obj.values():
                    if isinstance(v, dict):
                        got = dig(v, keys, depth + 1)
                        if got:
                            return got
                return None

            try:
                j = json.loads(txt)
                d = j.get("data", j) if isinstance(j, dict) else {}
                gstin = dig(d, ("gstin", "ctin")) or "unknown"
                prd = dig(d, ("rtnprd", "ret_period", "rtn_prd")) or a.prd or "unknown"
            except Exception:
                pass

            out = Path(a.out.format(gstin=gstin, prd=prd))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(txt, encoding="utf-8")
            print(f"saved {len(txt):>9,} bytes  ->  {out}")

        elif a.cmd == "evalf":
            # Evaluate JS read from a file - avoids PowerShell mangling braces
            # and quotes in inline JS.
            js = Path(a.arg).read_text(encoding="utf-8")
            out = page.evaluate(js)
            print(json.dumps(out, indent=2, default=str)[:a.max])

        else:
            sys.exit(f"unknown command: {a.cmd}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
