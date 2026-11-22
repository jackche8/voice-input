; Inno Setup script for Ollama Voice Input (Windows x64, per-user install, single executable --onedir)
; Pass /DMyAppVersion=<version> from packaging\windows\build.ps1 (single source: _version.py).

#ifndef MyAppVersion
  #error "Pass /DMyAppVersion=<version> (read from _version.py)"
#endif

#define MyAppName "Ollama Voice Input"
#define MyAppPublisher "Ollama Voice Input Maintainers"
#define MyAppURL "https://github.com/jackche8/voice-input"
#define MyAppExeName "ollama-voice-input.exe"

[Setup]
AppId={{8C9E5A31-6D4B-4E2A-9B7F-4A1E2F3C5D6E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={localappdata}\Programs\Ollama Voice Input
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
LicenseFile=..\..\LICENSE
InfoBeforeFile=..\..\COMMERCIAL.md
OutputDir=..\..\dist
OutputBaseFilename=Ollama-Voice-Input-{#MyAppVersion}-windows-x64-setup
SetupIconFile=..\..\resources\ollama-voice-input.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/normal
SolidCompression=no
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\dist\ollama-voice-input\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\COMMERCIAL.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\THIRD-PARTY-NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; AppUserModelID: "io.github.jackche8.ollama-voice-input"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; AppUserModelID: "io.github.jackche8.ollama-voice-input"; Tasks: desktopicon

[Registry]
; Clean per-user autostart entry written by _platform/windows.py on uninstall without touching user data directories
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "OllamaVoiceInput"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
