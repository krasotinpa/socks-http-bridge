# Hosted bootstrapper for the socks-http-bridge (issue #49, spec #26).
#
# One-liner (install and update converge):
#   powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://raw.githubusercontent.com/krasotinpa/socks-http-bridge/main/scripts/install.ps1 | iex"
#
# Thin by design: resolve channel/target, bootstrap uv if missing, fetch
# the source tarball, then delegate to scripts/install.py --source-tarball
# (the issue #48 core owns stop/swap/sync/marker/shortcut/launch/verify).
# BRIDGE_* env overrides exist because the irm | iex form cannot pass
# parameters. No git, no gh, no embedded tokens.
# -DryRun changes no install state; it may download/extract to TEMP
# (removed afterwards) to pass through to the install script's plan.

[CmdletBinding()]
param(
    # No ValidateSet: iex re-binds the caller's empty $Channel and the
    # attribute rejects it; the manual check below covers both forms.
    [string]$Channel,
    [switch]$DryRun,
    [string]$SourceTarball,
    [string]$Version,
    [string]$Sha
)

$ErrorActionPreference = 'Stop'
$Repo = 'krasotinpa/socks-http-bridge'
$RetryHint = 'Re-run the one-liner to retry.'
$staging = $null
$ownTarball = $false
$Tarball = $null

function Fail([string]$Message) {
    Write-Host "Install failed: $Message"
    Write-Host $RetryHint
    exit 1
}

function Find-Uv {
    if ($env:UV_BIN) { return $env:UV_BIN }
    if ($env:UV) { return $env:UV }
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $astral = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path -LiteralPath $astral) { return $astral }
    # install-manager layout: numeric version precedence, not lexical.
    $base = Join-Path $env:LOCALAPPDATA 'Python'
    $managed = Get-ChildItem -LiteralPath $base -Directory -Filter 'pythoncore-*' -ErrorAction SilentlyContinue |
        ForEach-Object {
            $p = Join-Path $_.FullName 'Scripts\uv.exe'
            if (Test-Path -LiteralPath $p) {
                [pscustomobject]@{
                    Path = $p
                    Ver  = [version]($_.Name -replace '^pythoncore-', '' -replace '-64$', '')
                }
            }
        } | Sort-Object Ver -Descending | Select-Object -First 1
    if ($managed) { return $managed.Path }
    return $null
}

