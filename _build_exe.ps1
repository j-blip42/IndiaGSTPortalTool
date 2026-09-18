# Build gst-portal-tools.exe.
#
#     .\_build_exe.ps1
#
# WHY THE WORK AND OUTPUT PATHS ARE LOCAL, AND NOT BESIDE THIS FILE:
#
# Building straight onto Z: fails at the very last step, after ~60 seconds of
# apparently fine progress:
#
#     win32ctypes.pywin32.pywintypes.error:
#       (30, 'EndUpdateResourceW', 'The system cannot read from the specified device.')
#
# Windows' resource-update API (used to embed the manifest in the .exe) is not
# reliable on a mapped / sync-backed drive, which Z: is. The build itself is
# fine - only the destination is wrong. So PyInstaller works in $env:TEMP and
# the finished .exe is copied here afterwards.
#
# Run the sync first if you have touched the fetchers: the .exe bundles whatever
# .py files are sitting in THIS folder, and DSB is canonical for all three.

# NOT $ErrorActionPreference = "Stop": PyInstaller writes its INFO log to
# stderr, and under Stop PowerShell turns those ordinary progress lines into a
# terminating NativeCommandError - the build "fails" while actually succeeding.
# Native tools are judged on $LASTEXITCODE here, and cmdlets are checked by hand.
$ErrorActionPreference = "Continue"
$here = $PSScriptRoot
$work = Join-Path $env:TEMP "gst-portal-tools-build"
$dist = Join-Path $work "dist"

& (Join-Path $here "_sync_from_dsb.ps1")

Push-Location $here
try {
    python -m PyInstaller --noconfirm --clean `
        --distpath $dist --workpath (Join-Path $work "build") `
        gst_portal_tools.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit $LASTEXITCODE" }
} finally {
    Pop-Location
}

$built = Join-Path $dist "gst-portal-tools.exe"
if (-not (Test-Path $built)) { throw "no exe produced at $built" }
Copy-Item $built (Join-Path $here "gst-portal-tools.exe") -Force

$mb = [math]::Round((Get-Item $built).Length / 1MB, 1)
"built and copied: gst-portal-tools.exe  ($mb MB)"

# A build that omits Playwright's Node driver still produces a working-looking
# .exe and only fails when someone actually runs a pull. So prove the driver is
# in there: with nothing listening on 9222 the RIGHT answer is a connection
# refusal from Playwright, not a missing-driver error.
"verifying the bundled Playwright driver..."
$out = & (Join-Path $here "gst-portal-tools.exe") r2x --fy 2025-26 --status-only 2>&1 | Out-String
if ($out -match "connect_over_cdp|ECONNREFUSED|cannot attach") {
    "  OK - Playwright started and refused a connection (no browser on 9222)."
} elseif ($out -match "GSTR-2X|verdict|NIL|FILED|UNCLAIMED") {
    # A logged-in browser happened to be open, so the pull actually ran. That is
    # a STRONGER result than a refusal, and an earlier version of this check
    # reported it as a failure because it only looked for the refusal.
    "  OK - Playwright ran a real sweep against a live session."
} else {
    Write-Warning "  Playwright did NOT start as expected. Output was:`n$out"
}
