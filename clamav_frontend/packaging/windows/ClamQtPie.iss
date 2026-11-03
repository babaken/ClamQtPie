; ClamQtPie の Windows インストーラ（Inno Setup 6。サービス化・Web API設計書 11章。段階 12-4d）
;
; build_release.py が ISCC に /DAppVersion=... /DSourceDir=<dist\ClamQtPie> を渡してビルドする。
; データの置き場所の確認・グループ・サービスの登録は、ここには書かず clamqtpie-ctl setup が行う（テストできるように）。
; - 導入: %ProgramFiles%\ClamQtPie に置き、clamqtpie-ctl setup install（データの置き場所の確認と退避、
;   ClamQtPie Users グループ、導入した利用者の登録、サービスの登録・回復動作・開始）
; - 更新: サービスを止め、動いているトレイGUI・昇格ヘルパーを終わらせて差し替える。終わったら元の利用者の権限で
;   トレイGUI を起動し直す（トレイGUI はほかのセッションの昇格していないプロセスのため、Restart Manager では
;   閉じられない（Permission Denied + Session Mismatch。Windows の実機で発見）。そのため CloseApplications=no）
; - 旧版（ポータブル）の検出: 自動起動（HKCU\...\Run の ClamQtPie）がほかの場所を指していれば、移行を勧める
; - 削除: サービスを止めて登録を消す。データ（隔離ファイルを含む）は確かめて、既定は残す

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\ClamQtPie"
#endif

