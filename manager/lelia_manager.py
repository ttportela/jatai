#!/usr/bin/env python3
"""Lelia Manager — app desktop leve (stdlib only) que gerencia a
instalação e execução do Lelia.

Funções:
- verifica Python >= 3.11 no sistema
- cria venv e instala o pacote `lelia` (+ dependências) via pip
- inicia/para o servidor uvicorn como subprocesso
- configura pasta de trabalho (LELIA_HOME), porta e fonte do pacote
- notificações nativas best-effort (osascript/notify-send/powershell)

Empacotar com PyInstaller --windowed (ver manager/README.md).
"""

from __future__ import annotations

import json
import os
import platform
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Lelia Manager"
PKG_NAME = "lelia"  # nome no PyPI / importlib.metadata
MIN_PY = (3, 11)
CONFIG_PATH = Path.home() / ".lelia" / "manager.json"
IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"


def _resource(name: str) -> Path:
    """Resolve asset empacotado: PyInstaller (_MEIPASS) ou ao lado do script."""
    base = getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
    return Path(base) / name
IS_LINUX = platform.system() == "Linux"

# aparência próxima do sistema
if IS_MAC:
    FONT = ("Helvetica Neue", 13)
    FONT_SMALL = ("Helvetica Neue", 11)
    BG = "#f5f5f7"
    CARD = "#ffffff"
    ACCENT = "#007aff"
    MUTED = "#86868b"
elif IS_WIN:
    FONT = ("Segoe UI", 10)
    FONT_SMALL = ("Segoe UI", 9)
    BG = "#f3f3f3"
    CARD = "#ffffff"
    ACCENT = "#0067c0"
    MUTED = "#616161"
else:
    FONT = ("DejaVu Sans", 10)
    FONT_SMALL = ("DejaVu Sans", 9)
    BG = "#f6f6f6"
    CARD = "#ffffff"
    ACCENT = "#3584e4"
    MUTED = "#777777"

DEFAULTS = {
    "workdir": str(Path.home() / ".lelia"),
    "port": 8000,
    "package": "lelia",
    "host": "127.0.0.1",
}


# ---------------------------------------------------------------- config

def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------- helpers

def venv_dir() -> Path:
    return Path.home() / ".lelia" / "venv"


def venv_python() -> Path:
    if IS_WIN:
        return venv_dir() / "Scripts" / "python.exe"
    return venv_dir() / "bin" / "python"


def lelia_script() -> Path:
    if IS_WIN:
        return venv_dir() / "Scripts" / "lelia.exe"
    return venv_dir() / "bin" / "lelia"


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def find_python() -> str | None:
    """Localiza um Python >= MIN_PY no sistema."""
    candidates = []
    if IS_WIN:
        for v in ("3.13", "3.12", "3.11"):
            candidates.append(["py", f"-{v}"])
        candidates += [["python"], ["python3"]]
    else:
        for name in ("python3.13", "python3.12", "python3.11", "python3"):
            if shutil.which(name):
                candidates.append([name])
    for cmd in candidates:
        exe = shutil.which(cmd[0])
        if not exe:
            continue
        try:
            out = _run(cmd + ["-c",
                              "import sys;print('.'.join(map(str,sys.version_info[:2])))"],
                       timeout=15).stdout.strip()
            major, minor = (int(p) for p in out.split(".")[:2])
            if (major, minor) >= MIN_PY:
                return cmd[0] if len(cmd) == 1 else " ".join(cmd)
        except Exception:
            continue
    return None


def _notify(title: str, msg: str) -> None:
    """Notificação nativa best-effort; nunca falha."""
    try:
        if IS_MAC:
            subprocess.Popen(["osascript", "-e",
                              f'display notification "{msg}" with title "{title}"'])
        elif IS_WIN:
            ps = (f"[Windows.UI.Notifications.ToastNotificationManager,"
                  f"Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null;"
                  f"$n=[Windows.UI.Notifications.ToastNotification]::new("
                  f"[Windows.Data.Xml.Dom.XmlDocument]::new());"
                  f"$x=[Windows.Data.Xml.Dom.XmlDocument]::new();"
                  f"$x.LoadXml('<toast><visual><binding template=\"ToastGeneric\">"
                  f"<text>{title}</text><text>{msg}</text></binding></visual></toast>');"
                  f"$n.Content=$x;"
                  f"[Windows.UI.Notifications.ToastNotificationManager]::"
                  f"CreateToastNotifier('Lelia').Show($n)")
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                              "-Command", ps])
        else:
            subprocess.Popen(["notify-send", title, msg])
    except Exception:
        pass


