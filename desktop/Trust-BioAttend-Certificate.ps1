<#
  Makes this PC trust the BioAttend code-signing certificate (BioAttend-CodeSigning.cer in the same folder).

  Run once per PC, as administrator:
      Right-click PowerShell -> Run as administrator, then:
      powershell -ExecutionPolicy Bypass -File .\Trust-BioAttend-Certificate.ps1
  Remove the trust again:
      powershell -ExecutionPolicy Bypass -File .\Trust-BioAttend-Certificate.ps1 -Remove

  For many PCs use Group Policy instead: Computer Configuration -> Windows Settings -> Security Settings ->
  Public Key Policies -> "Trusted Root Certification Authorities" AND "Trusted Publishers" -> Import the .cer file.
#>
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Please run this script as administrator." }

$certFile = Join-Path $PSScriptRoot "BioAttend-CodeSigning.cer"
if (-not (Test-Path $certFile)) { throw "BioAttend-CodeSigning.cer not found next to this script." }
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 $certFile
if ($cert.Subject -ne "CN=BioAttend Code Signing") { throw "Unexpected certificate: $($cert.Subject)" }

foreach ($store in "Root", "TrustedPublisher") {
    $path = "Cert:\LocalMachine\$store\$($cert.Thumbprint)"
    if ($Remove) {
        if (Test-Path $path) { Remove-Item $path; "Removed from $store" }
    } elseif (-not (Test-Path $path)) {
        Import-Certificate -FilePath $certFile -CertStoreLocation "Cert:\LocalMachine\$store" | Out-Null
        "Added to $store"
    } else {
        "Already trusted in $store"
    }
}
if (-not $Remove) {
    "This PC now trusts software signed by BioAttend Code Signing (thumbprint $($cert.Thumbprint))."
}
