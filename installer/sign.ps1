# Signs the given files with the code-signing certificate in the SIGN_PFX_BASE64 /
# SIGN_PFX_PASSWORD secrets. Without them it does nothing, so unsigned builds
# still work (Windows SmartScreen will then warn on first launch).
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Files)
$ErrorActionPreference = "Stop"

if (-not $env:SIGN_PFX_BASE64) {
    Write-Host "No signing certificate configured - leaving $($Files -join ', ') unsigned."
    exit 0
}
$pfx = Join-Path $env:RUNNER_TEMP "codesign.pfx"
[IO.File]::WriteAllBytes($pfx, [Convert]::FromBase64String($env:SIGN_PFX_BASE64))
try {
    $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" |
        Sort-Object FullName -Descending | Select-Object -First 1
    if (-not $signtool) { throw "signtool.exe not found" }
    foreach ($f in $Files) {
        & $signtool.FullName sign /f $pfx /p $env:SIGN_PFX_PASSWORD /fd sha256 `
            /tr http://timestamp.digicert.com /td sha256 /d "Peripherals Battery" $f
        if ($LASTEXITCODE -ne 0) { throw "signing $f failed" }
    }
} finally {
    Remove-Item $pfx -ErrorAction SilentlyContinue
}
