# Refresh this distribution folder from the canonical copies in the DSB folder.
#
# The DSB folder is canonical: the app loads gst_fetch*.py from beside itself, so
# a fix made only here would never reach the app. This exists so the two cannot
# drift silently - run it before every push, and it will tell you if they have.
#
# Nothing here is office-specific: the scripts take --root, and default to
# $env:GST_LANDING_ROOT or .\GSTN Portal Data.

$src = "Z:\My Folders\Self Experimentation\Accounting\DSB"
$dst = $PSScriptRoot
$files = @("gst_fetch.py", "gst_fetch_r2x.py", "gst_cdp.py")

$changed = @()
foreach ($f in $files) {
    $s = Join-Path $src $f
    $d = Join-Path $dst $f
    if (-not (Test-Path $s)) { Write-Warning "missing in DSB: $f"; continue }
    $hs = (Get-FileHash $s -Algorithm SHA256).Hash
    $hd = if (Test-Path $d) { (Get-FileHash $d -Algorithm SHA256).Hash } else { "" }
    if ($hs -ne $hd) {
        Copy-Item $s $d -Force
        $changed += $f
    }
}

if ($changed.Count) { "updated: " + ($changed -join ", ") }
else { "already in sync" }

# Guard: this folder is meant to be the git root. The parent (Tax Tools) holds
# clients.json and filings_db.json - real client data that must never be pushed.
if (Test-Path (Join-Path (Split-Path $dst -Parent) ".git")) {
    Write-Warning ("A .git exists in the PARENT folder (Tax Tools), which holds " +
                   "clients.json and filings_db.json. Publishing from there would " +
                   "expose client data. Initialise git inside gst-portal-tools instead.")
}
