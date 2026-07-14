param(
    [string]$RuntimeRoot = (Join-Path (Split-Path $PSScriptRoot -Parent) '.runtime'),
    [string]$TextModel = 'qwen3:4b',
    [string]$VisionModel = 'qwen3-vl:4b',
    [string]$OllamaVersion = 'v0.30.8',
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
if (-not $RuntimeRoot.StartsWith('E:\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "RuntimeRoot must be on E: to avoid consuming C: space: $RuntimeRoot"
}

$Downloads = Join-Path $RuntimeRoot 'downloads'
$TesseractRoot = Join-Path $RuntimeRoot 'tesseract'
$OllamaRoot = Join-Path $RuntimeRoot 'ollama'
$ModelRoot = Join-Path $RuntimeRoot 'ollama-models'
New-Item -ItemType Directory -Force -Path $Downloads, $TesseractRoot, $OllamaRoot, $ModelRoot | Out-Null

function Get-VerifiedDownload {
    param(
        [Parameter(Mandatory=$true)][string]$Uri,
        [Parameter(Mandatory=$true)][string]$Destination,
        [string]$ExpectedSha256 = ''
    )
    if (Test-Path -LiteralPath $Destination) {
        if (-not $ExpectedSha256 -or (Get-FileHash -Algorithm SHA256 -LiteralPath $Destination).Hash -eq $ExpectedSha256) {
            return
        }
        Remove-Item -LiteralPath $Destination -Force
    }
    & curl.exe -L --fail --retry 3 --retry-delay 2 $Uri -o $Destination
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $Uri" }
    if ((Get-Item -LiteralPath $Destination).Length -eq 0) {
        throw "Download produced an empty file: $Uri"
    }
    if ($ExpectedSha256) {
        $Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Destination).Hash
        if ($Actual -ne $ExpectedSha256) {
            throw "SHA256 mismatch for $Destination (expected $ExpectedSha256, got $Actual)"
        }
    }
}

$TesseractExe = Join-Path $TesseractRoot 'tesseract.exe'
if (-not (Test-Path -LiteralPath $TesseractExe)) {
    throw "Portable Tesseract is not preseeded at $TesseractExe. The upstream Windows installer ignores alternate destinations and installs on C:, so this E:-only bootstrap refuses to execute it. Copy an approved portable Tesseract 5 build into $TesseractRoot, then rerun."
}

$TessdataRoot = Join-Path $TesseractRoot 'tessdata'
New-Item -ItemType Directory -Force -Path $TessdataRoot | Out-Null
foreach ($Language in @('eng', 'kor')) {
    Get-VerifiedDownload `
        -Uri "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0/$Language.traineddata" `
        -Destination (Join-Path $TessdataRoot "$Language.traineddata")
}

# Resolve the pinned Ollama release through GitHub's official release API so
# the asset digest published by GitHub can be verified before extraction.
$Release = Invoke-RestMethod -Headers @{ 'User-Agent' = 'loss-adjustment-harness' } `
    -Uri "https://api.github.com/repos/ollama/ollama/releases/tags/$OllamaVersion"
$Asset = $Release.assets | Where-Object { $_.name -eq 'ollama-windows-amd64.zip' } | Select-Object -First 1
if (-not $Asset) { throw "Ollama release $OllamaVersion has no ollama-windows-amd64.zip asset" }
$OllamaZip = Join-Path $Downloads "ollama-windows-amd64-$OllamaVersion.zip"
$Digest = if ($Asset.digest -and $Asset.digest.StartsWith('sha256:')) { $Asset.digest.Substring(7).ToUpperInvariant() } else { '' }
if ($Digest -and [long]$Asset.size -ge 67108864) {
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'download_verified_ranges.ps1') `
        -Uri $Asset.browser_download_url -Sha256 $Digest -Size ([long]$Asset.size) -Destination $OllamaZip
    if ($LASTEXITCODE -ne 0) { throw "Range download failed for Ollama $OllamaVersion" }
} else {
    Get-VerifiedDownload -Uri $Asset.browser_download_url -Destination $OllamaZip -ExpectedSha256 $Digest
}
if (-not (Test-Path -LiteralPath (Join-Path $OllamaRoot 'ollama.exe'))) {
    Expand-Archive -LiteralPath $OllamaZip -DestinationPath $OllamaRoot -Force
}
$OllamaExe = Join-Path $OllamaRoot 'ollama.exe'
if (-not (Test-Path -LiteralPath $OllamaExe)) { throw "ollama.exe missing after extraction" }

$env:HARNESS_LOCAL_OCR_COMMAND = $TesseractExe
$env:HARNESS_LOCAL_LLM_COMMAND = $OllamaExe
$env:HARNESS_LOCAL_VLM_COMMAND = $OllamaExe
$env:TESSDATA_PREFIX = $TessdataRoot
$env:OLLAMA_MODELS = $ModelRoot
$env:OLLAMA_HOST = 'http://127.0.0.1:11434'
$env:HARNESS_LOCAL_LLM_MODEL = $TextModel
$env:HARNESS_LOCAL_VLM_MODEL = $VisionModel

