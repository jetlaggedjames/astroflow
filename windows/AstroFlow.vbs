' Starts AstroFlow without a black console window (used by the desktop and Start menu shortcuts).
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
If fso.GetFileName(here) = "windows" Then here = fso.GetParentFolderName(here)
py = here & "\python\pythonw.exe"
If Not fso.FileExists(py) Then
  MsgBox "AstroFlow is not installed in this folder yet (no python\pythonw.exe in " & here & ")." & vbCrLf & _
         "Please double-click 'Install AstroFlow.bat' first.", 16, "AstroFlow"
  WScript.Quit 1
End If
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = here
sh.Run """" & py & """ """ & here & "\run.py""", 0, False
