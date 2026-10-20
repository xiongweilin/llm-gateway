Option Explicit

Dim shell, pwsh, script, command, exitCode
Set shell = CreateObject("WScript.Shell")

pwsh = shell.ExpandEnvironmentStrings("%USERPROFILE%\scoop\apps\pwsh\current\pwsh.exe")
script = "D:\agent\llm-gateway\scripts\watch-agent-gateway.ps1"

command = """" & pwsh & """" & _
    " -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " & _
    """" & script & """"

exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode
