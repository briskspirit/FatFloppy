; Inno Setup Script for FatFloppy Windows Installer
; Documentation: https://jrsoftware.org/ishelp/

#ifndef MyAppVersion
  #define MyAppVersion "0.2.0"
#endif

#define MyAppName "FatFloppy"
#define MyAppPublisher "FatFloppy"
#define MyAppURL "https://github.com/briskspirit/FatFloppy"
#define MyAppExeName "FatFloppy.exe"
#define MyAppAssocName "Floppy Disk Image"
#define MyAppAssocKey StringChange(MyAppAssocName, " ", "") + ".img"

[Setup]
; Unique application identifier - do not change after release
AppId={{8B5E4A3F-9D2C-4E1B-A8F7-6C3D5E2B1A09}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=admin
; Output settings
OutputDir=..\..\dist
OutputBaseFilename={#MyAppName}-{#MyAppVersion}-Windows-Setup
; Compression
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
; Visual settings
SetupIconFile=..\..\assets\icons\fatfloppy_icon.ico
WizardStyle=modern
WizardSizePercent=100
; Uninstaller
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
; Minimum Windows version (Windows 10)
MinVersion=10.0
; Architecture
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "associateimg"; Description: "Associate .img files with {#MyAppName}"; GroupDescription: "File associations:"; Flags: unchecked
Name: "associateima"; Description: "Associate .ima files with {#MyAppName}"; GroupDescription: "File associations:"; Flags: unchecked
Name: "associateimd"; Description: "Associate .imd files with {#MyAppName}"; GroupDescription: "File associations:"; Flags: unchecked

[Files]
; Main application bundle (onedir output from PyInstaller)
Source: "..\..\dist\FatFloppy\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Start menu shortcuts
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
; Desktop shortcut (optional, unchecked by default)
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; File associations for .img (optional)
Root: HKA; Subkey: "Software\Classes\.img\OpenWithProgids"; ValueType: string; ValueName: "{#MyAppAssocKey}"; ValueData: ""; Flags: uninsdeletevalue; Tasks: associateimg
Root: HKA; Subkey: "Software\Classes\{#MyAppAssocKey}"; ValueType: string; ValueName: ""; ValueData: "{#MyAppAssocName}"; Flags: uninsdeletekey; Tasks: associateimg
Root: HKA; Subkey: "Software\Classes\{#MyAppAssocKey}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Tasks: associateimg
Root: HKA; Subkey: "Software\Classes\{#MyAppAssocKey}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: associateimg

; File associations for .ima (optional)
Root: HKA; Subkey: "Software\Classes\.ima\OpenWithProgids"; ValueType: string; ValueName: "FatFloppyDiskImage.ima"; ValueData: ""; Flags: uninsdeletevalue; Tasks: associateima
Root: HKA; Subkey: "Software\Classes\FatFloppyDiskImage.ima"; ValueType: string; ValueName: ""; ValueData: "Floppy Disk Image"; Flags: uninsdeletekey; Tasks: associateima
Root: HKA; Subkey: "Software\Classes\FatFloppyDiskImage.ima\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Tasks: associateima
Root: HKA; Subkey: "Software\Classes\FatFloppyDiskImage.ima\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: associateima

; File associations for .imd (optional)
Root: HKA; Subkey: "Software\Classes\.imd\OpenWithProgids"; ValueType: string; ValueName: "FatFloppyImageDisk.imd"; ValueData: ""; Flags: uninsdeletevalue; Tasks: associateimd
Root: HKA; Subkey: "Software\Classes\FatFloppyImageDisk.imd"; ValueType: string; ValueName: ""; ValueData: "ImageDisk Image"; Flags: uninsdeletekey; Tasks: associateimd
Root: HKA; Subkey: "Software\Classes\FatFloppyImageDisk.imd\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Tasks: associateimd
Root: HKA; Subkey: "Software\Classes\FatFloppyImageDisk.imd\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: associateimd

[Run]
; Option to launch application after installation
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
