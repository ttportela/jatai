# Lelia Manager

App desktop leve (somente stdlib: tkinter) que gerencia a instalação e
execução do Lelia. É congelado com PyInstaller num binário pequeno
(~15 MB) — **sem dependências externas**, porque todo o trabalho pesado
(baixar `lelia` + deps) é feito por `pip` dentro de um venv criado em
`~/.lelia/venv`.

## O que o app faz

- **Verifica Python ≥ 3.11** no sistema (`python3.13/3.12/3.11/3.13`,
  `py -3.x` no Windows).
- **Instala/atualiza** o pacote `lelia` (+ FastAPI, SQLAlchemy, etc.)
  num venv isolado via `pip install --upgrade`.
- **Iniciar/Parar** o servidor: subprocesso
  `python -m lelia.cli start --host … --port …` com `LELIA_HOME`
  apontando para a pasta de trabalho configurada.
- **Configura**: pasta de trabalho, porta, host e fonte do pacote
  (PyPI `lelia` ou `lelia @ git+https://…` para desenvolvimento).
- **Verifica atualizações** consultando `pypi.org/pypi/lelia/json`.
- **Notificações nativas** best-effort: `osascript` (macOS),
  `notify-send` (Linux), toast PowerShell (Windows).

Config persiste em `~/.lelia/manager.json`. O servidor usa
`LELIA_HOME=<pasta de trabalho>` — já suportado por
`lelia/config/settings.py`.

## Rodar sem empacotar

```bash
python3 manager/lelia_manager.py   # requer Python com tkinter
```

> O Python precisa ter **Tk** (`_tkinter`). Instaladores python.org já
> incluem; no Homebrew use `brew install python-tk@3.12` (o `python3`
> do projeto `.venv` pode não ter).

## Build por SO

Pré-requisito comum: `pip install pyinstaller` num Python **com
tkinter** (o build embute o Tk no binário).

### macOS → `.app` + `.dmg`

```bash
manager/build-mac.sh
```

Gera `dist/Lelia Manager.app` e `dist/Lelia-Manager-<data>.dmg`.
Para distribuir sem aviso do Gatekeeper, assine e notarize antes
(`codesign --deep --options runtime` + `xcrun notarytool`) — requer
certificado Apple Developer.

### Windows → `.exe` + instalador

```powershell
.\manager\build-win.ps1
```

Gera `dist-windows\lelia_manager.exe` e, se o Inno Setup 6 estiver
instalado, `Lelia-Manager-Setup-<ver>.exe` (instalador com entrada no
menu Iniciar, desinstalador e ícone opcional na Área de Trabalho).

### Linux → `.deb`

```bash
manager/build-linux.sh
```

Gera `dist/lelia-manager` (binário) e `dist/lelia-manager_<ver>_<arch>.deb`
(com `.desktop` e ícone). Requer `dpkg-deb`.

## Notas

- O app **não congela** o Lelia — ele instala via pip em runtime.
  Atualizar o Lelia = clicar em "Instalar / Atualizar", sem rebuild.
- Playwright (browsers das fontes anti-bot) é instalado pelo próprio
  Lelia sob demanda; nada disso vai no binário do manager.
- Se `lelia` ainda não estiver no PyPI, use o campo "Pacote" com
  `lelia @ git+https://github.com/<voce>/lelia.git`.
