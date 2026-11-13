[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\TRACE",
    [switch]$NoPath
)

$ErrorActionPreference = 'Stop'
$archive = Join-Path $PSScriptRoot 'TRACE-2.0-Windows-x64.zip'
$checksumFile = Join-Path $PSScriptRoot 'TRACE-2.0-Windows-x64.sha256'
$expected = (Get-Content -LiteralPath $checksumFile -Raw).Trim()
if ($expected -notmatch '^[a-f0-9]{64}$' -or
    (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) {
    throw 'The TRACE archive checksum did not match.'
}

$stage = Join-Path ([IO.Path]::GetTempPath()) ('trace-installer-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $stage)
    $installer = Join-Path $stage 'Install-TRACE.ps1'
    $arguments = @{ InstallRoot = $InstallRoot }
    if ($NoPath) { $arguments.NoPath = $true }
    & $installer @arguments
} finally {
    $absolute = [IO.Path]::GetFullPath($stage)
    $parent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    $item = Get-Item -LiteralPath $absolute -Force -ErrorAction SilentlyContinue
    if ((Split-Path -Parent $absolute) -eq $parent -and
        (Split-Path -Leaf $absolute) -match '^trace-installer-[a-f0-9]{32}$' -and
        $item -and $item.PSIsContainer -and -not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        Remove-Item -LiteralPath $absolute -Recurse -Force
    }
}
