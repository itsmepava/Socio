param(
    [int]$Port = 5001
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

Write-Host 'Socio local setup' -ForegroundColor Cyan
Write-Host "Project folder: $ProjectRoot"

$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    $PythonLauncher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $PythonLauncher) {
        throw 'Python 3.12 was not found. Install Python 3.12, then run this script again.'
    }

    Write-Host 'Step 1/4: Creating the local Python environment...'
    & $PythonLauncher.Source -3.12 -m venv (Join-Path $ProjectRoot '.venv')
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not create the local Python environment. Confirm Python 3.12 is installed.'
    }
}

Write-Host 'Step 2/4: Checking and installing app packages...'
& $Python -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) {
    throw 'Package installation failed. Check your internet connection and try again.'
}

Write-Host 'Step 3/4: Preparing local settings and database...'
$env:SECRET_KEY = & $Python -c "import secrets; print(secrets.token_urlsafe(48))"
if ($LASTEXITCODE -ne 0 -or -not $env:SECRET_KEY) {
    throw 'Could not create the local app secret.'
}
$env:DATABASE_URL = 'sqlite:///membership.db'
$env:PUBLIC_BASE_URL = "http://127.0.0.1:$Port"
$env:DEV_EMAIL_PREVIEW = '1'
$env:SHOW_PLATFORM_ADMIN_LINK = '1'

& $Python -m flask --app 'app:create_app' init-db
if ($LASTEXITCODE -ne 0) {
    throw 'Database setup failed. The app was not started.'
}

$SeedDemo = Read-Host 'Add the sample Colombo Chess Club, plans, offers, and a local organization-admin account? Type Y to seed sample data, or press Enter to skip'
if ($SeedDemo -match '^(y|yes)$') {
    & $Python -m flask --app 'app:create_app' seed-demo
    if ($LASTEXITCODE -ne 0) {
        throw 'Sample data setup failed. The app was not started.'
    }
}

$CreateAdmin = Read-Host 'Is this a fresh database with no platform administrator? Type Y to create one now, or press Enter to skip'
if ($CreateAdmin -match '^(y|yes)$') {
    & $Python -m flask --app 'app:create_app' create-admin
    if ($LASTEXITCODE -ne 0) {
        throw 'Platform administrator setup failed. The app was not started.'
    }
}

Write-Host 'Step 4/4: Starting Socio...' -ForegroundColor Cyan
Write-Host "Open http://127.0.0.1:$Port in your browser. Keep this window open."
Write-Host 'Local confirmation links will appear here. Press Ctrl+C to stop Socio.'
& $Python -m flask --app 'app:create_app' run --host 127.0.0.1 --port $Port
if ($LASTEXITCODE -ne 0) {
    throw "Socio stopped with an error on port $Port. If that port is busy, try: .\start-local.ps1 -Port 5002"
}
