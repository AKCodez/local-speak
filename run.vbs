' Silent launcher for STT. Double-click or invoke via wscript.exe for
' autostart. Runs pythonw.exe main.py with no visible console and no
' taskbar entry.
Option Explicit

Dim fso, scriptDir, WshShell, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = scriptDir

cmd = """" & scriptDir & "\.venv\Scripts\pythonw.exe"" """ & scriptDir & "\main.py"""
' 0 = hidden window, False = don't wait for process to exit
WshShell.Run cmd, 0, False