[Setup]
AppId={{3A4B03CD-0338-4B7E-A7D6-278AB04E7512}
AppName=ClamQtPie
AppVersion={#AppVersion}
AppVerName=ClamQtPie {#AppVersion}
AppPublisher=ClamQtPie
AppPublisherURL=https://github.com/babaken/ClamQtPie
DefaultDirName={autopf}\ClamQtPie
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputBaseFilename=ClamQtPie-{#AppVersion}-setup
SetupIconFile=..\..\icons\clamQtPie.ico
UninstallDisplayIcon={app}\ClamQtPie.exe
UninstallDisplayName=ClamQtPie
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no
ChangesEnvironment=yes
; HKCU（自動起動・旧版の検出）は、インストーラを動かした管理者の利用者のもの。導入した本人が管理者として
; 動かす前提のため、警告は出さない（別の管理者の資格情報で昇格したときは、その管理者の HKCU になる）
UsedUserAreasWarning=no

[Languages]
Name: "ja"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "autostart"; Description: "サインインしたときにトレイGUI を起動する"
Name: "path"; Description: "管理用コマンド（clamqtpie-ctl）を PATH に追加する"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\ClamQtPie"; Filename: "{app}\ClamQtPie.exe"; Comment: "ClamAV を使ったウイルス対策（トレイGUI）"

[Registry]
; 自動起動（トレイGUI の設定画面と同じ値。パスに空白があるため引用符で囲む）
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "ClamQtPie"; \
  ValueData: """{app}\ClamQtPie.exe"""; Tasks: autostart; Flags: uninsdeletevalue

[UninstallDelete]
; 前の削除・更新で使用中のため消せなかったファイルがあると、そのフォルダは次の導入で記録されず残る（Windows の実機で発見）。
; _internal は ClamQtPie だけのものなので、中身ごと消す
Type: filesandordirs; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"

[Run]
Filename: "{app}\ClamQtPie.exe"; Description: "ClamQtPie（トレイGUI）を起動する"; \
  Flags: postinstall nowait skipifsilent runasoriginaluser
; 静かな更新（/SILENT）でも、終わらせたトレイGUI を起動し直す
Filename: "{app}\ClamQtPie.exe"; Flags: nowait runasoriginaluser; Check: WizardSilent

[Code]
const
  EnvironmentKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';

var
  LegacyDir: String;
  PurgeData: Boolean;

function CtlPath(): String;
begin
  Result := ExpandConstant('{app}\clamqtpie-ctl.exe');
end;

function RunCtl(const Params: String; var ResultCode: Integer): Boolean;
begin
  Result := Exec(CtlPath(), Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

{ clamqtpie-ctl を実行し、出力（失敗の理由）を Output に入れる。日本語の出力をそのまま読めるよう CP932 で書かせる }
function RunCtlWithOutput(const Params: String; var ResultCode: Integer; var Output: String): Boolean;
var
  TmpFile: String;
  Text: AnsiString;
begin
  TmpFile := ExpandConstant('{tmp}\ctl-output.txt');
  DeleteFile(TmpFile);
  Result := Exec(ExpandConstant('{cmd}'), '/C "set PYTHONIOENCODING=cp932&& "' + CtlPath() + '" ' + Params +
                 ' > "' + TmpFile + '" 2>&1"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Output := '';
  if LoadStringFromFile(TmpFile, Text) then
    Output := Trim(String(Text));
end;

{ 旧版（ポータブル）: 自動起動の値が、導入先以外の ClamQtPie.exe を指していれば、そのフォルダ }
function FindLegacyDir(): String;
var
  Value: String;
begin
  Result := '';
  if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'ClamQtPie', Value) then
  begin
    StringChangeEx(Value, '"', '', True);
    Value := Trim(Value);
    if (CompareText(ExtractFileName(Value), 'ClamQtPie.exe') = 0) and
       (CompareText(ExtractFileDir(Value), ExpandConstant('{app}')) <> 0) and DirExists(ExtractFileDir(Value)) then
      Result := ExtractFileDir(Value);
  end;
end;

{ sc query の出力（状態の名前 STOPPED・RUNNING は OS の言語によらず英字。登録が無ければ 1060） }
function ServiceState(): String;
var
  TmpFile: String;
  Output: AnsiString;
  ResultCode: Integer;
begin
  Result := '';
  TmpFile := ExpandConstant('{tmp}\sc-query.txt');
  DeleteFile(TmpFile);
  Exec(ExpandConstant('{cmd}'), '/C ""' + ExpandConstant('{sys}\sc.exe') + '" query ClamQtPie > "' + TmpFile + '""',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if LoadStringFromFile(TmpFile, Output) then
    Result := String(Output);
end;

{ トレイGUI・昇格ヘルパー・通知のクリックのプログラムを終わらせる（ファイルを差し替える・消す前に） }
procedure CloseClamQtPieApps();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM ClamQtPie.exe /IM ClamQtPieElevate.exe /IM ClamQtPieActivate.exe',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

{ サービスが STOPPED になった後も、プロセスが少しの間ファイルを使っていることがある（削除・差し替えで使用中の
  ファイルが残った。Windows の実機で発見）。終わるまで待ち、終わらなければ強制的に終わらせる }
procedure WaitServiceProcessExit();
var
  ResultCode, I: Integer;
begin
  for I := 1 to 20 do
  begin
    if not Exec(ExpandConstant('{cmd}'), '/C tasklist /FI "IMAGENAME eq ClamQtPieService.exe" /NH | find /I "ClamQtPieService.exe" >nul',
                '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
      Exit;
    Sleep(500);
  end;
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM ClamQtPieService.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1000);
end;

{ 更新の前にサービスを止め、止まるまで待つ。止まらなければ導入をやめる }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode, I: Integer;
  State: String;
begin
  Result := '';
  LegacyDir := FindLegacyDir();
  CloseClamQtPieApps();
  State := ServiceState();
  if (State = '') or (Pos('1060', State) > 0) then
    Exit;  { まだ導入されていない }
  Exec(ExpandConstant('{sys}\sc.exe'), 'stop ClamQtPie', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  for I := 1 to 90 do
  begin
    State := ServiceState();
    if Pos('STOPPED', State) > 0 then
    begin
      WaitServiceProcessExit();
      Exit;
    end;
    Sleep(1000);
  end;
  Result := 'ClamQtPie サービスが止まりません。しばらく待ってから、もう一度インストーラを実行してください。';
end;

procedure AddToPath();
var
  Path, Dir: String;
begin
  Dir := ExpandConstant('{app}');
  if not RegQueryStringValue(HKLM, EnvironmentKey, 'Path', Path) then
    Path := '';
  if Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Path) + ';') = 0 then
  begin
    if (Path <> '') and (Copy(Path, Length(Path), 1) <> ';') then
      Path := Path + ';';
    RegWriteExpandStringValue(HKLM, EnvironmentKey, 'Path', Path + Dir);
  end;
end;

procedure RemoveFromPath();
var
  Path, Dir: String;
  P: Integer;
begin
  Dir := ExpandConstant('{app}');
  if not RegQueryStringValue(HKLM, EnvironmentKey, 'Path', Path) then
    Exit;
  Path := ';' + Path + ';';
  P := Pos(';' + Uppercase(Dir) + ';', Uppercase(Path));
  if P > 0 then
  begin
    Delete(Path, P, Length(Dir) + 1);
    Path := Copy(Path, 2, Length(Path) - 2);
    RegWriteExpandStringValue(HKLM, EnvironmentKey, 'Path', Path);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Output: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  if WizardIsTaskSelected('path') then
    AddToPath();
  WizardForm.StatusLabel.Caption := 'サービスを登録しています...';
  if not RunCtlWithOutput('setup install --user "' + ExpandConstant('{username}') + '"', ResultCode, Output) or
     (ResultCode <> 0) then
    { 行の先頭に # を置かない（Inno Setup のプリプロセッサの命令とみなされる） }
    MsgBox('サービスの登録に失敗しました（終了コード ' + IntToStr(ResultCode) + '）。' + #13#10#13#10 + Output + #13#10#13#10 +
           '原因を取り除いてから、管理者のコマンドプロンプトで "' + CtlPath() + '" setup install を実行してください。',
           mbError, MB_OK);
  if (LegacyDir <> '') and not WizardSilent() then
  begin
    if MsgBox('旧版（ポータブル）の ClamQtPie を検出しました:' + #13#10 + LegacyDir + #13#10#13#10 +
              '設定・隔離ファイル・スケジュール・ログを、新しいサービスへ移しますか？' + #13#10 +
              '（旧版を終了してから「はい」を押してください。旧版のログの日付を保つため、移行は導入した日のうちに行ってください）',
              mbConfirmation, MB_YESNO) = IDYES then
    begin
      WizardForm.StatusLabel.Caption := '旧版のデータを移しています...';
      if not RunCtl('migrate --from "' + LegacyDir + '" --yes --restart', ResultCode) or (ResultCode <> 0) then
        MsgBox('旧版のデータを移せませんでした（終了コード ' + IntToStr(ResultCode) + '）。' + #13#10 +
               '管理者のコマンドプロンプトで clamqtpie-ctl migrate --from "' + LegacyDir + '" を実行してください。',
               mbError, MB_OK)
      else
        MsgBox('旧版のデータを移しました。旧版のフォルダは、動作を確かめてから削除してください。', mbInformation, MB_OK);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  Params: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    { 削除してよいかの確認（Inno Setup の標準の確認）の後に、データを消すかを尋ねる。静かな削除では残す }
    PurgeData := False;
    if not UninstallSilent() then
      PurgeData := MsgBox('設定・データ・ログ（隔離したファイルを含む）も削除しますか？' + #13#10 +
                          '「いいえ」なら残します（入れ直したときに引き継ぎます）。', mbConfirmation,
                          MB_YESNO or MB_DEFBUTTON2) = IDYES;
    CloseClamQtPieApps();
    Params := 'setup uninstall';
    if PurgeData then
      Params := Params + ' --purge';
    RunCtl(Params, ResultCode);
    WaitServiceProcessExit();
    RemoveFromPath();
  end;
end;
