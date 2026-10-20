; Inno Setup script (https://jrsoftware.org/isinfo.php, free) - makes "AstroFlow-Setup-1.0.0.exe"
; from the portable folder. First run:  powershell -ExecutionPolicy Bypass -File tools\build_portable.ps1
; then open this file in Inno Setup and press Compile. The result appears in dist\.

#define AppVersion "1.0.0"

[Setup]
AppId={{6F1B7E4A-AF10-4C7E-9A63-2A57C0F1A5F1}
AppName=AstroFlow
AppVersion={#AppVersion}
AppPublisher=AstroFlow
DefaultDirName={localappdata}\Programs\AstroFlow
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=AstroFlow-Setup-{#AppVersion}
SetupIconFile=astroflow.ico
UninstallDisplayIcon={app}\windows\astroflow.ico
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\AstroFlow-Portable\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\AstroFlow"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\AstroFlow.vbs"""; WorkingDir: "{app}"; IconFilename: "{app}\windows\astroflow.ico"
Name: "{autodesktop}\AstroFlow"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\AstroFlow.vbs"""; WorkingDir: "{app}"; IconFilename: "{app}\windows\astroflow.ico"; Tasks: desktopicon

[Run]
Filename: "{sys}\wscript.exe"; Parameters: """{app}\AstroFlow.vbs"""; Description: "Start AstroFlow now"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; Only the program folder's byte-code caches. Your settings (%APPDATA%\AstroFlow) and your pictures
; (Documents\AstroFlow) are never removed by the uninstaller.
Type: filesandordirs; Name: "{app}\astroflow\__pycache__"
