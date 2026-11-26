# Jataí Manager

App desktop leve (somente stdlib: tkinter) que gerencia a instalação e
execução do Jataí. É congelado com PyInstaller num binário pequeno
(~15 MB) — **sem dependências externas**, porque todo o trabalho pesado
(baixar `jatai` + deps) é feito por `pip` dentro de um venv criado em
`~/.jatai/venv`.

## O que o app faz

- **Verifica Python ≥ 3.11** no sistema (`python3.13/3.12/3.11/3.13`,
  `py -3.x` no Windows).
- **Instala/atualiza** o pacote `jatai` (+ FastAPI, SQLAlchemy, etc.)
  num venv isolado via `pip install --upgrade`.
- **Iniciar/Parar** o servidor: subprocesso
  `python -m jatai.cli start --host … --port …` com `JATAI_HOME`
  apontando para a pasta de trabalho configurada.
- **Configura**: pasta de trabalho, porta, host e fonte do pacote
  (PyPI `jatai` ou `jatai @ git+https://…` para desenvolvimento).
- **Verifica atualizações** consultando `pypi.org/pypi/jatai/json`.
- **Notificações nativas** best-effort: `osascript` (macOS),
  `notify-send` (Linux), toast PowerShell (Windows).

Config persiste em `~/.jatai/manager.json`. O servidor usa
`JATAI_HOME=<pasta de trabalho>` — já suportado por
`jatai/config/settings.py`.

## Rodar sem empacotar

```bash
python3 manager/jatai_manager.py   # requer Python com tkinter
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

Gera `dist/Jataí Manager.app` e `dist/Jataí-Manager-<data>.dmg`.
Para distribuir sem aviso do Gatekeeper, assine e notarize antes
(`codesign --deep --options runtime` + `xcrun notarytool`) — requer
certificado Apple Developer.

### Windows → `.exe` + instalador

```powershell
.\manager\build-win.ps1
```

Gera `dist-windows\jatai_manager.exe` e, se o Inno Setup 6 estiver
instalado, `Jataí-Manager-Setup-<ver>.exe` (instalador com entrada no
menu Iniciar, desinstalador e ícone opcional na Área de Trabalho).

### Linux → `.deb`

```bash
manager/build-linux.sh
```

Gera `dist/jatai-manager` (binário) e `dist/jatai-manager_<ver>_<arch>.deb`
(com `.desktop` e ícone). Requer `dpkg-deb`.

## Notas

- O app **não congela** o Jataí — ele instala via pip em runtime.
  Atualizar o Jataí = clicar em "Instalar / Atualizar", sem rebuild.
- Playwright (browsers das fontes anti-bot) é instalado pelo próprio
  Jataí sob demanda; nada disso vai no binário do manager.
- Se `jatai` ainda não estiver no PyPI, use o campo "Pacote" com
  `jatai @ git+https://github.com/<voce>/jatai.git`.
