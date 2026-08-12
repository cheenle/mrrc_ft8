#define MyAppName "MRRC_FT8"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "MRRC"
#define MyAppExeName "MRRC_FT8.exe"

[Setup]
AppId={{86A45855-A275-49EE-A8B1-BB0009CF1BF3}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\MRRC_FT8
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist\windows
OutputBaseFilename=MRRC_FT8-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin

[Files]
Source: "..\..\dist\windows\MRRC_FT8\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Edit Configuration"; Filename: "notepad.exe"; Parameters: """{localappdata}\MRRC-FT8\ft8.env"""
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "netsh.exe"; Parameters: "advfirewall firewall add rule name=""MRRC_FT8 Web"" dir=in action=allow protocol=TCP localport=8000"; Flags: runhidden; StatusMsg: "Opening firewall port 8000..."
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
