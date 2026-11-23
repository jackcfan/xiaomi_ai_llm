param(
    [string]$Port = 'COM5',
    [int]$Baud = 115200,
    [string]$Root = 'D:\workspace\github\xiaomi_ai_llm\.tmp-uart',
    [int]$SpamSeconds = 0,
    [switch]$NoSpam
)

$ErrorActionPreference = 'Continue'

$Log      = Join-Path $Root 'uart.log'
$CmdFile  = Join-Path $Root 'uart_in.txt'
$StopFile = Join-Path $Root 'stop'

if (-not (Test-Path $Root)) { New-Item -ItemType Directory -Path $Root -Force | Out-Null }
foreach ($f in @($CmdFile, $StopFile)) { if (Test-Path $f) { Remove-Item $f -Force } }

[System.IO.File]::AppendAllText($Log, "`r`n=== BRIDGE START $(Get-Date -Format s) port=$Port spam=${SpamSeconds}s nospam=$NoSpam ===`r`n")

$sp = New-Object System.IO.Ports.SerialPort $Port, $Baud, 'None', 8, 'One'
$sp.Encoding = [System.Text.Encoding]::ASCII
$sp.ReadTimeout = 200
$sp.WriteTimeout = 500
$sp.DtrEnable = $false
$sp.RtsEnable = $false
$sp.Open()

$sw = [Diagnostics.Stopwatch]::StartNew()
$all = New-Object System.Text.StringBuilder
$caught = [bool]$NoSpam

function Log-Append([string]$s) { [System.IO.File]::AppendAllText($Log, $s) }

function Drain([int]$ms) {
    $deadline = $sw.Elapsed.TotalMilliseconds + $ms
    while ($sw.Elapsed.TotalMilliseconds -lt $deadline) {
        try { $chunk = $sp.ReadExisting() } catch { $chunk = '' }
        if ($chunk.Length -gt 0) { [void]$all.Append($chunk); Log-Append $chunk }
        Start-Sleep -Milliseconds 20
    }
}

while ($true) {
    if (Test-Path $StopFile) { break }

    $t = $sw.Elapsed.TotalSeconds
    if (-not $caught -and $t -lt $SpamSeconds) { try { $sp.Write("`r") } catch { } }

    try { $chunk = $sp.ReadExisting() } catch { $chunk = '' }
    if ($chunk.Length -gt 0) {
        [void]$all.Append($chunk)
        Log-Append $chunk
        if (-not $caught -and $all.ToString().Contains('s12#')) {
            $caught = $true
            Log-Append "`r`n=== PROMPT CAUGHT at $([math]::Round($t,1))s ===`r`n"
        }
    }

    if (Test-Path $CmdFile) {
        Start-Sleep -Milliseconds 150
        try { $lines = [System.IO.File]::ReadAllLines($CmdFile); Remove-Item $CmdFile -Force } catch { $lines = @() }
        foreach ($ln in $lines) {
            if ($ln.Length -eq 0) { continue }
            Log-Append "`r`n>>> $ln`r`n"
            try { $sp.Write($ln + "`r") } catch { }
            # wait for prompt to come back before sending the next command
            $deadline = $sw.Elapsed.TotalMilliseconds + 120000
            Start-Sleep -Milliseconds 200
            while ($sw.Elapsed.TotalMilliseconds -lt $deadline) {
                try { $c2 = $sp.ReadExisting() } catch { $c2 = '' }
                if ($c2.Length -gt 0) { [void]$all.Append($c2); Log-Append $c2 }
                $tail = $all.ToString()
                if ($tail.Length -gt 4 -and $tail.Substring($tail.Length - 4) -eq 's12#') { break }
                Start-Sleep -Milliseconds 40
            }
        }
    }

    Start-Sleep -Milliseconds 30
}

$sp.Close()
$sp.Dispose()
Log-Append "`r`n=== BRIDGE EXIT $(Get-Date -Format s) caught=$caught ===`r`n"
