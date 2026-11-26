; Inno Setup — instalador Windows do Jataí Manager
; Compile com: ISCC manager\installer.iss  (a partir da raiz do repo)
#define AppName "Jataí Manager"
#define AppVersion "0.1.0"
#define AppExe "jatai_manager.exe"

[Setup]
AppId={{7B2E4C1A-9F3D-4E8B-A5C6-1D2E3F4A5B6C}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Jataí
DefaultGroupName=Jataí
OutputDir=dist-windows
OutputBaseFilename=Jataí-Manager-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#AppExe}

[Files]
Source: "dist-windows\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Desinstalar {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Criar ícone na Área de Trabalho"; GroupDescription: "Ícones adicionais:"

[Run]
Filename: "{app}\{#AppExe}"; Description: "Abrir o Jataí Manager"; Flags: nowait postinstall skipifsilent
