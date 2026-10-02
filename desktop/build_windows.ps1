<#
  Builds BioAttend for Windows (all signed):
      dist\BioAttend-<version>.msi              MSI for IT deployment (per machine, Program Files)
      dist\BioAttend-Setup-<version>.exe        installer for one user (no admin rights)
      dist\BioAttend-portable-<version>.zip     no installation
      dist\BioAttend-CodeSigning.cer + Trust-BioAttend-Certificate.ps1   (self-signed certificate only)

  Run on Windows from the project folder (PowerShell):
      powershell -ExecutionPolicy Bypass -File desktop\build_windows.ps1
  Optional:  -Python C:\Path\to\python.exe   (Python 3.12 or 3.13, 64-bit, officially signed)
  No Python installed?  The official portable Python is downloaded automatically (nothing is installed).

  Code signing (Authenticode, SHA-256, timestamped):
      default                      a self-signed "BioAttend Code Signing" certificate (created once, kept in your
                                   Windows certificate store); office PCs trust it via Trust-BioAttend-Certificate.ps1
      -CertThumbprint <thumb>      a purchased certificate already in Cert:\CurrentUser\My
      -PfxPath file.pfx -PfxPassword ...   a purchased certificate file
      -NoSign                      do not sign

  Everything is installed into .buildvenv inside the project folder; nothing is installed system-wide.
