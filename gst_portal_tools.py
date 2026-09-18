"""gst-portal-tools - one executable, four commands.

Packaging-only. The app never loads this: DSB runs gst_fetch*.py directly from
beside itself, so nothing here is on the path that matters to a client book.

ONE executable rather than four because PyInstaller bundles Playwright's Node
driver (~107 MB on disk, ~37 MB packed) into whatever it builds. Four exes would
be four copies of the same driver for no gain.

Each sub-command hands straight over to that tool's own main(), with sys.argv
rewritten so its argparse errors and --help read as the sub-command the user
actually typed. The tools keep their documented flags exactly:

    gst-portal-tools browser                     open the debug browser, log in there
    gst-portal-tools fetch --fy 2025-26
    gst-portal-tools r2x   --fy 2025-26 --status-only
    gst-portal-tools cdp   url

Imports are lazy, one module per run: importing all three fetchers would drag
Playwright in for a command that may not need it.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys

COMMANDS = {
    "browser": (None, "open the debug browser on its own profile, to log in"),
    "fetch": ("gst_fetch", "GSTR-1 / 3B / 2B for a financial year"),
    "r2x": ("gst_fetch_r2x", "GSTR-2X - TDS/TCS credit received (GST TDS)"),
    "cdp": ("gst_cdp", "drive the logged-in browser (mapping / diagnostics)"),
}

# Edge before Chrome, and each on its OWN profile: the fetchers attach over CDP
# and name no executable, so this is purely a launch choice. A dedicated profile
# keeps the debug browser out of the everyday one, where a stray window has
# stalled a pull before.
BROWSERS = [
    (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "gst_edge"),
    (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "gst_edge"),
    (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "gst_chrome"),
    (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", "gst_chrome"),
]

LOGIN_URL = "https://services.gst.gov.in/services/login"


def _local_app_dir() -> str:
    return os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), ".local", "share")


# Profile folders created by earlier in-house builds, adopted only if one is
# already on this machine. Purely backward compatibility: on any other machine
# these simply do not exist. Safe to delete this line - the only consequence is
# that a machine holding an old profile logs in once more.
LEGACY_PROFILE_PARENTS = ("KSJ",)


def _profile_dir(browser: str) -> str:
    r"""Where the dedicated debug browser keeps its profile.

    NOT an office-specific folder: this ships to other people, and a tool that
    silently creates a stranger's machine a folder named after someone else's
    firm is both puzzling and presumptuous. Order of preference:

      1. $GST_BROWSER_PROFILE, if the user set it
      2. an existing profile from an older in-house build, so the machine this
         was written on keeps the login it has rather than quietly starting a
         second one (see LEGACY_PROFILE_PARENTS)
      3. %LOCALAPPDATA%\gst-portal-tools\<browser>  (or ~/.local/share on POSIX)
    """
    env = os.environ.get("GST_BROWSER_PROFILE")
    if env:
        return env
    for parent in LEGACY_PROFILE_PARENTS:
        legacy = os.path.join(_local_app_dir(), parent, browser)
        if os.path.isdir(legacy):
            return legacy
    return os.path.join(_local_app_dir(), "gst-portal-tools", browser)

USAGE = """gst-portal-tools - pull your own GST portal data, locally.

  gst-portal-tools <command> [options]

Commands:
%s
FIRST TIME, IN ORDER:

  1.  gst-portal-tools browser        opens Edge on its own profile
  2.  log in to the GST portal in that window, and STOP at the welcome page
  3.  gst-portal-tools r2x --fy 2025-26 --status-only

These tools do NOT launch a browser for you on a fetch and never handle your
password: they ATTACH to the browser you logged in to, over 127.0.0.1:9222, and
share that exact session. Nothing is sent anywhere except gst.gov.in.

