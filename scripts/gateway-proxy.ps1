function Initialize-GatewayProxyEnvironment {
    param([object]$InternetSettings = $null)

    if ([string]::IsNullOrWhiteSpace($env:HTTP_PROXY) -or [string]::IsNullOrWhiteSpace($env:HTTPS_PROXY)) {
        if ($null -eq $InternetSettings) {
            $InternetSettings = Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
        }
        if ($null -ne $InternetSettings) {
            $enabled = $InternetSettings.PSObject.Properties['ProxyEnable']
            $server = $InternetSettings.PSObject.Properties['ProxyServer']
            if ($null -ne $enabled -and [int]$enabled.Value -eq 1 -and $null -ne $server) {
                $proxySpec = ([string]$server.Value).Trim()
                $systemProxies = @{}
                if ($proxySpec.Contains('=')) {
                    foreach ($entry in ($proxySpec -split ';')) {
                        if ($entry -match '^\s*(https?)\s*=\s*(\S.*?)\s*$') {
                            $systemProxies[$Matches[1].ToLowerInvariant()] = $Matches[2]
                        }
                    }
                } elseif (-not [string]::IsNullOrWhiteSpace($proxySpec)) {
                    $systemProxies['http'] = $proxySpec
                    $systemProxies['https'] = $proxySpec
                }
                foreach ($scheme in @('http', 'https')) {
                    $variable = "${scheme}_PROXY".ToUpperInvariant()
                    if (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($variable, 'Process'))) { continue }
                    if (-not $systemProxies.ContainsKey($scheme)) { continue }
                    $proxyUrl = $systemProxies[$scheme]
                    if ($proxyUrl -notmatch '^[a-z][a-z0-9+.-]*://') { $proxyUrl = "http://$proxyUrl" }
                    [Environment]::SetEnvironmentVariable($variable, $proxyUrl, 'Process')
                }
            }
        }
    }

    # 本地各层使用直连；分别补全地址，保留用户已有的其他绕过规则。
    $bypass = @($env:NO_PROXY -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    foreach ($address in @('127.0.0.1', 'localhost', '::1')) {
        if ($address -notin $bypass) { $bypass += $address }
    }
    $env:NO_PROXY = $bypass -join ','
}