try {
    if ($env:OS -ne 'Windows_NT') { Fail 'the Bridge is Windows-only.' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    # The Bridge cannot be its own bootstrap proxy (#59): the client hint
    # points HTTP(S)_PROXY at the Bridge itself, which is stopped during
    # install/update. Strip proxy/venv vars for this process so downloads,
    # uv's Python provisioning and the delegated sync never dial it.
    # Env: is case-insensitive on Windows (covers http_proxy et al).
    foreach ($name in 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'VIRTUAL_ENV') {
        Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
    }

    if (-not $Channel -and $env:BRIDGE_CHANNEL) { $Channel = $env:BRIDGE_CHANNEL }
    if (-not $SourceTarball -and $env:BRIDGE_SOURCE_TARBALL) { $SourceTarball = $env:BRIDGE_SOURCE_TARBALL }
    $IsDryRun = [bool]($DryRun.IsPresent -or ($env:BRIDGE_DRYRUN -and $env:BRIDGE_DRYRUN -ne '0'))
    if ($Channel -and $Channel -notin @('stable', 'main')) {
        Fail "unknown channel '$Channel' (stable|main)."
    }

    $AppDir = if ($env:BRIDGE_INSTALL_DIR) { $env:BRIDGE_INSTALL_DIR }
              else { Join-Path $env:LOCALAPPDATA 'socks-http-bridge\app' }
    # Channel precedence: explicit flag/env > persisted marker (#48) >
    # stable default. A bare re-run never resets a main install to stable.
    $MarkerChannel = $null
    $markerPath = Join-Path $AppDir 'install-marker.json'
    if (Test-Path -LiteralPath $markerPath) {
        try {
            $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
            if ($marker.channel -in @('stable', 'main')) { $MarkerChannel = $marker.channel }
        } catch { }  # corrupt marker: fall back to stable
    }
    $effChannel = if ($Channel) { $Channel }
                  elseif ($MarkerChannel) { $MarkerChannel }
                  else { 'stable' }

    $headers = @{ 'User-Agent' = 'socks-http-bridge-installer' }
    if ($SourceTarball) {
        # CI/offline override: bypass the download, same delegation path.
        # 'local' target keeps the marker honest for overridden installs.
        if (-not $IsDryRun) {
            if (-not (Test-Path -LiteralPath $SourceTarball)) {
                Fail "source tarball not found: $SourceTarball"
            }
            $SourceTarball = (Resolve-Path -LiteralPath $SourceTarball).Path
        }
        $target = if ($effChannel -eq 'stable') { if ($Version) { $Version } else { 'local' } }
                  else { if ($Sha) { $Sha } else { 'local' } }
        $Tarball = $SourceTarball
    } elseif ($effChannel -eq 'stable') {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers $headers
        $target = $release.tag_name
        if (-not $target) { Fail 'latest release has no tag_name.' }
        $url = "https://codeload.github.com/$Repo/tar.gz/refs/tags/$target"
        $ownTarball = $true
    } else {
        $commit = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/commits/main" -Headers $headers
        $target = $commit.sha
        if (-not $target) { Fail 'cannot resolve main HEAD sha.' }
        $url = "https://codeload.github.com/$Repo/tar.gz/$target"
        $ownTarball = $true
    }
    Write-Host "Resolving target: channel $effChannel, target $target"

    if ($IsDryRun -and $SourceTarball -and -not (Test-Path -LiteralPath $SourceTarball)) {
        # Nothing to delegate from: plan at bootstrapper level, mutate nothing.
        Write-Host 'dry-run: planning only, no changes will be made'
        Write-Host "  channel $effChannel, target $target"
        Write-Host "  app dir: $AppDir"
        Write-Host "  source tarball not found: $SourceTarball"
        Write-Host '  would extract the given tarball and delegate to scripts/install.py'
        exit 0
    }

    $uv = Find-Uv
    if (-not $uv) {
        if ($IsDryRun) {
            # No uv yet: plan at bootstrapper level, mutate nothing.
            Write-Host 'dry-run: planning only, no changes will be made'
            Write-Host "  channel $effChannel, target $target"
            Write-Host "  app dir: $AppDir"
            Write-Host '  would bootstrap uv via the astral installer'
            Write-Host '  would fetch the source tarball and delegate to scripts/install.py'
            exit 0
        }
        Write-Host 'Bootstrapping uv (astral installer)...'
        Invoke-Expression (Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1' -Headers $headers)
        $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
        if (-not (Test-Path -LiteralPath $uv)) {
            Fail 'uv bootstrap produced no uv.exe (antivirus may have removed it).'
        }
    }
    $env:UV_BIN = $uv

    if ($ownTarball) {
        $Tarball = Join-Path $env:TEMP "bridge-$([guid]::NewGuid().ToString('N')).tar.gz"
        Write-Host "Downloading $url"
        Invoke-WebRequest -Uri $url -OutFile $Tarball -UseBasicParsing -Headers $headers
    }

    # Extract just to locate install.py; the core re-extracts into its own
    # staging dir for the swap. Temp dirs are removed in finally.
    $staging = Join-Path $env:TEMP "bridge-boot-$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $staging | Out-Null
    # Full path: a PATH tar.exe may be GNU tar (MSYS), which reads C:\ as
    # a remote host; System32\tar.exe is the bsdtar shipped with Windows.
    $tar = Join-Path $env:SystemRoot 'System32\tar.exe'
    if (-not (Test-Path -LiteralPath $tar)) { Fail 'Windows tar.exe (System32) not found.' }
    & $tar -xzf $Tarball -C $staging
    if ($LASTEXITCODE -ne 0) { Fail "cannot extract $Tarball (tar.exe exit $LASTEXITCODE)." }
    $src = Get-ChildItem -LiteralPath $staging -Directory | Select-Object -First 1
    if (-not $src) { Fail 'unexpected tarball layout (want one top-level dir).' }
    $installPy = Join-Path $src.FullName 'scripts\install.py'
    if (-not (Test-Path -LiteralPath $installPy)) { Fail 'tarball has no scripts/install.py.' }

    $env:PYTHONPATH = Join-Path $src.FullName 'src'
    $pyArgs = @('run', '--no-project', 'python', $installPy,
                '--source-tarball', $Tarball, '--app-dir', $AppDir,
                '--channel', $effChannel)
    if ($effChannel -eq 'stable') { $pyArgs += @('--version', $target) }
    else { $pyArgs += @('--sha', $target) }
    if ($IsDryRun) { $pyArgs += '--dry-run' }
    & $uv @pyArgs
    $code = $LASTEXITCODE
    if ($code -ne 0) { Write-Host $RetryHint }
    if ($code -eq 0 -and -not $IsDryRun) {
        $listener = '127.0.0.1:8118'
        $cfgPath = Join-Path $env:LOCALAPPDATA 'socks-http-bridge\config.json'
        if (Test-Path -LiteralPath $cfgPath) {
            try {
                $cfg = Get-Content -LiteralPath $cfgPath -Raw | ConvertFrom-Json
                if ($cfg.listeners) { $listener = @($cfg.listeners)[0] }
            } catch { }  # unreadable config: fall back to the default
        }
        # Mirror config.connect_target: wildcard binds are not connectable.
        $listener = $listener -replace '^0\.0\.0\.0:', '127.0.0.1:'
        $listener = $listener -replace '^\[?::\]?:', '[::1]:'
        Write-Host ''
        Write-Host "Bridge ready: channel $effChannel, target $target."
        Write-Host 'Configure HTTP-only clients (do not set ALL_PROXY):'
        Write-Host "  HTTP_PROXY=http://$listener"
        Write-Host "  HTTPS_PROXY=http://$listener"
        Write-Host 'Exit IP Test: tray menu -> Exit IP Test shows the VPN exit IP.'
    }
    exit $code
}
catch {
    Fail $_.Exception.Message
}
finally {
    if ($staging -and (Test-Path -LiteralPath $staging)) {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($ownTarball -and $Tarball -and (Test-Path -LiteralPath $Tarball)) {
        Remove-Item -LiteralPath $Tarball -Force -ErrorAction SilentlyContinue
    }
}