function Install-OllamaRegistryModel {
    param([Parameter(Mandatory=$true)][string]$ModelName)

    $NameAndTag = $ModelName.Split(':', 2)
    $Name = $NameAndTag[0]
    $Tag = if ($NameAndTag.Count -eq 2) { $NameAndTag[1] } else { 'latest' }
    $PathParts = $Name.Split('/', [System.StringSplitOptions]::RemoveEmptyEntries)
    if ($PathParts.Count -eq 1) {
        $Namespace = 'library'
        $Repository = $PathParts[0]
    } elseif ($PathParts.Count -eq 2) {
        $Namespace = $PathParts[0]
        $Repository = $PathParts[1]
    } else {
        throw "Unsupported Ollama model name: $ModelName"
    }

    $RegistryBase = "https://registry.ollama.ai/v2/$Namespace/$Repository"
    $ManifestDirectory = Join-Path $ModelRoot "manifests\registry.ollama.ai\$Namespace\$Repository"
    $ManifestDestination = Join-Path $ManifestDirectory $Tag
    $ManifestDownload = Join-Path $Downloads ("ollama-manifest-{0}-{1}.json" -f $Repository, $Tag)
    Get-VerifiedDownload -Uri "$RegistryBase/manifests/$Tag" -Destination $ManifestDownload
    $Manifest = Get-Content -Raw -LiteralPath $ManifestDownload | ConvertFrom-Json
    $Descriptors = @($Manifest.config) + @($Manifest.layers)

    foreach ($Descriptor in ($Descriptors | Sort-Object digest -Unique)) {
        if (-not $Descriptor.digest.StartsWith('sha256:')) {
            throw "Unsupported model blob digest: $($Descriptor.digest)"
        }
        $Hash = $Descriptor.digest.Substring(7).ToLowerInvariant()
        $BlobDestination = Join-Path $ModelRoot "blobs\sha256-$Hash"
        $BlobUri = "$RegistryBase/blobs/$($Descriptor.digest)"
        if ([long]$Descriptor.size -ge 67108864) {
            & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'download_verified_ranges.ps1') `
                -Uri $BlobUri -Sha256 $Hash -Size ([long]$Descriptor.size) -Destination $BlobDestination
            if ($LASTEXITCODE -ne 0) { throw "Range download failed for $($Descriptor.digest)" }
        } else {
            Get-VerifiedDownload -Uri $BlobUri -Destination $BlobDestination -ExpectedSha256 $Hash.ToUpperInvariant()
        }
    }

    New-Item -ItemType Directory -Force -Path $ManifestDirectory | Out-Null
    Copy-Item -Force -LiteralPath $ManifestDownload -Destination $ManifestDestination
}

# Install registry blobs directly into the E:-scoped model store. This avoids
# Ollama's downloader creating large partial files in an unintended location.
Install-OllamaRegistryModel -ModelName $TextModel
if ($VisionModel -ne $TextModel) { Install-OllamaRegistryModel -ModelName $VisionModel }

$ServerReady = $false
try {
    & $OllamaExe list *> $null
    $ServerReady = ($LASTEXITCODE -eq 0)
} catch { $ServerReady = $false }
if (-not $ServerReady) {
    Start-Process -FilePath $OllamaExe -ArgumentList @('serve') -WindowStyle Hidden | Out-Null
    for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
        Start-Sleep -Seconds 1
        try {
            & $OllamaExe list *> $null
            if ($LASTEXITCODE -eq 0) { $ServerReady = $true; break }
        } catch { }
    }
}
if (-not $ServerReady) { throw 'Ollama loopback server did not become ready' }

# Providers never call pull, so inference cannot silently fall back to network
# access. Confirm that the preloaded registry manifest is visible to Ollama.
foreach ($PreloadedModel in @($TextModel, $VisionModel) | Sort-Object -Unique) {
    & $OllamaExe show $PreloadedModel *> $null
    if ($LASTEXITCODE -ne 0) { throw "Ollama did not recognize preloaded model $PreloadedModel" }
}

$EnvironmentFile = Join-Path $RuntimeRoot 'local-runtime.env.ps1'
@"
`$env:HARNESS_LOCAL_OCR_COMMAND = '$TesseractExe'
`$env:HARNESS_LOCAL_LLM_COMMAND = '$OllamaExe'
`$env:HARNESS_LOCAL_VLM_COMMAND = '$OllamaExe'
`$env:TESSDATA_PREFIX = '$TessdataRoot'
`$env:OLLAMA_MODELS = '$ModelRoot'
`$env:OLLAMA_HOST = 'http://127.0.0.1:11434'
`$env:HARNESS_LOCAL_LLM_MODEL = '$TextModel'
`$env:HARNESS_LOCAL_VLM_MODEL = '$VisionModel'
"@ | Set-Content -LiteralPath $EnvironmentFile -Encoding UTF8

if (-not $Python) {
    $BundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $BundledPython) { $Python = $BundledPython }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { $Python = (Get-Command python).Source }
    else { throw 'Python executable not found; pass -Python with an absolute path' }
}

& $Python (Join-Path $PSScriptRoot 'local_runtime.py') --runtime-root $RuntimeRoot --text-model $TextModel --vision-model $VisionModel
if ($LASTEXITCODE -ne 0) { throw 'Local runtime preflight failed' }
Write-Output "Local runtime ready. Activate it with: . '$EnvironmentFile'"
