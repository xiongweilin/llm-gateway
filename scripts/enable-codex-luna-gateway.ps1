#Requires -Version 7.0
[CmdletBinding()]
param(
    [ValidateRange(120,900)][int]$TimeoutSeconds = 360,
    [switch]$Worker
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $Worker) {
    $workerRoot = Join-Path ([IO.Path]::GetTempPath()) ('ratio-luna-worker-' + [guid]::NewGuid().ToString('N'))
    $workerOut = Join-Path $workerRoot 'worker.out.log'
    $workerErr = Join-Path $workerRoot 'worker.err.log'
    New-Item -ItemType Directory -Path $workerRoot -Force | Out-Null
    $pwshExe = (Get-Process -Id $PID).Path
    if (-not (Test-Path -LiteralPath $pwshExe -PathType Leaf)) {
        $pwshExe = Join-Path $PSHOME 'pwsh.exe'
    }
    $workerArgs = @(
        '-NoProfile',
        '-NonInteractive',
        '-ExecutionPolicy', 'Bypass',
        '-File', $PSCommandPath,
        '-Worker',
        '-TimeoutSeconds', $TimeoutSeconds.ToString()
    )
    $workerProcess = Start-Process -FilePath $pwshExe -ArgumentList $workerArgs -WorkingDirectory $PSScriptRoot -RedirectStandardOutput $workerOut -RedirectStandardError $workerErr -WindowStyle Hidden -PassThru
    Write-Host "[ratio-luna] 独立 worker 已启动（pid=$($workerProcess.Id)，日志=$workerRoot）"
    $parentDeadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds + 180)
    $lastReport = [DateTime]::UtcNow
    while (-not $workerProcess.HasExited) {
        if ([DateTime]::UtcNow -ge $parentDeadline) {
            Write-Host "[ratio-luna] 前台等待已到上限；独立 worker 继续运行，日志=$workerRoot"
            exit 3
        }
        if (([DateTime]::UtcNow - $lastReport).TotalSeconds -ge 30) {
            Write-Host "[ratio-luna] 独立 worker 仍在运行（pid=$($workerProcess.Id)）"
            $lastReport = [DateTime]::UtcNow
        }
        Start-Sleep -Seconds 1
        $workerProcess.Refresh()
    }
    if (Test-Path -LiteralPath $workerOut -PathType Leaf) {
        Get-Content -LiteralPath $workerOut
    }
    if (Test-Path -LiteralPath $workerErr -PathType Leaf) {
        Get-Content -LiteralPath $workerErr
    }
    exit $workerProcess.ExitCode
}

$GatewayRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$GatewayConfigPath = Join-Path $GatewayRoot 'config\gateway.json'
$GatewayConfig = Get-Content -LiteralPath $GatewayConfigPath -Raw | ConvertFrom-Json
$ListenHost = [string]$GatewayConfig.listen_host
$CorePort = [int]$GatewayConfig.ports.core
$AgentPort = [int]$GatewayConfig.ports.agent
$ResponsesPort = [int]$GatewayConfig.ports.responses
$ChatPort = [int]$GatewayConfig.ports.chat
$GatewayPorts = @($CorePort, $AgentPort, $ResponsesPort, $ChatPort)
$CoreServiceUrl = "http://${ListenHost}:$CorePort"
$AgentServiceUrl = "http://${ListenHost}:$AgentPort"
$ResponsesServiceUrl = "http://${ListenHost}:$ResponsesPort"
$CoreBaseUrl = "$CoreServiceUrl/v1"
$AgentBaseUrl = "$AgentServiceUrl/v1"
$ResponsesBaseUrl = "$ResponsesServiceUrl/v1"
$CodexBaseUrl = $AgentBaseUrl
$ControlRoot = [Environment]::GetEnvironmentVariable('AIOS_ROOT')
if ([string]::IsNullOrWhiteSpace($ControlRoot)) { throw 'AIOS_ROOT must identify the AIOS monorepo for Control Plane integration.' }
$ControlRoot = (Resolve-Path -LiteralPath $ControlRoot).Path
$ControlPort = 0
if (-not [int]::TryParse([Environment]::GetEnvironmentVariable('CONTROL_PLANE_PORT'), [ref]$ControlPort) -or $ControlPort -lt 1 -or $ControlPort -gt 65535) {
    throw 'CONTROL_PLANE_PORT must contain the configured Control Plane port.'
}
$ControlBaseUrl = "http://${ListenHost}:$ControlPort"
$CodexHome = Join-Path $env:USERPROFILE '.codex'
$CodexConfig = Join-Path $CodexHome 'config.toml'
$CodexModelCache = Join-Path $CodexHome 'models_cache.json'
$OfficialModel = 'gpt-6-luna'
$GatewayTask = 'LLM-Gateway-Agent-Entry'
$ControlTask = 'ControlPlane'
$GatewayStart = Join-Path $GatewayRoot 'scripts\start-agent-gateway.ps1'
$GatewayWatch = Join-Path $GatewayRoot 'scripts\watch-agent-gateway.ps1'
$GatewayWatchPattern = [regex]::Escape($GatewayWatch)
$ExistingGatewayTask = Get-ScheduledTask -TaskPath '\' -ErrorAction SilentlyContinue |
    Where-Object {
        $ActionText = [string]::Join(' ', @($_.Actions | ForEach-Object { [string]$_.Execute; [string]$_.Arguments }))
        $ActionText -match $GatewayWatchPattern
    } |
    Select-Object -First 1
