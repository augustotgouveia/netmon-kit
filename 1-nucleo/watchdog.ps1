$CFG = Get-Content (Join-Path $PSScriptRoot 'config.json') -Raw | ConvertFrom-Json
$ok = $false
try {
    $c = New-Object Net.Sockets.TcpClient
    $iar = $c.BeginConnect('127.0.0.1', 8787, $null, $null)
    $ok = $iar.AsyncWaitHandle.WaitOne(3000) -and $c.Connected
    $c.Close()
} catch { $ok = $false }
if (-not $ok) {
    try { Start-ScheduledTask -TaskName 'NetMon' } catch {}
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    Add-Content -Path 'C:\netmon\watchdog.log' -Value ($stamp + '  NetMon fora (porta 8787 sem resposta) -> religado pelo watchdog')
}

# DNS (NetMon-DNS): resolve 'noc' pelo proprio servidor; se falhar, mata o dns.py pendurado e religa
$dnsok = $false
try {
    $r = Resolve-DnsName -Server 127.0.0.1 -Name 'noc' -Type A -DnsOnly -QuickTimeout -ErrorAction Stop
    $dnsok = [bool]($r | Where-Object { $_.IPAddress -eq $CFG.rede.servidor })
} catch { $dnsok = $false }
if (-not $dnsok) {
    try { Get-CimInstance Win32_Process -Filter "name='pythonw.exe'" | Where-Object { $_.CommandLine -like '*dns.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } } catch {}
    Start-Sleep -Milliseconds 500
    try { Start-ScheduledTask -TaskName 'NetMon-DNS' } catch {}
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    Add-Content -Path 'C:\netmon\watchdog.log' -Value ($stamp + '  NetMon-DNS fora (noc nao resolveu pelo servidor) -> religado pelo watchdog')
}
