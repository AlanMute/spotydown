# app_config.py
import os
import json
from typing import Optional

APP_DIR_NAME = "SpotifyPlaylistDownloader"
CONFIG_FILE = "config.json"

def _config_dir() -> str:
    # %APPDATA%\SpotifyPlaylistDownloader  (Windows)
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path

def config_path() -> str:
    return os.path.join(_config_dir(), CONFIG_FILE)

def load_config() -> dict:
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_config(cfg: dict) -> None:
    path = config_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _ask_dir_dialog(initialdir: Optional[str] = None) -> Optional[str]:
    """
    Показывает системный диалог выбора папки (tkinter).
    Возвращает выбранный путь или None, если пользователь отменил.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(
            initialdir=initialdir or os.path.expanduser("~"),
            title="Выбери папку для загрузки музыки"
        )
        root.destroy()
        if path:
            return os.path.normpath(path)
    except Exception:
        pass
    return None

def ensure_music_dir(console) -> str:
    """
    Возвращает путь к папке музыки.
    Если в конфиге нет/не существует — спрашиваем через диалог, сохраняем в конфиг.
    Если пользователь отменил — используем текущую директорию.
    """
    cfg = load_config()
    music_dir = cfg.get("music_dir", "")

    if not music_dir or not os.path.isdir(music_dir):
        console.print("\n[bold cyan]Первый запуск:[/bold cyan] выбери папку, куда будем сохранять музыку.")
        picked = _ask_dir_dialog(music_dir or os.getcwd())
        if not picked:
            console.print("[yellow]Диалог отменён. Использую текущую папку.[/yellow]")
            picked = os.getcwd()
        os.makedirs(picked, exist_ok=True)
        cfg["music_dir"] = picked
        save_config(cfg)
        music_dir = picked

    return music_dir

def change_music_dir(console) -> str:
    """
    Открывает диалог снова, обновляет конфиг, возвращает новый путь
    (или старый, если пользователь отменил).
    """
    cfg = load_config()
    current = cfg.get("music_dir") or os.getcwd()
    console.print(f"Текущая папка: [dim]{current}[/dim]")
    picked = _ask_dir_dialog(current)
    if picked:
        os.makedirs(picked, exist_ok=True)
        cfg["music_dir"] = picked
        save_config(cfg)
        console.print(f"[green]Новая папка сохранена:[/green] [dim]{picked}[/dim]")
        return picked
    else:
        console.print("[yellow]Изменение отменено.[/yellow]")
        return current
