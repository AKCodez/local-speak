' Silent supervised launcher for STT.
'
' Loops: launch pythonw main.py, wait for it to exit, decide whether to
' relaunch.
'
' Exit codes from main.py:
'   0  = user-requested clean exit (tray Quit, Ctrl+Alt+Q, signal). Stop.
'   42 = self-restart requested (e.g. keyboard hooks gone unrecoverably
'        deaf and an in-process rebuild can't fix them). Loop.
'   *  = anything else (crash, OS terminate, etc.). Loop with a longer
'        backoff so a hard failure doesn't burn CPU.
'
' To prevent runaway loops if Python itself can't even start, we cap the
' total relaunches per hour. Once that cap is hit we wait a long time
' before trying again.

Option Explicit

Dim fso, scriptDir, WshShell, cmd, exitCode
Dim restartCount, windowStart, now, secsBetweenChecks

Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = scriptDir

cmd = """" & scriptDir & "\.venv\Scripts\pythonw.exe"" """ & scriptDir & "\main.py"""

restartCount = 0
windowStart = Timer  ' seconds since midnight, wraps at 86400

Do
    ' bWaitOnReturn = True so we wait for pythonw to exit and capture its
    ' exit code. Window style 0 keeps everything hidden.
    exitCode = WshShell.Run(cmd, 0, True)

    If exitCode = 0 Then
        Exit Do  ' user asked us to quit. Don't relaunch.
    End If

    ' Reset the rolling-hour window if more than 3600s have passed.
    now = Timer
    If now < windowStart Then windowStart = now  ' midnight wrap
    If (now - windowStart) > 3600 Then
        windowStart = now
        restartCount = 0
    End If

    restartCount = restartCount + 1

    If restartCount > 20 Then
        ' More than 20 relaunches in an hour means something is fundamentally
        ' wrong (Python install missing, model corrupted, etc.). Sleep a long
        ' time before trying again so we don't burn CPU.
        WScript.Sleep 30 * 60 * 1000  ' 30 minutes
        restartCount = 0
        windowStart = Timer
    ElseIf exitCode = 42 Then
        ' Requested self-restart (deaf hooks). Brief settle pause.
        WScript.Sleep 1500
    Else
        ' Unexpected exit. Back off a bit so a crash loop doesn't pin a core.
        WScript.Sleep 5000
    End If
Loop
