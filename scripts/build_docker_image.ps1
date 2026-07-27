param(
    [string]$ImageTag = "finevision-to-sharegpt:latest",
    [string]$OutputTar = "finevision-to-sharegpt_latest.tar",
    [string]$Platform = "linux/amd64"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Push-Location $ProjectRoot
try {
    docker build --platform $Platform -t $ImageTag .
    docker save $ImageTag -o $OutputTar

    $Saved = Get-Item -LiteralPath $OutputTar
    Write-Host "Saved Docker image $ImageTag to $($Saved.FullName)"
    Write-Host "Size: $([Math]::Round($Saved.Length / 1MB, 2)) MB"
}
finally {
    Pop-Location
}
