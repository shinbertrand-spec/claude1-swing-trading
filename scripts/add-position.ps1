# Helper - append a new position to journal/positions.json after you place a trade.
#
# USAGE
#   .\add-position.ps1 -Ticker JCI -EntryPrice 145.33 -Shares 3 -Stop 134.10 -Target1 165 -Target2 180 -Sector Industrials -Thesis "Record backlog + post-earnings pullback"
#   .\add-position.ps1 -Ticker JCI -EntryPrice 145.33 -Shares 3 -Stop 134.10 -Target1 165 -CatalystDate 2026-07-15 -CatalystEvent "Q2 earnings"

param(
    [Parameter(Mandatory=$true)][string]$Ticker,
    [Parameter(Mandatory=$true)][double]$EntryPrice,
    [Parameter(Mandatory=$true)][int]$Shares,
    [Parameter(Mandatory=$true)][double]$Stop,
    [Parameter(Mandatory=$true)][double]$Target1,
    [double]$Target2 = 0,
    [string]$Sector = "Unknown",
    [string]$Thesis = "",
    [string]$EntryDate = (Get-Date).ToString('yyyy-MM-dd'),
    [string]$CatalystDate = "",
    [string]$CatalystEvent = "",
    [string]$PositionsFile = "C:\Users\User\Desktop\Claude1\journal\positions.json"
)

$ErrorActionPreference = "Stop"

$data = Get-Content -LiteralPath $PositionsFile -Raw | ConvertFrom-Json
$existing = $data.positions | Where-Object { $_.ticker -eq $Ticker }
if ($existing) {
    Write-Error "Position for $Ticker already exists. Edit positions.json directly to modify or remove first."
    exit 1
}

$catalysts = @()
if ($CatalystDate -and $CatalystEvent) {
    $catalysts += [pscustomobject]@{ date = $CatalystDate; event = $CatalystEvent }
}

$pos = [pscustomobject]@{
    ticker        = $Ticker.ToUpper()
    entry_date    = $EntryDate
    entry_price   = $EntryPrice
    shares        = $Shares
    stop          = $Stop
    target_1      = $Target1
    target_2      = if ($Target2 -gt 0) { $Target2 } else { $null }
    thesis        = $Thesis
    sector        = $Sector
    catalysts     = $catalysts
    trail_state   = "initial"
    alerts_sent   = @()
}

$data.positions = @($data.positions) + $pos
$data.updated = (Get-Date).ToString('o')
$data | ConvertTo-Json -Depth 10 | Out-File -LiteralPath $PositionsFile -Encoding UTF8

Write-Output ("Added position - {0} {1} shares @ `${2:N2} (stop `${3:N2}, T1 `${4:N2})" -f $Ticker, $Shares, $EntryPrice, $Stop, $Target1)
Write-Output "Stop distance - {0:N2}%" -f ((($EntryPrice - $Stop) / $EntryPrice) * 100)
if ($Target2 -gt 0) {
    $rr = ($Target1 - $EntryPrice) / ($EntryPrice - $Stop)
    Write-Output ("R:R to T1 - {0:N2}:1" -f $rr)
}
