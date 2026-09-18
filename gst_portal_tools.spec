# PyInstaller spec for gst-portal-tools.exe
#
#     pyinstaller --noconfirm gst_portal_tools.spec
#
# Two things here are load-bearing, and a plain `pyinstaller --onefile` gets both
# wrong:
#
# 1. collect_all('playwright') pulls in the Node DRIVER directory (~107 MB) as
#    data. Without it the build succeeds and then dies at run time with a missing
#    -driver error - the failure the README warns about.
# 2. The three tools are imported by NAME at run time, so PyInstaller's static
#    analysis cannot see them from the launcher. They are declared as hidden
#    imports; drop one and that sub-command fails only when someone runs it.

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all("playwright")

hiddenimports += ["gst_fetch", "gst_fetch_r2x", "gst_cdp"]

a = Analysis(
    ["gst_portal_tools.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas + [("LICENSE", "."), ("NOTICE", "."), ("README.md", ".")],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Nothing here draws a window; tkinter would only add weight.
    excludes=["tkinter", "matplotlib", "PIL", "pandas", "numpy"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="gst-portal-tools",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
