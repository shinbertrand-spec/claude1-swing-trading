# Position checker - runs every 30 min during US market hours.
# Reads journal/positions.json, fetches live prices from Yahoo Finance,
# and POSTs Telegram alerts for:
#   1. STOP_PROXIMITY     - current price within 2% of stop
#   2. CATALYST_TODAY     - any catalyst date matches today
#   3. TRAIL_TO_BREAKEVEN - position +5% (move stop to entry)
#   4. TRAIL_TO_PLUS5     - position +10% (move stop to +5%)
#   5. PROFIT_TARGET_T1   - current price >= target_1 (consider trimming)
#   6. PROFIT_TARGET_T2   - current price >= target_2 (consider closing)
#
# State (alerts_sent + trail_state) is persisted back to positions.json so
# the same alert doesn't repeat every 30 min.

param(
    [string]$PositionsFile = "C:\Users\User\Desktop\Claude1\journal\positions.json",
    [string]$SendScript    = "C:\Users\User\Desktop\Claude1\scripts\send-to-telegram.ps1",
    [switch]$Force                 # bypass market-hours check (for testing)
)

$ErrorActionPreference = "Stop"

# ---- Market-hours gate (US ET, M-F 9:30 AM - 4:00 PM) ----
function Test-USMarketOpen {
    $utcNow = (Get-Date).ToUniversalTime()
    # Convert to ET. EDT (Mar-Nov) = UTC-4, EST (Nov-Mar) = UTC-5.
    # Rough approximation - doesn't handle exact DST switch dates.
    $month = $utcNow.Month
    $offset = if ($month -ge 3 -and $month -le 10) { -4 } else { -5 }
    $et = $utcNow.AddHours($offset)
    if ($et.DayOfWeek -eq 'Saturday' -or $et.DayOfWeek -eq 'Sunday') { return $false }
    $minutes = $et.Hour * 60 + $et.Minute
    return ($minutes -ge (9 * 60 + 30)) -and ($minutes -le (16 * 60))
}

if (-not $Force -and -not (Test-USMarketOpen)) {
    Write-Output "US market closed - skipping check."
    exit 0
}

# ---- Load positions ----
if (-not (Test-Path -LiteralPath $PositionsFile)) {
    Write-Output "Positions file missing: $PositionsFile"
    exit 0
}
$data = Get-Content -LiteralPath $PositionsFile -Raw | ConvertFrom-Json
if (-not $data.positions -or $data.positions.Count -eq 0) {
    Write-Output "No open positions - nothing to check."
    exit 0
}

# ---- Fetch live prices via yfinance (batched) ----
# Yahoo's free /v7/finance/quote HTTP endpoint started returning 401
# Unauthorized in 2024-2025; replaced with a yfinance-based batched
# fetch via scripts/fetch_prices.py. One Python invocation per run
# (~3s for 7 tickers) vs one HTTP call per ticker in the old design.
function Get-AllPrices {
    param([string[]]$Tickers)
    if (-not $Tickers -or $Tickers.Count -eq 0) { return @{} }
    $helper = "C:\Users\User\Desktop\Claude1\scripts\fetch_prices.py"
    if (-not (Test-Path -LiteralPath $helper)) {
        Write-Warning "fetch_prices.py missing at $helper"
        return @{}
    }
    try {
        # uv run inherits cwd from script; helper script uses only yfinance.
        $json = & uv run python $helper @Tickers 2>$null
        if (-not $json) {
            Write-Warning "fetch_prices.py returned empty output"
            return @{}
        }
        $obj = $json | ConvertFrom-Json
        # PS 5.1 ConvertFrom-Json returns PSCustomObject; convert to hashtable
        # for case-sensitive lookup by ticker.
        $out = @{}
        foreach ($prop in $obj.PSObject.Properties) {
            $out[$prop.Name] = [double]$prop.Value
        }
        return $out
    } catch {
        Write-Warning "Batched price fetch failed: $_"
        return @{}
    }
}

# ---- Alert helper ----
$alertsPosted = 0
function Send-Alert {
    param([string]$Body)
    try {
        & $SendScript -Message $Body | Out-Null
        $script:alertsPosted++
    } catch {
        Write-Warning "Telegram send failed - $_"
    }
}

# ---- Check each position ----
$today = (Get-Date).ToString('yyyy-MM-dd')
$mutated = $false

# Fetch all prices in one batched yfinance call (~3s for 7-10 tickers).
$tickers = @($data.positions | ForEach-Object { $_.ticker })
$prices = Get-AllPrices -Tickers $tickers

