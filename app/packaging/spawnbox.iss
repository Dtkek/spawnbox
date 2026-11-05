; Установщик Spawnbox. Ставится для одного пользователя, поэтому права администратора не нужны.
; Миры лежат отдельно, в %LOCALAPPDATA%\Spawnbox: обновление и удаление их не трогают.
#ifndef MyVersion
  #define MyVersion "1.0"
#endif

[Setup]
AppId={{6F2C1E7A-3B44-4D8E-9A61-5E0C7D2B8F13}
AppName=Spawnbox
AppVersion={#MyVersion}
AppVerName=Spawnbox {#MyVersion}
DefaultDirName={localappdata}\Programs\Spawnbox
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=SpawnboxSetup
SetupIconFile=..\spawnbox\static\icon.ico
UninstallDisplayIcon={app}\spawnbox.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Закрывать приложение насильно нельзя: сервер должен успеть сохранить мир, см. [Code]
CloseApplications=no
RestartApplications=no
; Иначе обновление снова включило бы автозапуск, выключенный в настройках
UsePreviousTasks=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Значок на рабочем столе"
Name: "autostart"; Description: "Запускать вместе с Windows, чтобы друзья могли включать сервер"; Flags: unchecked

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\..\dist\spawnbox\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Spawnbox"; Filename: "{app}\spawnbox.exe"
Name: "{userdesktop}\Spawnbox"; Filename: "{app}\spawnbox.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Spawnbox"; ValueData: """{app}\spawnbox.exe"" --tray"; Tasks: autostart
; Автозапуск можно включить и в настройках приложения, поэтому при удалении он убирается всегда
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "Spawnbox"; Flags: dontcreatekey uninsdeletevalue

[Run]
Filename: "{app}\spawnbox.exe"; Description: "Запустить Spawnbox"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\spawnbox.exe"; Parameters: "--quit"; Flags: runhidden waituntilterminated; RunOnceId: "QuitSpawnbox"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\spawnbox.exe')) then
    Exec(ExpandConstant('{app}\spawnbox.exe'), '--quit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;