Run a command with --help for its own options.""" % "".join(
    "  %-9s %s\n" % (k, v[1]) for k, v in COMMANDS.items())


def _owns_the_console() -> bool:
    """True when nothing but us is attached to this console.

    That is what a double-click from Explorer looks like: the window was created
    for us and dies with us, so anything printed is never read.

    THE COUNT DEPENDS ON WHETHER WE ARE FROZEN, and getting this wrong is why an
    earlier build still flashed shut. A PyInstaller one-file exe runs as TWO
    processes - the bootloader that unpacks, and the real app it starts - and
    both are attached to the console. Unfrozen there is only the one python.
    Launched from a shell, that shell is on the console too and the count is one
    higher again, so nothing pauses and normal command-line use is unaffected.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        buf = (ctypes.c_uint * 16)()
        n = ctypes.windll.kernel32.GetConsoleProcessList(buf, 16)
        return n <= (2 if getattr(sys, "frozen", False) else 1)
    except Exception:
        return False


def _pause_if_double_clicked() -> None:
    if _owns_the_console():
        try:
            input("\nPress Enter to close this window...")
        except Exception:
            pass


def _port_is_up(port: int = 9222) -> bool:
    import urllib.request
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=2)
        return True
    except Exception:
        return False


def _extensions_loaded(port: int = 9222):
    """Extension targets in the debug browser. There should be none.

    Not hygiene - a hazard. Edge's first-run import copies the DEFAULT browser's
    extensions into a brand-new --user-data-dir (--no-first-run suppresses the
    first-run SCREEN, not the import), and a fresh Chrome profile still receives
    registry-installed ones. A profile created this way came up carrying seven,
    three with <all_urls> and one holding `cookies` - enough to read the portal
    session and every invoice row fetched. The flags below are mandatory, and
    this re-checks that they worked rather than trusting them.
    """
    import urllib.request
    try:
        raw = urllib.request.urlopen(
            "http://127.0.0.1:%d/json/list" % port, timeout=3).read()
        return [t.get("url", "") for t in json.loads(raw)
                if "extension://" in (t.get("url") or "")]
    except Exception:
        return []


def open_browser(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="gst-portal-tools browser",
                                 description="Open the debug browser, to log in.")
    # argparse %-formats help strings, so a literal % must be doubled.
    ap.add_argument("--profile", default=None,
                    help="browser profile directory (default: $GST_BROWSER_PROFILE, "
                         "else %%LOCALAPPDATA%%\\gst-portal-tools\\<browser>)")
    ap.add_argument("--port", type=int, default=9222)
    a = ap.parse_args(argv)

    if _port_is_up(a.port):
        print("The debug browser is already open on port %d." % a.port)
        print("Log in to the portal there, then run a fetch.")
        return 0

    exe = profile_name = None
    for path, name in BROWSERS:
        if os.path.exists(path):
            exe, profile_name = path, name
            break
    if not exe:
        print("Neither Edge nor Chrome was found at the usual paths:\n  "
              + "\n  ".join(p for p, _ in BROWSERS), file=sys.stderr)
        return 1

    profile = a.profile or _profile_dir(profile_name)
    os.makedirs(profile, exist_ok=True)

    subprocess.Popen([
        exe,
        "--remote-debugging-port=%d" % a.port,
        "--user-data-dir=%s" % profile,
        # see _extensions_loaded - these four are load-bearing, not tidiness
        "--disable-extensions",
        "--disable-component-extensions-with-background-pages",
        "--disable-sync",
        "--disable-features=EdgeImportOnFirstRun,ImportBrowsingDataOnFirstRun",
        "--window-size=1366,730", "--window-position=0,0",
        "--no-first-run", "--no-default-browser-check",
        LOGIN_URL])

    print("%s opened on its own profile:\n  %s\n"
          % (os.path.basename(exe), profile))
    print("Log in to the GST portal in that window, then run a fetch, e.g.")
    print("  gst-portal-tools r2x --fy 2025-26 --status-only\n")
    print("STOP at the welcome page - do NOT type a portal URL. Typing an")
    print("/auth/ address returns Access Denied AND kills the session behind it,")
    print("costing a fresh login.")

    # Give it a moment to come up, then check the flags actually took.
    import time
    for _ in range(20):
        time.sleep(0.5)
        if _port_is_up(a.port):
            break
    ext = _extensions_loaded(a.port)
    if ext:
        print("\nWARNING: %d extension(s) are running in this browser:" % len(ext),
              file=sys.stderr)
        for u in ext[:8]:
            print("  " + u, file=sys.stderr)
        print("Any of them can read the portal session and every row fetched.\n"
              "Close it, remove the profile directory above, and reopen.",
              file=sys.stderr)
        return 1
    return 0