foreach ($p in $data.positions) {
    $live = if ($prices.ContainsKey($p.ticker)) { $prices[$p.ticker] } else { $null }
    if (-not $live) {
        Write-Warning "No price for $($p.ticker) - skipping"
        continue
    }

    $pctGain = (($live - $p.entry_price) / $p.entry_price) * 100
    $stopDist = (($live - $p.stop) / $live) * 100  # how far above stop (%)
    $sent = @($p.alerts_sent)

    # 1. STOP PROXIMITY
    if ($stopDist -le 2 -and $stopDist -gt 0 -and $sent -notcontains 'STOP_PROXIMITY') {
        Send-Alert ("*STOP PROXIMITY - {0}*`n`nCurrent: `${1:N2} (P/L: {2:+0.00;-0.00}%)`nStop: `${3:N2} (only {4:N1}% above stop)`n`nAction: re-evaluate thesis or tighten." -f $p.ticker, $live, $pctGain, $p.stop, $stopDist)
        $sent += 'STOP_PROXIMITY'; $mutated = $true
    }
    # Stop breached
    if ($live -le $p.stop -and $sent -notcontains 'STOP_HIT') {
        Send-Alert ("*STOP HIT - {0}*`n`nCurrent: `${1:N2} <= stop `${2:N2}`nLoss: {3:+0.00;-0.00}% from entry `${4:N2}`n`nFramework rule: close without waiting." -f $p.ticker, $live, $p.stop, $pctGain, $p.entry_price)
        $sent += 'STOP_HIT'; $mutated = $true
    }

    # 2. CATALYST TODAY
    if ($p.catalysts) {
        foreach ($cat in $p.catalysts) {
            $catKey = "CATALYST_$($cat.date)"
            if ($cat.date -eq $today -and $sent -notcontains $catKey) {
                Send-Alert ("*CATALYST TODAY - {0}*`n`nEvent: {1}`nCurrent: `${2:N2}`n`nMonitor for thesis confirmation or break." -f $p.ticker, $cat.event, $live)
                $sent += $catKey; $mutated = $true
            }
        }
    }

    # 3. TRAIL TO BREAKEVEN at +5%
    if ($pctGain -ge 5 -and $p.trail_state -eq 'initial') {
        Send-Alert ("*TRAIL STOP TO BREAKEVEN - {0}*`n`nGain: +{1:N2}%  ·  Current `${2:N2}`nMove stop from `${3:N2} to `${4:N2} (entry).`n`nFramework rule: trail to breakeven at +5%." -f $p.ticker, $pctGain, $live, $p.stop, $p.entry_price)
        $p.trail_state = 'breakeven'; $mutated = $true
    }

    # 4. TRAIL TO +5% at +10%
    if ($pctGain -ge 10 -and $p.trail_state -in @('initial','breakeven')) {
        $newStop = [math]::Round($p.entry_price * 1.05, 2)
        Send-Alert ("*TRAIL STOP TO +5% - {0}*`n`nGain: +{1:N2}%  ·  Current `${2:N2}`nMove stop to `${3:N2} (entry + 5%, locks in profit).`n`nFramework rule: trail to +5% at +10%." -f $p.ticker, $pctGain, $live, $newStop)
        $p.trail_state = 'plus5'; $mutated = $true
    }

    # 5. PROFIT TARGET T1
    if ($p.target_1 -and $live -ge $p.target_1 -and $sent -notcontains 'TARGET_T1') {
        $partialShares = [math]::Max(1, [math]::Floor($p.shares / 2))
        Send-Alert ("*TARGET 1 HIT - {0}*`n`nCurrent: `${1:N2} (+{2:N2}%)`nT1: `${3:N2}`n`nFramework suggests: trim {4} of {5} shares, let runner ride to T2 `${6:N2}." -f $p.ticker, $live, $pctGain, $p.target_1, $partialShares, $p.shares, $p.target_2)
        $sent += 'TARGET_T1'; $mutated = $true
    }

    # 6. PROFIT TARGET T2
    if ($p.target_2 -and $live -ge $p.target_2 -and $sent -notcontains 'TARGET_T2') {
        Send-Alert ("*TARGET 2 HIT - {0}*`n`nCurrent: `${1:N2} (+{2:N2}%)`nT2: `${3:N2}`n`nFramework suggests: close remaining position." -f $p.ticker, $live, $pctGain, $p.target_2)
        $sent += 'TARGET_T2'; $mutated = $true
    }

    # write state back
    $p.alerts_sent = $sent
}

# ---- Persist state if any changes ----
if ($mutated) {
    # $data is a PSCustomObject from ConvertFrom-Json. Setting a nonexistent
    # property fails with "The property cannot be found" — the positions.json
    # never carried an 'updated' field, so we add-or-overwrite via Add-Member
    # with -Force (idempotent across runs).
    $data | Add-Member -NotePropertyName 'updated' -NotePropertyValue ((Get-Date).ToString('o')) -Force
    $data | ConvertTo-Json -Depth 10 | Out-File -LiteralPath $PositionsFile -Encoding UTF8
}

Write-Output "Position check complete. Alerts posted - $alertsPosted"