if ($null -ne $ExistingGatewayTask) { $GatewayTask = [string]$ExistingGatewayTask.TaskName }
$ControlPython = if (-not [string]::IsNullOrWhiteSpace($env:AIOS_PYTHON)) { $env:AIOS_PYTHON } else { Join-Path $ControlRoot '.venv\Scripts\python.exe' }
$ControlConfig = [Environment]::GetEnvironmentVariable('AIOS_CONTROL_PLANE_CONFIG')
if ([string]::IsNullOrWhiteSpace($ControlConfig)) { throw 'AIOS_CONTROL_PLANE_CONFIG must identify the Control Plane configuration file.' }
if (-not [IO.Path]::IsPathRooted($ControlConfig)) { $ControlConfig = Join-Path $ControlRoot $ControlConfig }
$ControlConfig = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($ControlConfig))
$ControlConfigPy = Join-Path $ControlRoot 'src\domains\control_plane\config.py'
$ControlAlertPy = Join-Path $ControlRoot 'src\domains\control_plane\alert_policy.py'
$Schtasks = Join-Path $env:SystemRoot 'System32\schtasks.exe'

$tx = Join-Path ([IO.Path]::GetTempPath()) ('ratio-luna-gateway-' + [guid]::NewGuid().ToString('N'))
$backup = Join-Path $tx 'backup'
$log = Join-Path $tx 'run.log'
$snapshots = @{}
$changed = [System.Collections.Generic.List[string]]::new()
$rollbackErrors = [System.Collections.Generic.List[string]]::new()
$stage = 'init'
$gatewayTouched = $false
$controlTouched = $false
$gatewayWasReady = $false
$gatewayModelsMatched = $false
$controlWasLive = $false
$gatewayTaskBefore = $null
$controlTaskBefore = $null
$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
New-Item -ItemType Directory -Path $backup -Force | Out-Null

