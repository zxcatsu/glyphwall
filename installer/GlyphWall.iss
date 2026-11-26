; Скрипт установщика GlyphWall. Собирается build_installer.bat,
; на выходе — installer\GlyphWall-Setup.exe.

#define AppName "GlyphWall"
#define AppVersion "1.0.0"
#define AppExe "GlyphWall.exe"
#define AppId "{{8F3C1A24-6B7E-4D92-9C15-2E4A7B0D5F31}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=GlyphWall
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
SetupIconFile=..\glyphwall.ico
OutputDir=.
OutputBaseFilename=GlyphWall-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
CloseApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\GlyphWall\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\GlyphWall\_internal\*"; DestDir: "{app}\_internal"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Удалить {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Запустить {#AppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  LegacyName = 'ASCIIWallpaper';

procedure KillApp();
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM GlyphWall.exe', '',
       SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM ' + LegacyName + '.exe', '',
       SW_HIDE, ewWaitUntilTerminated, Code);
end;

procedure DropRunValue();
begin
  RegDeleteValue(HKCU, RunKey, '{#AppName}');
  RegDeleteValue(HKCU, RunKey, LegacyName);
end;

function InitializeSetup(): Boolean;
begin
  KillApp();
  Result := True;
end;

function InitializeUninstall(): Boolean;
begin
  KillApp();
  DropRunValue();
  if not UninstallSilent() then
    if MsgBox('Удалить настройки и кэш видео?' + #13#10 +
              'Они лежат в профиле пользователя и занимают до нескольких ГБ.',
              mbConfirmation, MB_YESNO) = IDYES then begin
      DelTree(ExpandConstant('{userappdata}\{#AppName}'), True, True, True);
      DelTree(ExpandConstant('{localappdata}\{#AppName}'), True, True, True);
    end;
  Result := True;
end;