#>
param(
    [string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    [string]$Version = "1.0.0",
    [string]$CertThumbprint = "",
    [string]$PfxPath = "",
    [string]$PfxPassword = "",
    [string]$TimestampServer = "http://timestamp.digicert.com",
    [switch]$NoSign
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Log = Join-Path $Root "dist\build.log"
New-Item -ItemType Directory -Force -Path (Join-Path $Root "dist"), (Join-Path $Root "desktop\vendor") | Out-Null
"BioAttend build $Version  $(Get-Date)" | Out-File $Log -Encoding utf8

function Step($text) { "`n=== $text" | Tee-Object -FilePath $Log -Append | Write-Host }
function Run([string]$exe, [string[]]$arguments) {
    $ErrorActionPreference = "Continue"   # tools print warnings on stderr; judge by the exit code
    & $exe @arguments 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $Log -Append | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed ($LASTEXITCODE): $exe $arguments  - see $Log" }
}
function Download($url, $file) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    # A non-browser user agent makes SourceForge send the file instead of its download web page
    Invoke-WebRequest -Uri $url -OutFile $file -UseBasicParsing -UserAgent "Wget/1.21"
}

$SelfSignedSubject = "CN=BioAttend Code Signing"
function Get-SigningCertificate {
    if ($NoSign) { return $null }
    if ($PfxPath) {
        $pw = ConvertTo-SecureString $PfxPassword -AsPlainText -Force
        return (Get-PfxData -FilePath $PfxPath -Password $pw).EndEntityCertificates[0]
    }
    if ($CertThumbprint) {
        $c = Get-Item "Cert:\CurrentUser\My\$CertThumbprint" -ErrorAction SilentlyContinue
        if (-not $c) { throw "Certificate $CertThumbprint not found in Cert:\CurrentUser\My" }
        return $c
    }
    $c = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object { $_.Subject -eq $SelfSignedSubject -and $_.NotAfter -gt (Get-Date).AddDays(30) } |
         Sort-Object NotAfter -Descending | Select-Object -First 1
    if (-not $c) {
        $c = New-SelfSignedCertificate -Type CodeSigningCert -Subject $SelfSignedSubject -KeyAlgorithm RSA -KeyLength 3072 `
             -HashAlgorithm SHA256 -CertStoreLocation Cert:\CurrentUser\My -NotAfter (Get-Date).AddYears(5) -KeyExportPolicy Exportable
        "  created self-signed certificate $($c.Thumbprint) (valid until $($c.NotAfter.ToString('yyyy-MM-dd')))" | Tee-Object -FilePath $Log -Append | Write-Host
    }
    return $c
}
function Sign-File($file) {
    if (-not $script:Cert) { return }
    $sig = $null
    try {
        $sig = Set-AuthenticodeSignature -FilePath $file -Certificate $script:Cert -HashAlgorithm SHA256 -TimestampServer $TimestampServer -ErrorAction Stop
    } catch {
        "  timestamp server not reachable - signing $file without timestamp" | Tee-Object -FilePath $Log -Append | Write-Host
        $sig = Set-AuthenticodeSignature -FilePath $file -Certificate $script:Cert -HashAlgorithm SHA256
    }
    if (-not $sig.SignerCertificate) { throw "Signing failed for $file : $($sig.StatusMessage)" }
    "  signed $(Split-Path $file -Leaf)" | Tee-Object -FilePath $Log -Append | Out-Null
}
function Get-NuGetPackage($id, $ver, $dest) {
    # Downloads a NuGet package and checks it against the SHA-512 published by nuget.org
    if (Test-Path $dest) { return }
    $pkg = "$dest.nupkg"
    Download "https://api.nuget.org/v3-flatcontainer/$id/$ver/$id.$ver.nupkg" $pkg
    $reg = Invoke-RestMethod "https://api.nuget.org/v3/registration5-semver1/$id/$ver.json"
    $entry = $reg.catalogEntry
    if ($entry -is [string]) { $entry = Invoke-RestMethod $entry }
    $actual = [Convert]::ToBase64String([Security.Cryptography.SHA512]::Create().ComputeHash([IO.File]::ReadAllBytes((Resolve-Path $pkg))))
    if ($actual -ne $entry.packageHash) { throw "Checksum mismatch for NuGet package $id $ver" }
    Expand-Archive $pkg -DestinationPath $dest -Force
    Remove-Item $pkg
}

Step "Python build environment (.buildvenv)"
# Only an officially signed Python is used (signed by the Python Software Foundation). Without one, the official
# portable Python package (NuGet) is downloaded into desktop\vendor - nothing is installed on this PC.
function Test-SignedPython($exe) {
    if (-not $exe -or -not (Test-Path $exe)) { return $false }
    $sig = Get-AuthenticodeSignature $exe
    return ($sig.Status -eq "Valid" -and $sig.SignerCertificate.Subject -match "Python Software Foundation")
}
if (-not (Test-SignedPython $Python)) {
    "  $Python is missing or not officially signed - using the portable official Python instead" | Tee-Object -FilePath $Log -Append | Write-Host
    $pv = "3.13.16"
    $Python = Join-Path $Root "desktop\vendor\python-$pv\tools\python.exe"
    if (-not (Test-Path $Python)) {
        $pkg = "desktop\vendor\python-$pv.zip"
        Download "https://api.nuget.org/v3-flatcontainer/python/$pv/python.$pv.nupkg" $pkg
        Expand-Archive $pkg -DestinationPath "desktop\vendor\python-$pv" -Force
        Remove-Item $pkg
    }
    foreach ($f in "python.exe", "pythonw.exe", "python313.dll") {
        if (-not (Test-SignedPython (Join-Path (Split-Path $Python) $f))) { throw "Portable Python file $f is not validly signed" }
    }
}
if (-not (Test-Path ".buildvenv\Scripts\python.exe")) { Run $Python @("-m", "venv", ".buildvenv") }
$Py = Join-Path $Root ".buildvenv\Scripts\python.exe"
Run $Py @("-m", "pip", "install", "--upgrade", "pip", "--quiet")
Run $Py @("-m", "pip", "install", "--quiet", "-r", "requirements.txt", "-r", "desktop\requirements-desktop.txt")

Step "Icon"
Run $Py @("desktop\make_icon.py")

Step "rclone (checksum verified)"
$RcloneExe = "desktop\vendor\rclone.exe"
if (-not (Test-Path $RcloneExe)) {
    $rv = "1.75.1"
    $zip = "desktop\vendor\rclone.zip"
    Download "https://downloads.rclone.org/v$rv/rclone-v$rv-windows-amd64.zip" $zip
    Download "https://downloads.rclone.org/v$rv/SHA256SUMS" "desktop\vendor\rclone-SHA256SUMS.txt"
    $line = Get-Content "desktop\vendor\rclone-SHA256SUMS.txt" | Where-Object { $_ -match "windows-amd64\.zip" } | Select-Object -First 1
    if (-not $line) { throw "rclone checksum list could not be read" }
    $expected = ($line -split "\s+")[0].ToLower()
    Remove-Item "desktop\vendor\rclone-SHA256SUMS.txt"
    $actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($expected -ne $actual) { throw "rclone checksum mismatch" }
    Expand-Archive $zip -DestinationPath "desktop\vendor\rclone-tmp" -Force
    Copy-Item (Get-ChildItem "desktop\vendor\rclone-tmp" -Recurse -Filter rclone.exe | Select-Object -First 1).FullName $RcloneExe
    Remove-Item $zip, "desktop\vendor\rclone-tmp" -Recurse -Force
}

Step "Static files"
$env:SECRET_KEY = "build-only-not-secret-0123456789abcdefghijklmnop"
$env:VAR_DIR = Join-Path $env:TEMP "bioattend-build-var"
$env:LOG_TO_FILE = "false"
Run $Py @("manage.py", "collectstatic", "--noinput", "--clear", "-v", "0")

Step "PyInstaller"
Run $Py @("-m", "PyInstaller", "desktop\bioattend.spec", "--noconfirm", "--distpath", "dist", "--workpath", "build\pyinstaller")
Remove-Item Env:SECRET_KEY, Env:VAR_DIR, Env:LOG_TO_FILE

Step "Code signing"
$script:Cert = Get-SigningCertificate
if ($script:Cert) {
    "  certificate: $($script:Cert.Subject)  thumbprint $($script:Cert.Thumbprint)" | Tee-Object -FilePath $Log -Append | Write-Host
    Sign-File "dist\BioAttend\BioAttend.exe"
    Sign-File "dist\BioAttend\bioattend-cli.exe"
} else { "  signing skipped (-NoSign)" | Tee-Object -FilePath $Log -Append | Write-Host }

Step "Smoke test of the packaged app"
$env:BIOATTEND_HOME = Join-Path $env:TEMP "bioattend-smoke-$(Get-Random)"
Run "dist\BioAttend\bioattend-cli.exe" @("check")
Run "dist\BioAttend\bioattend-cli.exe" @("migrate", "--noinput", "-v", "0")
Remove-Item $env:BIOATTEND_HOME -Recurse -Force
Remove-Item Env:BIOATTEND_HOME

Step "Installer (NSIS)"
$MakeNsis = Get-ChildItem "desktop\vendor\nsis" -Recurse -Filter makensis.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $MakeNsis) {
    $nz = "desktop\vendor\nsis.zip"
    Download "https://downloads.sourceforge.net/project/nsis/NSIS%203/3.11/nsis-3.11.zip" $nz
    if ((Get-FileHash $nz -Algorithm MD5).Hash.ToLower() -ne "b7c063bee3afc8127dca0fd64c4e22ce") { throw "NSIS checksum mismatch" }
    Expand-Archive $nz -DestinationPath "desktop\vendor\nsis" -Force
    Remove-Item $nz
    $MakeNsis = Get-ChildItem "desktop\vendor\nsis" -Recurse -Filter makensis.exe | Select-Object -First 1
}
Run $MakeNsis.FullName @("/V2", "/DVERSION=$Version", "desktop\installer.nsi")
Sign-File "dist\BioAttend-Setup-$Version.exe"

Step "MSI (WiX Toolset 5, checksum verified)"
Get-NuGetPackage "wix" "5.0.2" "desktop\vendor\wix"
$WixExe = Join-Path $Root "desktop\vendor\wix\tools\net6.0\any\wix.exe"
$Msi = "dist\BioAttend-$Version.msi"
Run $WixExe @("build", "desktop\msi\BioAttend.wxs", "-arch", "x64", "-d", "Version=$Version",
              "-d", "SourceDir=$Root\dist\BioAttend", "-d", "IconFile=$Root\desktop\bioattend.ico",
              "-o", $Msi, "-nologo")
Remove-Item "dist\BioAttend-$Version.wixpdb" -ErrorAction SilentlyContinue
Sign-File $Msi

if ($script:Cert -and $script:Cert.Subject -eq $SelfSignedSubject) {
    Step "Certificate for office PCs"
    Export-Certificate -Cert $script:Cert -FilePath "dist\BioAttend-CodeSigning.cer" -Type CERT | Out-Null
    Copy-Item "desktop\Trust-BioAttend-Certificate.ps1" "dist\" -Force
    "  dist\BioAttend-CodeSigning.cer and dist\Trust-BioAttend-Certificate.ps1" | Tee-Object -FilePath $Log -Append | Write-Host
}

Step "Portable ZIP"
$Portable = "dist\BioAttend-portable-$Version.zip"
if (Test-Path $Portable) { Remove-Item $Portable }
Compress-Archive -Path "dist\BioAttend" -DestinationPath $Portable -CompressionLevel Optimal

Step "Done"
Get-ChildItem "dist\BioAttend-$Version.msi", "dist\BioAttend-Setup-$Version.exe", $Portable | ForEach-Object {
    "{0}  {1:N1} MB" -f $_.Name, ($_.Length / 1MB) } | Tee-Object -FilePath $Log -Append | Write-Host
