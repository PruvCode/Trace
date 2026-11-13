[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\TRACE",
    [switch]$NoPath
)

$ErrorActionPreference = 'Stop'
foreach ($module in 'Microsoft.PowerShell.Management', 'Microsoft.PowerShell.Utility') {
    Import-Module "$PSHOME\Modules\$module\$module.psd1"
}
$architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
if ($architecture -ne 'AMD64') {
    throw 'This TRACE preview requires 64-bit Windows on an Intel or AMD processor.'
}

$payload = Join-Path $PSScriptRoot 'trace-payload.zip'
$expected = '@PAYLOAD_SHA256@'
if ((Get-FileHash -LiteralPath $payload -Algorithm SHA256).Hash -ne $expected) {
    throw 'The TRACE package checksum did not match. Download a fresh copy.'
}

$root = [IO.Path]::GetFullPath($InstallRoot)
$destination = Join-Path $root ('preview-' + $expected.Substring(0, 12).ToLowerInvariant())
$receipt = Join-Path $destination 'bundle.sha256'
[IO.Directory]::CreateDirectory($root) | Out-Null
if ((Get-Item -LiteralPath $root -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'Choose a regular directory for the TRACE installation.'
}

if (Test-Path -LiteralPath $destination) {
    $existing = Get-Item -LiteralPath $destination -Force
    if (-not $existing.PSIsContainer -or
        ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        -not (Test-Path -LiteralPath $receipt -PathType Leaf) -or
        (Get-Content -LiteralPath $receipt -Raw).Trim() -ne $expected) {
        throw "The installation path already contains other files: $destination"
    }
} else {
    $stage = Join-Path $root ('.stage-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($stage) | Out-Null
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($payload, $stage)

        if (-not (Get-Command git.exe -CommandType Application -ErrorAction SilentlyContinue)) {
            Write-Host 'Installing private Git for project history...'
            $gitArchive = Join-Path $stage 'MinGit.zip'
            $gitUrl = 'https://github.com/git-for-windows/git/releases/download/v2.56.1.windows.1/MinGit-2.56.1-64-bit.zip'
            $gitHash = '4a92850b6e917da4fd010669b614b55dccf5b0ec333a534cc4be4700dfefdafd'
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 180 -Uri $gitUrl -OutFile $gitArchive
            if ((Get-FileHash -LiteralPath $gitArchive -Algorithm SHA256).Hash -ne $gitHash) {
                throw 'The Git download checksum did not match.'
            }
            [IO.Compression.ZipFile]::ExtractToDirectory($gitArchive, (Join-Path $stage 'git'))
            Remove-Item -LiteralPath $gitArchive
            & (Join-Path $stage 'git\cmd\git.exe') --version
            if ($LASTEXITCODE -ne 0) { throw 'The private Git check failed.' }
        }

        & (Join-Path $stage 'bin\trace.cmd') --version
        if ($LASTEXITCODE -ne 0) { throw 'The installed TRACE check failed.' }
        & (Join-Path $stage 'bin\trace-opencode.cmd') --version
        if ($LASTEXITCODE -ne 0) { throw 'The installed OpenCode check failed.' }

        [IO.File]::WriteAllText((Join-Path $stage 'bundle.sha256'), $expected)
        [IO.Directory]::Move($stage, $destination)
    } finally {
        if (Test-Path -LiteralPath $stage) {
            $leftover = Get-Item -LiteralPath $stage -Force
            if ($leftover.Parent.FullName -eq $root.TrimEnd('\') -and
                $leftover.Name -match '^\.stage-[0-9a-f]{32}$' -and
                -not ($leftover.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                Remove-Item -LiteralPath $leftover.FullName -Recurse -Force
            }
        }
    }
}

$bin = Join-Path $destination 'bin'
if (-not $NoPath) {
    $environment = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    try {
        $current = $environment.GetValue('Path', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $kind = if ($null -eq $current) { 'ExpandString' } else { $environment.GetValueKind('Path') }
        if ($kind -notin ('String', 'ExpandString')) { throw 'The current user PATH has an unexpected format.' }
        $entries = @($current -split ';' | Where-Object { $_ -and $_ -ne $bin })
        $environment.SetValue('Path', ((@($bin) + $entries) -join ';'), $kind)
    } finally {
        $environment.Dispose()
    }

    if (-not ('TraceInstall.EnvironmentNotice' -as [type])) {
        Add-Type -Namespace TraceInstall -Name EnvironmentNotice -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
public static extern IntPtr SendMessageTimeout(IntPtr window, uint message, UIntPtr wParam,
    string setting, uint flags, uint timeout, out UIntPtr result);
'@
    }
    $notification = [UIntPtr]::Zero
    [TraceInstall.EnvironmentNotice]::SendMessageTimeout(
        [IntPtr]0xffff, 0x001a, [UIntPtr]::Zero, 'Environment', 2, 500, [ref]$notification
    ) | Out-Null
}

Write-Host "TRACE installed in $destination"
Write-Host 'Open a new PowerShell window and run trace --version.'
Write-Host 'Run trace-opencode to open the bundled OpenCode.'
