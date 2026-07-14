param(
    [Parameter(Mandatory=$true)][string]$Uri,
    [Parameter(Mandatory=$true)][string]$Sha256,
    [Parameter(Mandatory=$true)][long]$Size,
    [Parameter(Mandatory=$true)][string]$Destination,
    [int]$Parallel = 16,
    [long]$ChunkBytes = 4194304
)

$ErrorActionPreference = 'Stop'
$Destination = [System.IO.Path]::GetFullPath($Destination)
$ExpectedHash = $Sha256.ToUpperInvariant()
if (Test-Path -LiteralPath $Destination) {
    $Existing = Get-Item -LiteralPath $Destination
    if ($Existing.Length -eq $Size) {
        $ExistingHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Destination).Hash
        if ($ExistingHash -eq $ExpectedHash) {
            Write-Output "verified range download already complete: $Destination"
            exit 0
        }
    }
}
$WorkRoot = "$Destination.parts"
New-Item -ItemType Directory -Force -Path $WorkRoot | Out-Null
$PartCount = [int][math]::Ceiling($Size / $ChunkBytes)

for ($BatchStart = 0; $BatchStart -lt $PartCount; $BatchStart += $Parallel) {
    $Processes = @()
    $BatchEnd = [math]::Min($PartCount - 1, $BatchStart + $Parallel - 1)
    for ($Index = $BatchStart; $Index -le $BatchEnd; $Index++) {
        $Start = [long]($Index * $ChunkBytes)
        $End = [long][math]::Min($Size - 1, $Start + $ChunkBytes - 1)
        $Expected = $End - $Start + 1
        $Part = Join-Path $WorkRoot ("part-{0:D5}" -f $Index)
        if ((Test-Path -LiteralPath $Part) -and (Get-Item -LiteralPath $Part).Length -eq $Expected) {
            continue
        }
        if (Test-Path -LiteralPath $Part) { Remove-Item -Force -LiteralPath $Part }
        $Process = Start-Process -FilePath 'curl.exe' -WindowStyle Hidden -PassThru -ArgumentList @(
            '-L', '--fail', '--retry', '3', '--retry-delay', '2',
            '--range', "$Start-$End", $Uri, '-o', $Part
        )
        $Processes += [PSCustomObject]@{ Process = $Process; Part = $Part; Expected = $Expected }
    }
    foreach ($Entry in $Processes) { $Entry.Process.WaitForExit() }
    $Failures = @()
    foreach ($Entry in $Processes) {
        if ($Entry.Process.ExitCode -ne 0) {
            $Failures += "curl failed for $($Entry.Part) with exit code $($Entry.Process.ExitCode)"
            continue
        }
        $Actual = (Get-Item -LiteralPath $Entry.Part).Length
        if ($Actual -ne $Entry.Expected) {
            $Failures += "range size mismatch for $($Entry.Part): expected $($Entry.Expected), got $Actual"
        }
    }
    if ($Failures.Count) { throw ($Failures -join [Environment]::NewLine) }
    $Completed = Get-ChildItem -File -LiteralPath $WorkRoot | Measure-Object Length -Sum
    $Percent = [math]::Round(100 * $Completed.Sum / $Size, 1)
    Write-Output "range download: $Percent% ($($Completed.Sum)/$Size bytes)"
}

$DestinationDirectory = Split-Path $Destination -Parent
New-Item -ItemType Directory -Force -Path $DestinationDirectory | Out-Null
$TemporaryDestination = "$Destination.assembling"
if (Test-Path -LiteralPath $TemporaryDestination) { Remove-Item -Force -LiteralPath $TemporaryDestination }
$Output = [System.IO.File]::Open($TemporaryDestination, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write)
try {
    for ($Index = 0; $Index -lt $PartCount; $Index++) {
        $Part = Join-Path $WorkRoot ("part-{0:D5}" -f $Index)
        $Input = [System.IO.File]::OpenRead($Part)
        try { $Input.CopyTo($Output) } finally { $Input.Dispose() }
    }
} finally {
    $Output.Dispose()
}

if ((Get-Item -LiteralPath $TemporaryDestination).Length -ne $Size) {
    throw "assembled size mismatch for $TemporaryDestination"
}
$ActualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $TemporaryDestination).Hash
if ($ActualHash -ne $ExpectedHash) {
    throw "assembled SHA256 mismatch: expected $Sha256, got $ActualHash"
}
Move-Item -Force -LiteralPath $TemporaryDestination -Destination $Destination
Write-Output "verified range download complete: $Destination"