def latest_version(package: str) -> str | None:
    """Última versão publicada no PyPI (None se indisponível)."""
    if package != PKG_NAME:
        return None
    try:
        with urllib.request.urlopen(
                f"https://pypi.org/pypi/{PKG_NAME}/json", timeout=8) as r:
            return json.loads(r.read())["info"]["version"]
    except Exception:
        return None


def installed_version() -> str | None:
    py = venv_python()
    if not py.exists():
        return None
    try:
        out = _run([str(py), "-c",
                    "import importlib.metadata as m;print(m.version('lelia'))"],
                   timeout=20).stdout.strip()
        return out or None
    except Exception:
        return None


# ---------------------------------------------------------------- GUI

class Manager(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("620x660")
        self.minsize(560, 600)
        self.configure(background=BG)
        self.cfg = load_config()
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue = queue.Queue()
        self._images: list = []  # mantém refs dos PhotoImage
        self._load_images()
        self._style()
        self._build()
        self.after(150, self._poll)
        self.refresh_status()

    def _load_images(self):
        try:
            icon = tk.PhotoImage(file=str(_resource("logo_64.png")))
            self._images.append(icon)
            self.iconphoto(True, icon)
            self._logo = icon
        except tk.TclError:
            self._logo = None

    # ---------------- UI ----------------

    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground="#1d1d1f",
                     font=FONT, borderwidth=0)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("TLabel", background=BG)
        st.configure("Card.TLabel", background=CARD)
        st.configure("Muted.TLabel", background=BG, foreground=MUTED,
                     font=FONT_SMALL)
        st.configure("Card.Muted.TLabel", background=CARD, foreground=MUTED,
                     font=FONT_SMALL)
        st.configure("Title.TLabel", background=BG, font=(FONT[0], 16, "bold"))
        st.configure("TEntry", padding=5)
        st.configure("TButton", padding=(10, 6), font=FONT_SMALL)
        st.configure("Accent.TButton", background=ACCENT,
                     foreground="white")
        st.map("Accent.TButton",
               background=[("active", ACCENT), ("disabled", "#b0b0b5")])
        st.configure("Tool.TButton", padding=(6, 4))
        st.configure("TLabelframe", background=CARD, borderwidth=1,
                     relief="solid")
        st.configure("TLabelframe.Label", background=CARD, font=FONT_SMALL,
                     foreground=MUTED)
        st.configure("Status.TLabel", background=BG, foreground=MUTED,
                     font=FONT_SMALL, padding=(10, 4), relief="sunken")
        st.configure("TCheckbutton", background=CARD)

    def _card(self, parent, title: str) -> ttk.Frame:
        box = ttk.LabelFrame(parent, text=title, style="TLabelframe",
                             padding=10)
        box.pack(fill="x", padx=14, pady=(10, 0))
        return box

    def _field(self, parent, label: str, row: int) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w",
                                           pady=3)
        ent = ttk.Entry(parent)
        ent.grid(row=row, column=1, sticky="we", padx=(8, 0), pady=3)
        parent.columnconfigure(1, weight=1)
        return ent

    def _build(self):
        # cabeçalho
        head = ttk.Frame(self)
        head.pack(fill="x", padx=16, pady=(14, 0))
        if getattr(self, "_logo", None):
            ttk.Label(head, image=self._logo).pack(side="left")
        titles = ttk.Frame(head)
        titles.pack(side="left", padx=(12, 0), pady=(4, 0))
        ttk.Label(titles, text="Lelia",
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(titles, text="Research Agent — gerente local",
                  style="Muted.TLabel").pack(anchor="w")

        # ---- instalação ----
        inst = self._card(self, "Instalação")
        pkg_row = ttk.Frame(inst)
        pkg_row.pack(fill="x")
        ttk.Label(pkg_row, text="Pacote",
                  style="Card.Muted.TLabel").pack(side="left")
        self.package = ttk.Entry(pkg_row)
        self.package.insert(0, self.cfg["package"])
        self.package.pack(side="left", padx=(8, 0), fill="x", expand=True)
        btns = ttk.Frame(inst)
        btns.pack(fill="x", pady=(8, 0))
        self.btn_install = ttk.Button(btns, text="Instalar / Atualizar",
                                      style="Accent.TButton",
                                      command=self.install)
        self.btn_install.pack(side="left")
        self.btn_check = ttk.Button(btns, text="Verificar versão",
                                    command=self.check_version)
        self.btn_check.pack(side="left", padx=(8, 0))
        self.btn_playwright = ttk.Button(
            btns, text="Instalar browsers (Playwright)",
            command=self.install_playwright)
        self.btn_playwright.pack(side="left", padx=(8, 0))

        # ---- servidor ----
        srv = self._card(self, "Servidor")
        self.workdir = self._field(srv, "Pasta de trabalho (LELIA_HOME):", 0)
        self.workdir.insert(0, self.cfg["workdir"])
        browse = ttk.Button(srv, text="…", style="Tool.TButton",
                            command=self._pick_dir)
        browse.grid(row=0, column=2, padx=(6, 0))
        srv.columnconfigure(2, weight=0)

        net = ttk.Frame(srv)
        net.grid(row=1, column=0, columnspan=3, sticky="we", pady=(6, 0))
        ttk.Label(net, text="Porta:", style="Card.Muted.TLabel").pack(
            side="left")
        self.port = ttk.Entry(net, width=8)
        self.port.insert(0, str(self.cfg["port"]))
        self.port.pack(side="left", padx=(6, 18))
        ttk.Label(net, text="Host:", style="Card.Muted.TLabel").pack(
            side="left")
        self.host = ttk.Entry(net, width=16)
        self.host.insert(0, self.cfg.get("host", "127.0.0.1"))
        self.host.pack(side="left", padx=(6, 0))

        sbtns = ttk.Frame(srv)
        sbtns.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.btn_start = ttk.Button(sbtns, text="Iniciar",
                                    style="Accent.TButton",
                                    command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(sbtns, text="Parar", command=self.stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.btn_open = ttk.Button(sbtns, text="Abrir no navegador",
                                   command=self.open_browser)
        self.btn_open.pack(side="left", padx=(8, 0))

        # ---- status ----
        self.status = ttk.Label(self, text="", style="Status.TLabel",
                                anchor="w")
        self.status.pack(fill="x", side="bottom")

        # ---- log ----
        logf = ttk.LabelFrame(self, text="Log", style="TLabelframe",
                              padding=6)
        logf.pack(fill="both", expand=True, padx=14, pady=10)
        self.logw = tk.Text(logf, height=14, state="disabled",
                            font=("Menlo" if IS_MAC else
                                  "Consolas" if IS_WIN else "DejaVu Sans Mono",
                                  9),
                            background="#1e1e1e", foreground="#d4d4d4",
                            insertbackground="#d4d4d4", relief="flat",
                            borderwidth=0)
        self.logw.pack(fill="both", expand=True)

    # ---------------- log / fila ----------------

    def log(self, msg: str):
        self.q.put(("log", msg))

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.logw.configure(state="normal")
                    self.logw.insert("end", val + "\n")
                    self.logw.see("end")
                    self.logw.configure(state="disabled")
                elif kind == "status":
                    self.status.configure(text=val)
                elif kind == "done":
                    self._worker_done()
                elif kind == "proc":
                    self._on_proc_exit(val)
        except queue.Empty:
            pass
        self.after(150, self._poll)

    # ---------------- ações ----------------

    def _save(self):
        self.cfg.update({
            "workdir": self.workdir.get().strip() or DEFAULTS["workdir"],
            "port": int(self.port.get() or DEFAULTS["port"]),
            "host": self.host.get().strip() or "127.0.0.1",
            "package": self.package.get().strip() or "lelia",
        })
        save_config(self.cfg)

    def _pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.workdir.get() or None)
        if d:
            self.workdir.delete(0, "end")
            self.workdir.insert(0, d)

    def refresh_status(self):
        py = find_python()
        parts = []
        if py:
            parts.append(f"Python: {py}")
        else:
            parts.append("Python >= 3.11 NÃO encontrado")
        ver = installed_version()
        parts.append(f"lelia instalado: {ver or 'não'}")
        latest = latest_version(self.cfg["package"])
        if latest:
            parts.append(f"PyPI: {latest}")
        if self.proc and self.proc.poll() is None:
            parts.append(f"status: RODANDO (porta {self.cfg['port']})")
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
        else:
            parts.append("status: parado")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
        self.q.put(("status", "  ·  ".join(parts)))

    def _busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        self.btn_install.configure(state=state)
        self.btn_check.configure(state=state)
        self.btn_playwright.configure(state=state)
        if not busy:
            self.refresh_status()

    def install(self):
        self._save()
        self._busy(True)
        threading.Thread(target=self._install_worker, daemon=True).start()

    def _install_worker(self):
        self.q.put(("status", "Preparando…"))
        py = find_python()
        if not py:
            self.log("ERRO: nenhum Python >= 3.11 encontrado no sistema.")
            self.log("Instale Python em https://www.python.org/downloads/ "
                     "e tente novamente.")
            self.q.put(("done", None))
            return
        self.log(f"Python encontrado: {py}")
        vd = venv_dir()
        vd.mkdir(parents=True, exist_ok=True)
        pyexe = venv_python()
        if not pyexe.exists():
            self.log(f"Criando venv em {vd} …")
            r = _run(shlex.split(py) + ["-m", "venv", str(vd)], timeout=300)
            if r.returncode != 0:
                self.log("ERRO ao criar venv:\n" + (r.stderr or r.stdout))
                self.q.put(("done", None))
                return
        self.log(f"Instalando '{self.cfg['package']}' no venv …")
        cmd = [str(pyexe), "-m", "pip", "install", "--upgrade",
               "--disable-pip-version-check", self.cfg["package"]]
        self.log("$ " + " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            self.log(line.rstrip())
        rc = proc.wait()
        if rc == 0:
            self.log("Instalação concluída.")
            _notify("Lelia", "Instalação concluída.")
        else:
            self.log(f"ERRO: pip retornou código {rc}.")
            _notify("Lelia", "Falha na instalação — veja o log.")
        self.q.put(("done", None))

    def install_playwright(self):
        self._busy(True)
        threading.Thread(target=self._playwright_worker, daemon=True).start()

    def _playwright_worker(self):
        py = venv_python()
        if not py.exists():
            self.log("ERRO: instale o Lelia primeiro (o Playwright é "
                     "dependência do pacote).")
            self.q.put(("done", None))
            return
        cmd = [str(py), "-m", "playwright", "install", "chromium"]
        self.log("$ " + " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            self.log(line.rstrip())
        rc = proc.wait()
        if rc == 0:
            self.log("Browsers do Playwright instalados.")
            _notify("Lelia", "Browsers do Playwright instalados.")
        else:
            self.log(f"ERRO: playwright retornou código {rc}.")
        self.q.put(("done", None))

    def start(self):
        self._save()
        if not lelia_script().exists() and not venv_python().exists():
            messagebox.showwarning(APP_NAME,
                                   "Lelia não instalado. Clique em "
                                   "'Instalar / Atualizar' primeiro.")
            return
        threading.Thread(target=self._start_worker, daemon=True).start()

    def _start_worker(self):
        py = venv_python()
        wd = Path(self.cfg["workdir"])
        wd.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, LELIA_HOME=str(wd))
        cmd = [str(py), "-m", "lelia.cli", "start",
               "--host", self.cfg["host"], "--port", str(self.cfg["port"])]
        self.log("$ " + " ".join(cmd) + f"   (LELIA_HOME={wd})")
        try:
            self.proc = subprocess.Popen(cmd, env=env,
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
        except Exception as exc:
            self.log(f"ERRO ao iniciar: {exc}")
            self.q.put(("done", None))
            return
        threading.Thread(target=self._watch_proc, daemon=True).start()
        self.log(f"Servidor iniciado na porta {self.cfg['port']} — "
                 f"abra http://{self.cfg['host']}:{self.cfg['port']}")
        _notify("Lelia", f"Servidor iniciado (porta {self.cfg['port']}).")
        self.q.put(("done", None))

    def _watch_proc(self):
        if self.proc:
            self.proc.wait()
            self.q.put(("proc", self.proc.returncode))

    def _on_proc_exit(self, rc):
        self.proc = None
        self.log(f"Servidor encerrado (código {rc}).")
        _notify("Lelia", "Servidor encerrado.")
        self.refresh_status()

    def _worker_done(self):
        self._busy(False)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.log("Parando servidor…")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
            self.log("Servidor parado.")
            _notify("Lelia", "Servidor parado.")
            self.refresh_status()

    def open_browser(self):
        self._save()
        webbrowser.open(f"http://{self.cfg['host']}:{self.cfg['port']}")

    def check_version(self):
        self._busy(True)
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self):
        self.q.put(("status", "Consultando PyPI…"))
        latest = latest_version(self.cfg["package"])
        cur = installed_version()
        if latest is None:
            self.log("Não foi possível consultar o PyPI.")
        else:
            self.log(f"PyPI: {latest} · instalado: {cur or 'nenhum'}")
            if cur and cur != latest:
                self.log("Há uma versão mais recente — use "
                         "'Instalar / Atualizar'.")
                _notify("Lelia", f"Nova versão disponível: {latest}")
            elif cur == latest:
                self.log("Versão instalada está atualizada.")
        self.q.put(("done", None))


if __name__ == "__main__":
    Manager().mainloop()
