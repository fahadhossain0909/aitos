#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
[Setup]
AppId={{E7A47CAC-580A-4267-A479-6B3BE210EA17}
AppName=AITOS Read-only Console
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\AITOS
DefaultGroupName=AITOS
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\artifacts\windows
OutputBaseFilename=AITOS-Console-{#AppVersion}-Windows-x64
Compression=lzma2
SolidCompression=yes
#ifdef SignedRelease
SignedUninstaller=yes
SignTool=aitos
#endif
UninstallDisplayIcon={app}\AITOS-Console.exe
CloseApplications=yes
[Files]
Source: "..\..\dist\AITOS-Console\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\AITOS Console"; Filename: "{app}\AITOS-Console.exe"
[Run]
Filename: "{app}\AITOS-Console.exe"; Description: "Open AITOS read-only console"; Flags: nowait postinstall skipifsilent
; Never delete vault credentials or user trading databases during uninstall.