function Event([string]$Message) {
    try { Add-Content -LiteralPath $script:log -Value "$(Get-Date -Format o) stage=$script:stage event=$Message" -Encoding utf8 } catch {}
}
function Say([string]$Message) { Write-Host "[ratio-luna] $Message"; Event $Message }
function Fail([string]$Message) { throw $Message }
function Remaining([int]$Requested = 30) {
    if ($script:stage -eq 'rollback') { return $Requested }
    $n = [int][Math]::Ceiling(($script:deadline - [DateTime]::UtcNow).TotalSeconds)
    if ($n -lt 1) { Fail 'global timeout reached' }
    return [Math]::Min($Requested,$n)
}
function Snapshot([string]$Path,[string]$Name) {
    if (Test-Path -LiteralPath $Path -PathType Container) { Fail "target is a directory: $Name" }
    $exists = Test-Path -LiteralPath $Path -PathType Leaf
    $dst = Join-Path $script:backup $Name
    if ($exists) { Copy-Item -LiteralPath $Path -Destination $dst -Force }
    $script:snapshots[$Path] = [pscustomobject]@{Path=$Path;Backup=$dst;Exists=$exists}
}
function MarkChanged([string]$Path) { if (-not $script:changed.Contains($Path)) { $null=$script:changed.Add($Path) } }
function ReadText([string]$Path) { return [IO.File]::ReadAllText($Path) }
function WriteText([string]$Path,[string]$Text) { Set-Content -LiteralPath $Path -Value $Text -Encoding utf8NoBOM -NoNewline }
function SetText([string]$Path,[string]$Before,[string]$After) {
    if ($Before -ceq $After) { return $false }
    MarkChanged $Path
    WriteText $Path $After
    return $true
}
function ReplaceOnce([string]$Path,[string]$Old,[string]$New) {
    $text=ReadText $Path
    $first=$text.IndexOf($Old,[StringComparison]::Ordinal)
    if($first -lt 0){if($text.Contains($New,[StringComparison]::Ordinal)){return};Fail "expected anchor missing: $Path"}
    if($first -ne $text.LastIndexOf($Old,[StringComparison]::Ordinal)){Fail "expected anchor is not unique: $Path"}
    $null=SetText $Path $text ($text.Remove($first,$Old.Length).Insert($first,$New))
}
function NormalizeControlConfigModels([string]$Path) {
    # 把 control-plane 默认模型统一到 gpt-6-luna（幂等：已是目标值时不改动；
    # 兼容 gpt-5.6-luna / gpt-6-luna 以及带 codex/ 前缀的历史写法）。
    $text=ReadText $Path
    $pattern='(?m)^([ \t]*(?:diagnosis_model|execution_model): str = ")(?:codex/)?gpt-[0-9.]+-luna(")$'
    $updated=[regex]::Replace($text,$pattern,{param($m) $m.Groups[1].Value+'gpt-6-luna'+$m.Groups[2].Value})
    $null=SetText $Path $text $updated
}
function TaskInfo([string]$Name) {
    $t=Get-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction Stop
    return [pscustomobject]@{Name=$Name;Enabled=[bool]$t.Settings.Enabled;State=[string]$t.State;Action=([string]::Join(' ',@($t.Actions|ForEach-Object{[string]$_.Execute;[string]$_.Arguments})) )}
}
function AssertCleanTarget([string]$Repo,[string]$RelativePath) {
    $git=(Get-Command git.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Path
    $result=@(& $git -C $Repo status --porcelain --untracked-files=all -- $RelativePath 2>$null)
    $ec=$LASTEXITCODE
    if($ec -ne 0){Fail "git status failed for target: $RelativePath"}
    if(@($result).Count -gt 0){Fail "target has uncommitted changes: $RelativePath"}
}
function Native([string[]]$Args,[int]$Limit=30,[int[]]$Allowed=@(0)) {
    $si=[Diagnostics.ProcessStartInfo]::new();$si.FileName=$Schtasks;$si.UseShellExecute=$false;$si.CreateNoWindow=$true;$si.RedirectStandardOutput=$true;$si.RedirectStandardError=$true
    foreach($a in $Args){$null=$si.ArgumentList.Add([string]$a)}
    $p=[Diagnostics.Process]::new();$p.StartInfo=$si
    try{if(-not $p.Start()){Fail 'native command did not start'};$p.BeginOutputReadLine();$p.BeginErrorReadLine();if(-not $p.WaitForExit(([int](Remaining $Limit)*1000))){try{$p.Kill($true)}catch{};Fail 'native command timeout'};$ec=[int]$p.ExitCode}finally{$p.Dispose()}
    if($Allowed -notcontains $ec){Fail "native command failed: exit=$ec"}
}
function TaskRun([string]$Name,[scriptblock]$Ready=$null){
    $before=(Get-ScheduledTaskInfo -TaskName $Name -TaskPath '\' -ErrorAction Stop).LastRunTime
    for($attempt=1;$attempt-le2;$attempt++){
        Native @('/Run','/TN',('\'+$Name)) 30
        # 宽限 45 秒：/Run 返回后任务状态可能先显示 Ready，子进程要几秒后才进入 Running。
        $end=[DateTime]::UtcNow.AddSeconds(45)
        while([DateTime]::UtcNow-lt$end){
            $task=Get-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction Stop
            $info=Get-ScheduledTaskInfo -TaskName $Name -TaskPath '\' -ErrorAction Stop
            if([string]$task.State-eq'Running'-or$info.LastRunTime-gt$before){return}
            if($null-ne$Ready-and(& $Ready)){return}
            Start-Sleep -Milliseconds 500
        }
    }
    Fail "scheduled task did not start: $Name"
}
function TaskEnd([string]$Name){Native @('/End','/TN',('\'+$Name)) 30 @(0,1,128)}
function TaskEnable([string]$Name,[bool]$Enable){if($Enable){Native @('/Change','/TN',('\'+$Name),'/ENABLE') 30}else{Native @('/Change','/TN',('\'+$Name),'/DISABLE') 30}}
function PortPids([int]$Port){return @((Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue|ForEach-Object{[int]$_.OwningProcess}|Sort-Object -Unique))}
function CmdLine([int]$ProcessId){$p=Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue|Select-Object -First 1;if($null -eq $p){return ''};return [string]$p.CommandLine}
function GatewayOwner([int]$Port,[int]$ProcessId){$c=CmdLine $ProcessId;$root=[regex]::Escape($GatewayRoot);if([string]::IsNullOrWhiteSpace($c)-or$c-notmatch "(?i)$root"){return $false};return [bool]($c-match '(?i)llm_gateway\.core_server|agent-gateway\.py|responses-proxy\.py|run_server\.py|chat-completions-proxy\.py')}
function ControlOwner([int]$ProcessId){
    $p=Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue|Select-Object -First 1
    if($null -eq $p -or $p.Name -notmatch '(?i)^python(\.exe)?$' -or [string]$p.CommandLine -notmatch '(?i)\s-m\s+control_plane(\s|$)'){return $false}
    if(([string]$p.ExecutablePath).Equals($ControlPython,[StringComparison]::OrdinalIgnoreCase)){return $true}
    $parent=Get-CimInstance Win32_Process -Filter "ProcessId = $($p.ParentProcessId)" -ErrorAction SilentlyContinue|Select-Object -First 1
    if($null -ne $parent -and ([string]$parent.ExecutablePath).Equals($ControlPython,[StringComparison]::OrdinalIgnoreCase) -and [string]$parent.CommandLine -match '(?i)\s-m\s+control_plane(\s|$)'){return $true}
    return $false
}
function AssertPortSafe([int]$Port,[string]$Kind){foreach($id in @(PortPids $Port)){$ok=if($Kind-eq'gateway'){GatewayOwner $Port $id}else{ControlOwner $id};if(-not$ok){Fail "unknown process owns local port $Port"}}}
function StopTree([int]$ProcessId){
    if($null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)){return}
    $taskkill=Join-Path ([Environment]::GetFolderPath('Windows')) 'System32\taskkill.exe'
    try{
        $null=& $taskkill /PID $ProcessId /T /F 2>$null
        if($LASTEXITCODE -ne 0 -and $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)){
            Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
        }
        $end=[DateTime]::UtcNow.AddSeconds(20)
        while([DateTime]::UtcNow-lt$end){
            if($null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)){return}
            Start-Sleep -Milliseconds 250
        }
        Fail "process tree did not exit: pid=${ProcessId}"
    }catch{
        Fail "unable to terminate process tree pid=${ProcessId}: $($_.Exception.Message)"
    }
}
function GatewayTreeRoot([int]$ProcessId){
    $process=Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue|Select-Object -First 1
    if ($null -eq $process) { return $ProcessId }
    $parent=Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ParentProcessId)" -ErrorAction SilentlyContinue|Select-Object -First 1
    if (
        $null -ne $parent -and
        [string]$parent.CommandLine -match [regex]::Escape($GatewayRoot)
    ) {
        return [int]$parent.ProcessId
    }
    return $ProcessId
}
function StopGatewayProcesses{foreach($port in $GatewayPorts){AssertPortSafe $port 'gateway'};$roots=@($GatewayPorts|ForEach-Object{PortPids $_}|ForEach-Object{GatewayTreeRoot $_}|Sort-Object -Unique);foreach($id in $roots){StopTree $id};$end=[DateTime]::UtcNow.AddSeconds(30);while([DateTime]::UtcNow-lt$end){$busy=$false;foreach($port in $GatewayPorts){if(@(PortPids $port).Count-gt0){$busy=$true}};if(-not$busy){return};Start-Sleep -Milliseconds 300};Fail 'gateway ports did not clear'}
function StopControlProcess{AssertPortSafe $ControlPort 'control';foreach($id in @(PortPids $ControlPort)){StopTree $id};$end=[DateTime]::UtcNow.AddSeconds(30);while([DateTime]::UtcNow-lt$end){if(@(PortPids $ControlPort).Count-eq0){return};Start-Sleep -Milliseconds 300};Fail 'control-plane port did not clear'}
function Status([string]$Url){try{return [int](Invoke-WebRequest -Uri $Url -TimeoutSec 3 -SkipHttpErrorCheck).StatusCode}catch{return 0}}
function ResolveCodexExe{
    if (
        -not [string]::IsNullOrWhiteSpace($env:CODEX_CLI_PATH) -and
        (Test-Path -LiteralPath $env:CODEX_CLI_PATH -PathType Leaf)
    ) {
        return [IO.Path]::GetFullPath($env:CODEX_CLI_PATH)
    }
    $command = Get-Command codex.exe -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (
        $null -ne $command -and
        (Test-Path -LiteralPath $command.Path -PathType Leaf)
    ) {
        return [IO.Path]::GetFullPath($command.Path)
    }
    $bin = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
    if (Test-Path -LiteralPath $bin -PathType Container) {
        $exe = Get-ChildItem -LiteralPath $bin -Filter codex.exe -File -Recurse -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($null -ne $exe) {
            return $exe.FullName
        }
    }
    Fail 'Codex CLI executable not found'
}
function OfficialModelSlugs{
    $catalog=Get-Content -Raw -LiteralPath $CodexModelCache|ConvertFrom-Json
    return @(
        $catalog.models |
            Where-Object { [string]$_.visibility -eq 'list' } |
            ForEach-Object { [string]$_.slug }
    )
}
function GatewayHealthy {
    return (
        (Status "$CoreServiceUrl/health/liveliness") -eq 200 -and
        (Status "$AgentServiceUrl/health/liveliness") -eq 200 -and
        (Status "$ResponsesServiceUrl/health/liveliness") -eq 200 -and
        (Status "http://${ListenHost}:$ChatPort/health/liveliness") -eq 200
    )
}
function GatewayModelsMatch{
    if (-not (GatewayHealthy)) { return $false }
    try{
        $expected=@(OfficialModelSlugs|Sort-Object -Unique)
        $m=Invoke-RestMethod -Uri "$AgentBaseUrl/models" -TimeoutSec 5
        $actual=@($m.data|ForEach-Object{[string]$_.id}|Sort-Object -Unique)
        return (
            $expected.Count -eq $actual.Count -and
            [string]::Join("`n",$expected) -ceq [string]::Join("`n",$actual)
        )
    }catch{return $false}
}
function GatewayReady { return ((GatewayHealthy) -and (GatewayModelsMatch)) }
function WaitGateway([int]$Limit=180){$end=[DateTime]::UtcNow.AddSeconds((Remaining $Limit));while([DateTime]::UtcNow-lt$end){if(GatewayReady){return};Start-Sleep -Seconds 2};Fail 'gateway readiness timeout'}
function WaitControl([int]$Limit=120){$end=[DateTime]::UtcNow.AddSeconds((Remaining $Limit));while([DateTime]::UtcNow-lt$end){if((Status "$ControlBaseUrl/live")-eq200){return};Start-Sleep -Seconds 2};Fail 'control-plane liveness timeout'}
function SetTopLevelCodexRouting{
    $path=$CodexConfig
    $text=ReadText $path
    $nl=[Environment]::NewLine
    $final=$text.EndsWith($nl)

    # Remove the gateway-owned provider section so reruns stay idempotent.
    $withoutProvider=[regex]::Replace(
        $text,
        '(?ms)^\[model_providers\.llm-gateway\]\s*\r?\n.*?(?=^\[[^\]]+\]\s*$|\z)',
        ''
    )
    $a=[regex]::Split($withoutProvider,'\r?\n')
    if($final -and $a.Count -gt 1 -and $a[$a.Count-1] -eq ''){$a=$a[0..($a.Count-2)]}
    $lines=[System.Collections.Generic.List[string]]::new()
    foreach($line in $a){$null=$lines.Add($line)}

    $first=$lines.Count
    for($i=0;$i-lt$lines.Count;$i++){
        if($lines[$i]-match '^\s*\[[^\]]+\]\s*$'){$first=$i;break}
    }

    foreach($key in @('model','model_provider','openai_base_url')){
        if(@($lines|Where-Object{$_-match ('^\s*'+[regex]::Escape($key)+'\s*=')}).Count -gt 1){
            Fail "duplicate top-level $key"
        }
    }

    # openai_base_url belongs to the built-in provider and must not be mixed with
    # the explicit gateway provider.
    for($i=$first-1;$i-ge0;$i--){
        if($lines[$i]-match '^\s*openai_base_url\s*='){
            $lines.RemoveAt($i)
            $first--
        }
    }

    $model=-1
    $provider=-1
    for($i=0;$i-lt$first;$i++){
        if($lines[$i]-match '^\s*model\s*='){$model=$i}
        elseif($lines[$i]-match '^\s*model_provider\s*='){$provider=$i}
    }
    if($model-ge0){$lines[$model]='model = "'+$OfficialModel+'"'}
    else{$lines.Insert($first,'model = "'+$OfficialModel+'"');$model=$first;$first++}
    if($provider-ge0){$lines[$provider]='model_provider = "llm-gateway"'}
    else{$lines.Insert($model+1,'model_provider = "llm-gateway"');$first++}

    while($lines.Count -gt 0 -and [string]::IsNullOrWhiteSpace($lines[$lines.Count-1])){
        $lines.RemoveAt($lines.Count-1)
    }
    if($lines.Count -gt 0){$null=$lines.Add('')}
    foreach($line in @(
        '[model_providers.llm-gateway]',
        'name = "LLM Gateway"',
        'base_url = "'+$CodexBaseUrl+'"',
        'wire_api = "responses"',
        'requires_openai_auth = true',
        'supports_websockets = false'
    )){$null=$lines.Add($line)}

    $after=[string]::Join($nl,$lines.ToArray())
    if($final){$after+=$nl}
    $null=SetText $path $text $after
}
function SetSection([string]$Text,[string]$Section,[string[]]$Keys,[hashtable]$Values){
    $nl=[Environment]::NewLine;$final=$Text.EndsWith($nl);$a=[regex]::Split($Text,'\r?\n');if($final -and $a.Count -gt 1 -and $a[$a.Count-1] -eq ''){$a=$a[0..($a.Count-2)]};$lines=[System.Collections.Generic.List[string]]::new();foreach($line in $a){$null=$lines.Add($line)}
    $headers=@();for($i=0;$i -lt $lines.Count;$i++){if($lines[$i]-match '^\s*\[[^\]]+\]\s*$'){$headers+=$i}};$wanted=@();foreach($h in $headers){if($lines[$h]-match "^\s*\[$([regex]::Escape($Section))\]\s*$"){$wanted+=$h}}
    if(@($wanted).Count-gt1){Fail "duplicate TOML section: $Section"}
    if(@($wanted).Count -eq 0){$at=$lines.Count;if($Section -eq 'model'){for($i=0;$i -lt $lines.Count;$i++){if($lines[$i]-match '^\s*\[agent\]\s*$'){$at=$i;break}}};$new=@("[$Section]");foreach($key in $Keys){$new+=('{0} = "{1}"'-f $key,$Values[$key])};$new+='';for($j=$new.Count-1;$j -ge 0;$j--){$lines.Insert($at,$new[$j])}}
    else{$start=$wanted[0];$end=$lines.Count;foreach($h in $headers){if($h -gt $start){$end=$h;break}};foreach($key in $Keys){$hits=@();for($i=$start+1;$i -lt $end;$i++){if($lines[$i]-match "^\s*$key\s*="){$hits+=$i}};if(@($hits).Count -gt 1){Fail "duplicate TOML key: $Section.$key"};$line=('{0} = "{1}"'-f $key,$Values[$key]);if(@($hits).Count -eq 1){$lines[$hits[0]]=$line}else{$lines.Insert($end,$line);$end++}}}
    $r=[string]::Join($nl,$lines.ToArray());if($final){$r+=$nl};return $r
}
function UpdatePersistentConfig{
    NormalizeControlConfigModels $ControlConfigPy
    $codexCli=(ResolveCodexExe)-replace '\\','/'
    $before=ReadText $ControlConfig;$t=SetSection $before 'model' @('diagnosis_model','execution_model','gateway_base_url','codex_cli') @{diagnosis_model=$OfficialModel;execution_model=$OfficialModel;gateway_base_url=$AgentBaseUrl;codex_cli=$codexCli};$t=SetSection $t 'agent' @('model','gateway_base_url') @{model=$OfficialModel;gateway_base_url=$AgentBaseUrl};$null=SetText $ControlConfig $before $t
    SetTopLevelCodexRouting
}
function GatewayProbe{$m=Invoke-RestMethod -Uri "$CodexBaseUrl/models" -TimeoutSec 30;$ids=@($m.data|ForEach-Object{[string]$_.id});if($ids -notcontains $OfficialModel){Fail "gateway catalog probe failed: missing $OfficialModel"}}
function CodexProbe{
    $exe=ResolveCodexExe;$si=[Diagnostics.ProcessStartInfo]::new();$si.FileName=$exe;$si.WorkingDirectory=$GatewayRoot;$si.UseShellExecute=$false;$si.CreateNoWindow=$true;$si.RedirectStandardOutput=$true;$si.RedirectStandardError=$true;$si.Environment['CODEX_HOME']=$CodexHome
    foreach($a in @('exec','--model',$OfficialModel,'--sandbox','read-only','--skip-git-repo-check','--ephemeral','--json','Return exactly RATIO_CODEX_GATEWAY_OK and nothing else.')){$null=$si.ArgumentList.Add($a)};$p=[Diagnostics.Process]::new();$p.StartInfo=$si
    try{if(-not$p.Start()){Fail 'codex probe did not start'};$p.BeginOutputReadLine();$p.BeginErrorReadLine();if(-not$p.WaitForExit(([int](Remaining 150)*1000))){try{$p.Kill($true)}catch{};Fail 'codex probe timeout'};$ec=$p.ExitCode}finally{$p.Dispose()};if($ec-ne0){Fail "codex probe failed: exit=$ec"}
}
function PythonConfigProbe{
    $code="from control_plane.config import ControlPlaneConfig; from control_plane.codex_provider import CodexProvider; c=ControlPlaneConfig.load(); assert c.diagnosis_model=='$OfficialModel' and c.execution_model=='$OfficialModel' and c.gateway_base_url=='$AgentBaseUrl' and c.codex_cli.is_file() and hasattr(CodexProvider,'invoke'); print('RATIO_CONTROL_OK')"
    $si=[Diagnostics.ProcessStartInfo]::new();$si.FileName=$ControlPython;$si.WorkingDirectory=$ControlRoot;$si.UseShellExecute=$false;$si.CreateNoWindow=$true;$si.RedirectStandardOutput=$true;$si.RedirectStandardError=$true;$si.Environment['CONTROL_PLANE_API_KEY']='ratio-luna-probe';foreach($a in @('-c',$code)){$null=$si.ArgumentList.Add($a)};$p=[Diagnostics.Process]::new();$p.StartInfo=$si
    try{if(-not$p.Start()){Fail 'python config probe did not start'};$p.BeginOutputReadLine();$p.BeginErrorReadLine();if(-not$p.WaitForExit(([int](Remaining 30)*1000))){try{$p.Kill($true)}catch{};Fail 'python config probe timeout'};$ec=$p.ExitCode}finally{$p.Dispose()};if($ec-ne0){Fail "control-plane config probe failed: exit=$ec"}
}
function RestoreFiles{for($i=$script:changed.Count-1;$i-ge0;$i--){$p=$script:changed[$i];$s=$script:snapshots[$p];if($s.Exists){Copy-Item -LiteralPath $s.Backup -Destination $p -Force}elseif(Test-Path -LiteralPath $p -PathType Leaf){Remove-Item -LiteralPath $p -Force}}}
function Rollback{
    if($script:controlTouched){try{TaskEnd $ControlTask;StopControlProcess}catch{$script:rollbackErrors.Add('control stop')|Out-Null}}
    if($script:gatewayTouched){try{TaskEnd $GatewayTask;StopGatewayProcesses}catch{$script:rollbackErrors.Add('gateway stop')|Out-Null}}
    try{RestoreFiles}catch{$script:rollbackErrors.Add('file restore')|Out-Null}
    try{if($script:gatewayTouched-and$null-ne$script:gatewayTaskBefore){TaskEnable $GatewayTask $script:gatewayTaskBefore.Enabled;if($script:gatewayWasReady){TaskEnable $GatewayTask $true;TaskRun $GatewayTask;WaitGateway 90;if(-not$script:gatewayTaskBefore.Enabled){TaskEnable $GatewayTask $false}}}}catch{$script:rollbackErrors.Add('gateway state')|Out-Null}
    try{if($script:controlTouched-and$null-ne$script:controlTaskBefore){TaskEnable $ControlTask $script:controlTaskBefore.Enabled;if($script:controlWasLive){TaskEnable $ControlTask $true;TaskRun $ControlTask;WaitControl 90;if(-not$script:controlTaskBefore.Enabled){TaskEnable $ControlTask $false}}}}catch{$script:rollbackErrors.Add('control state')|Out-Null}
    return($script:rollbackErrors.Count-eq0)
}

try{
    $stage='preflight';foreach($p in @($GatewayStart,$GatewayWatch,$ControlPython,$ControlConfig,$ControlConfigPy,$ControlAlertPy,$CodexConfig,$CodexModelCache)){if(-not(Test-Path -LiteralPath $p -PathType Leaf)){Fail "missing target: $p"}}
    $gatewayTaskBefore=TaskInfo $GatewayTask;$controlTaskBefore=TaskInfo $ControlTask
    if($gatewayTaskBefore.Action-notmatch'(?i)watch-agent-gateway\.ps1'){Fail 'gateway task action mismatch: watchdog required'};if($controlTaskBefore.Action-notmatch'(?i)Run-ControlPlaneHidden\.vbs'){Fail 'control task action mismatch'}
    $configSource=ReadText $ControlConfigPy
    if (-not (
        $configSource.Contains('diagnosis_model: str = "gpt-6-luna"') -and
        $configSource.Contains('execution_model: str = "gpt-6-luna"')
    )) { AssertCleanTarget $ControlRoot 'src/domains/control_plane/config.py' }
    foreach($port in $GatewayPorts){AssertPortSafe $port 'gateway'};AssertPortSafe $ControlPort 'control';$gatewayWasReady=GatewayHealthy;$gatewayModelsMatched=if($gatewayWasReady){GatewayModelsMatch}else{$false};$controlWasLive=(Status "$ControlBaseUrl/live")-eq200
    Snapshot $CodexConfig 'codex.config.toml';Snapshot $ControlConfigPy 'control.config.py';Snapshot $ControlAlertPy 'control.alert_policy.py';Snapshot $ControlConfig 'control_plane.toml';Snapshot (Join-Path $GatewayRoot 'core\models.yaml') 'gateway.models.yaml';Snapshot (Join-Path $GatewayRoot 'core\.run\core.pid') 'gateway.core.pid';Snapshot (Join-Path $GatewayRoot 'core\.run\agent.pid') 'gateway.agent.pid';Snapshot (Join-Path $GatewayRoot 'core\.run\responses.pid') 'gateway.responses.pid'
    $stage='config';UpdatePersistentConfig
    $stage='gateway';if(-not $gatewayWasReady -or -not $gatewayModelsMatched){$gatewayTouched=$true;TaskEnd $GatewayTask;StopGatewayProcesses;TaskEnable $GatewayTask $true;TaskRun $GatewayTask { GatewayHealthy };WaitGateway 180}elseif(-not $gatewayTaskBefore.Enabled){$gatewayTouched=$true;TaskEnable $GatewayTask $true};GatewayProbe;Say 'LLM Gateway Agent entry and routing validation passed'
    $stage='codex';CodexProbe;Say 'Codex 经 4101 Agent 入口验证通过'
    $stage='control';PythonConfigProbe;if(-not$controlWasLive-or$changed.Contains($ControlConfigPy)-or$changed.Contains($ControlAlertPy)-or$changed.Contains($ControlConfig)){$controlTouched=$true;TaskEnd $ControlTask;StopControlProcess;TaskEnable $ControlTask $true;TaskRun $ControlTask { (Status "$ControlBaseUrl/live") -eq 200 };WaitControl 120};Say 'control-plane 配置与 liveness 验证通过';Say '完成：网关任务已启用，模型统一为 gpt-6-luna';$parent=[IO.Path]::GetFullPath([IO.Path]::GetTempPath());$full=[IO.Path]::GetFullPath($tx);if($full.StartsWith($parent,[StringComparison]::OrdinalIgnoreCase)-and(Split-Path $full -Leaf).StartsWith('ratio-luna-gateway-')){Remove-Item -LiteralPath $full -Recurse -Force};exit 0
}catch{$detail=([string]$_.Exception.Message)-replace '[\r\n]+',' ';Say "失败，自动回滚（阶段=$stage，错误类型=$($_.Exception.GetType().Name)，原因=$detail）";$ok=Rollback;if($ok){Say '自动回滚完成，已恢复执行前文件与任务状态'}else{Write-Error "自动回滚未完全确认（$($rollbackErrors -join ', ')）；保留事务目录：$tx"};exit $(if($ok){1}else{2})}
