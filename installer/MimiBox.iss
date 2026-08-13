; ============================================================
;  MimiBox — установщик (Inno Setup 6)
;  Ставится для текущего пользователя, БЕЗ прав администратора.
;  Собрать: installer\build_installer.bat  ->  installer\Output\MimiBox-Setup.exe
; ============================================================

#define MyAppName      "MimiBox"
#define MyAppVersion   "4.0.0"
#define MyAppPublisher "mimidevil"
#define MyAppURL       "https://t.me/mimidevil"
#define MyAppExeName   "MimiBox.exe"

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
; onedir-сборка: забираем всю папку dist\\MimiBox (exe + _internal)
Source: "..\\dist\\MimiBox\\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; runtime-зависимости: xray core, tun2socks, wintun, geo-базы
Source: "..\\releases\\MimiBox\\xray.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\\releases\\MimiBox\\tun2socks.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\\releases\\MimiBox\\wintun.dll"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\\releases\\MimiBox\\geoip.dat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\\releases\\MimiBox\\geosite.dat"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\\data"
; Старый exe с прежним именем (LDK2ray.exe) может остаться от обновления.
Type: files; Name: "{app}\\LDK2ray.exe"
; Настройки и список серверов в {userappdata}\\MimiBox НЕ трогаем: обновление
; часто идёт через «удалить и поставить заново», и стирать их — значит терять
; подписку пользователя.

[Code]
// Перед заменой файлов аккуратно закрываем запущенный MimiBox: шлём guard-окну
// (MimiBoxShutdownGuard) служебное сообщение WM_MIMIBOX_QUIT. Приложение снимает
// системный прокси и выходит штатно, поэтому браузеры не остаются без интернета,
// пока идёт установка, и файлы не заняты.
const
  WM_MIMIBOX_QUIT = $8001;
  GRACEFUL_WAIT_MS = 15000;

function FindWindowW(ClassName, WindowName: string): LongInt;
  external 'FindWindowW@user32.dll stdcall';
function PostMessageW(hWnd: LongInt; Msg: Cardinal; wParam, lParam: LongInt): Boolean;
  external 'PostMessageW@user32.dll stdcall';
function GetWindowThreadProcessId(hWnd: LongInt; var ProcessId: Cardinal): Cardinal;
  external 'GetWindowThreadProcessId@user32.dll stdcall';
function OpenProcess(DesiredAccess: Cardinal; bInheritHandle: Boolean; ProcessId: Cardinal): LongInt;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(hHandle: LongInt; Milliseconds: Cardinal): Cardinal;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(hObject: LongInt): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  GuardHwnd: LongInt;
  ProcId: Cardinal;
  hProc: LongInt;
begin
  Result := '';
  NeedsRestart := False;
  GuardHwnd := FindWindowW('MimiBoxShutdownGuard', '');
  if GuardHwnd = 0 then Exit;

  if not PostMessageW(GuardHwnd, WM_MIMIBOX_QUIT, 0, 0) then Exit;

  // ждём, пока приложение снимет прокси и выйдет из процесса
  GetWindowThreadProcessId(GuardHwnd, ProcId);
  if ProcId = 0 then Exit;
  hProc := OpenProcess($00100000, False, ProcId);  // SYNCHRONIZE
  if hProc = 0 then Exit;
  WaitForSingleObject(hProc, GRACEFUL_WAIT_MS);
  CloseHandle(hProc);
end;