def _default_fy() -> str:
    """The FY that is current today, Indian convention (April start)."""
    import datetime
    t = datetime.date.today()
    y = t.year if t.month >= 4 else t.year - 1
    return "%d-%02d" % (y, (y + 1) % 100)


MENU = """
================================================================
  gst-portal-tools
  Pull your own GST portal data. Nothing is sent anywhere.
================================================================

  1   Open the GST browser   (do this FIRST, and log in there)
  2   Check what is unfiled  - GST TDS / GSTR-2X, writes nothing
  3   Download GST TDS       - GSTR-2X for a whole year
  4   Download GSTR-1/3B/2B  - for a whole year

  0   Quit

"""


class _Quit(Exception):
    """The user pressed 0, Ctrl-C, or input ran out."""


# A BOM arrives on the first line whenever input is piped or redirected rather
# than typed, which is how this gets tested and how a .cmd wrapper would drive
# it. Strip it, or every answer misses by one invisible character.
def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lstrip("﻿").strip()
    except (EOFError, KeyboardInterrupt):
        raise _Quit


def _exe_dir() -> str:
    """The folder the program lives in - not the working directory.

    Double-clicked, the working directory is whatever Explorer felt like, so
    `Path.cwd()` is not somewhere a person can find their files again.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.getcwd()


def _default_root() -> str:
    return os.environ.get("GST_LANDING_ROOT") or os.path.join(
        _exe_dir(), "GSTN Portal Data")


def _writable(d: str) -> bool:
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write_test")
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
        return True
    except Exception:
        return False


def _ask_root() -> str:
    r"""Where the downloaded JSON goes, asked rather than assumed.

    The default is beside the program. That is fine from a Downloads folder and
    NOT fine from C:\Program Files, which is read-only for normal users - so the
    directory is tested before anything is fetched, rather than after twelve
    periods of work. Documents is the fallback because it always exists and is
    always writable.
    """
    default = _default_root()
    if not _writable(default):
        fallback = os.path.join(os.path.expanduser("~"), "Documents",
                                "GSTN Portal Data")
        print("\n  Cannot write to:\n    %s" % default)
        print("  Using this instead:\n    %s" % fallback)
        default = fallback

    print("\n  Files will be saved under:\n    %s" % default)
    ans = _ask("  Enter to accept, or type another folder: ")
    root = ans or default
    if not _writable(root):
        print("\n  Cannot write to that folder either. Staying with:\n    %s" % default)
        return default
    return root


def _ask_fy() -> str:
    while True:
        fy = _ask("\n  Financial year [%s]: " % _default_fy()) or _default_fy()
        if len(fy) == 7 and fy[4] == "-" and fy[:4].isdigit() and fy[5:].isdigit():
            return fy
        print("  Expected the form 2025-26.")


def _login_step() -> bool:
    """Open the browser and WAIT while the user logs in. -> ready to fetch?

    The whole point of a workflow: opening the browser is step one of three, so
    finishing there and closing the window - which is what this did at first -
    strands the user exactly where the tool stops being obvious.
    """
    if _port_is_up():
        print("\n  The GST browser is already open on port 9222.")
        if _ask("  Have you logged in to the portal there? [Y/n]: ").lower() \
                not in ("n", "no"):
            return True
        print("\n  Log in there first, then come back.")
        return False

    if main(["browser"]) != 0:
        return False

    print("  ---------------------------------------------------------------")
    print("  NOW: log in to the GST portal in the window that just opened.")
    print("  Stop at the welcome page. Do NOT type a portal address.")
    print("  ---------------------------------------------------------------")
    _ask("\n  Press Enter HERE once you are logged in (or 0 to go back): ")

    if not _port_is_up():
        print("\n  That browser is no longer running on port 9222. If you closed")
        print("  it, choose 1 again.")
        return False
    return True


def _interactive() -> int:
    """What a double-click gets.

    Someone who double-clicks an .exe is not about to type sub-commands at it,
    and a wall of usage text they cannot read before the window shuts is worse
    than useless. So: the same commands, asked as questions, in the order they
    have to happen - and the window stays open until they say to close it. Each
    action returns to this menu rather than ending the session, so logging in
    and then pulling is one sitting and not two 5-second cold starts.
    """
    try:
        while True:
            print(MENU)
            pick = _ask("  Choose (0-4): ")
            if pick in ("0", "q", "quit", "exit", ""):
                return 0
            if pick not in ("1", "2", "3", "4"):
                print("\n  '%s' is not on the menu." % pick)
                continue

            if pick == "1":
                if _login_step():
                    print("\n  Logged in. You can pull now.")
                    if _ask("  Check what is unfiled, for GST TDS? [Y/n]: ").lower() \
                            not in ("n", "no"):
                        print()
                        main(["r2x", "--fy", _ask_fy(), "--status-only"])
                _ask("\n  Press Enter to return to the menu...")
                continue

            # 2, 3 and 4 all need a logged-in browser. Say so BEFORE asking for a
            # year and then failing with a connection error nobody can act on.
            if not _port_is_up():
                print("\n  The GST browser is not running, so there is nothing to")
                print("  attach to. Choose 1 first.")
                _ask("\n  Press Enter to return to the menu...")
                continue

            fy = _ask_fy()
            if pick == "2":
                # --status-only writes nothing, so there is nothing to ask about.
                args = ["r2x", "--fy", fy, "--status-only"]
            else:
                root = _ask_root()
                args = {"3": ["r2x", "--fy", fy, "--root", root],
                        "4": ["fetch", "--fy", fy, "--root", root]}[pick]
            print()
            rc = main(args)
            print("\n  (finished%s)" % ("" if rc == 0 else ", with errors above"))
            _ask("\n  Press Enter to return to the menu...")
    except _Quit:
        return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv and _owns_the_console():
        # Double-clicked: ask, rather than printing usage at a window that is
        # about to close anyway.
        return _interactive()
    if not argv or argv[0] in ("-h", "--help", "help", "/?"):
        print(USAGE)
        return 0

    cmd = argv.pop(0)
    if cmd not in COMMANDS:
        print("unknown command: %s\n" % cmd, file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2

    if cmd == "browser":
        return open_browser(argv)

    mod_name = COMMANDS[cmd][0]
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:                       # noqa: BLE001 - reported, not hidden
        print("could not load %s: %s" % (mod_name, e), file=sys.stderr)
        return 1

    # argparse reads sys.argv[0] for its usage line, so make it say what was typed.
    sys.argv = ["gst-portal-tools %s" % cmd] + argv
    return mod.main() or 0


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit as e:                      # argparse --help, and sys.exit()
        # Python's own semantics, reproduced deliberately: sys.exit("message")
        # PRINTS that message to stderr and exits 1. The tools report every
        # failure that way - `sys.exit("cannot attach on ...")` - so collapsing a
        # non-int code to 0 would swallow the message AND call the failure a
        # success. It did exactly that until this was caught.
        code = e.code
        if code is None:
            code = 0
        elif not isinstance(code, int):
            print(code, file=sys.stderr)
            code = 1
    _pause_if_double_clicked()
    raise SystemExit(code)
