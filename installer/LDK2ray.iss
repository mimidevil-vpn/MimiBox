; ============================================================
;  MimiBox — установщик (Inno Setup 6)
;  Ставится для текущего пользователя, БЕЗ прав администратора.
;  Собрать: installer\build_installer.bat  ->  installer\Output\MimiBox-Setup.exe
; ============================================================

#define MyAppName      "MimiBox"
#define MyAppVersion   "4.0.0"
#define MyAppPublisher "mimidevil"
#define MyAppURL       "https://t.me/mimidevil"
#define MyAppExeName   "LDK2ray.exe"

[Setup]
AppId={{7F3C2A10-1D4B-49E1-9C3A-9D2B7A5E0001}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
; --- установка для пользователя, без UAC ---
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf64}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
UninstallDisplayIcon={app}\\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
OutputDir=Output
OutputBaseFilename=MimiBox-Setup
SetupIconFile=..\ui\app.ico

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; onedir-сборка: забираем всю папку dist\\LDK2ray (exe + _internal)
Source: "..\\dist\\LDK2ray\\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; runtime-зависимости: xray core, tun2socks, wintun, geo-базы
Source: "..\\releases\\LDK2ray\\xray.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\\releases\\LDK2ray\\tun2socks.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\\releases\\LDK2ray\\wintun.dll"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\\releases\\LDK2ray\\geoip.dat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\\releases\\LDK2ray\\geosite.dat"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\\data"
; Настройки и список серверов в {userappdata}\\LDK2ray НЕ трогаем: обновление
; часто идёт через «удалить и поставить заново», и стирать их — значит терять
; подписку пользователя.