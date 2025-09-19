import os
from typing import Optional, List, Tuple
from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt, IntPrompt, Confirm
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from mutagen.flac import FLAC, Picture
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, error
import urllib.request
import yt_dlp as youtube_dl
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from urllib.parse import urlparse, parse_qs
from io import BytesIO
from rich import box
import sys

COVER_SIZE = 640
COVER_MAX_BYTES = 400 * 1024

console: Console
sanitize_filename = None

def clear_screen():
    try:
        console.print("\033[2J\033[3J\033[H", end="")
        console.file.flush()
    except Exception:
        pass
    try:
        console.clear()
    except Exception:
        pass
    if os.name == "nt":
        try:
            os.system("cls")
        except Exception:
            pass

def normalize_youtube_url(url: str) -> str:
    """
    Возвращает "чистую" ссылку на одно видео (без &list= ... и т.п.).
    Если это watch?v=ID&*, оставляем только v=ID.
    """
    try:
        u = urlparse(url)
        if u.netloc.endswith("youtube.com") and u.path == "/watch":
            qs = parse_qs(u.query)
            v = qs.get("v", [None])[0]
            if v:
                return f"https://www.youtube.com/watch?v={v}"
        if u.netloc.endswith("youtu.be"):
            vid = u.path.lstrip("/")
            if vid:
                return f"https://www.youtube.com/watch?v={vid}"
    except Exception:
        pass
    return url

def set_console(c: Console):
    """Вызывается из host-приложения, чтобы модуль пользовался его Console."""
    global console
    console = c

IS_WIN = os.name == "nt"
BOX_STYLE = box.SIMPLE if IS_WIN else box.ROUNDED

def page(title: str, subtitle: str | None = None):
    clear_screen()
    console.print(Panel.fit(subtitle or "", title=title, border_style="title"))

# ---------- Spotify helpers ----------

def get_spotify_track_info(track_url: str, client_id: str, client_secret: str) -> dict:
    """Возвращает meta трека по URL из Spotify."""
    auth = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
    sp = spotipy.Spotify(auth_manager=auth)
    track = sp.track(track_url)
    info = {
        "artist": ", ".join([a["name"] for a in track["artists"]]),
        "title": track["name"],
        "album": track["album"]["name"],
        "duration_ms": track["duration_ms"],
        "cover_url": track["album"]["images"][0]["url"] if track["album"]["images"] else None
    }
    return info

# ---------- YouTube helpers ----------

def format_duration(seconds: int) -> str:
    m = seconds // 60
    s = seconds % 60
    return f"{m:02d}:{s:02d}"

def yt_search_for_track(track_info: dict, cookies_file: Optional[str], limit: int = 8) -> List[dict]:
    """Ищет кандидатов на YouTube и возвращает список entries."""
    queries = [
        f"{track_info['artist']} - {track_info['title']} official audio",
        f"{track_info['artist']} - {track_info['title']}",
        f"{track_info['title']} {track_info['artist']}",
        f"{track_info['title']}"
    ]
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    if cookies_file and os.path.exists(cookies_file):
        opts["cookiefile"] = cookies_file

    seen_urls = set()
    collected: List[dict] = []
    with youtube_dl.YoutubeDL(opts) as ydl:
        for q in queries:
            if len(collected) >= limit:
                break
            try:
                res = ydl.extract_info(f"ytsearch5:{q}", download=False)
                for e in (res.get("entries") or []):
                    if not e:
                        continue
                    url = e.get("url")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    collected.append(e)
                    if len(collected) >= limit:
                        break
            except Exception:
                continue
    return collected

def yt_get_video_info(url: str, cookies_file: Optional[str]) -> Optional[dict]:
    """Достаёт инфу по прямой YouTube-ссылке (title/uploader/duration/thumbnail)."""
    url = normalize_youtube_url(url) 
    u = urlparse(url)
    host = u.netloc.lower()
    if "youtube.com" not in host and "youtu.be" not in host:
        return None
    
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,          
        "socket_timeout": 15,
        "prefer_ipv4": True,
        "extractor_args": {"youtube": {"player_client": ["web"]}},
    }
    if cookies_file and os.path.exists(cookies_file):
        opts["cookiefile"] = cookies_file

    try:
        with youtube_dl.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("_type") == "playlist":
                entries = info.get("entries") or []
                if entries:
                    info = entries[0]
            thumb = info.get("thumbnail")
            thumbs = info.get("thumbnails") or []
            if thumbs:
                jpg = next((t.get("url") for t in thumbs if (t.get("url") or "").lower().endswith(".jpg")), None)
                thumb = jpg or thumbs[-1].get("url") or thumb
            return {
                "title": info.get("title", ""),
                "uploader": info.get("uploader", "") or info.get("channel", ""),
                "duration": int(info.get("duration") or 0),
                "thumbnail": thumb,
                "url": normalize_youtube_url(info.get("webpage_url") or url),
            }
    except Exception:
        return None


def parse_title_guess(yt_title: str) -> tuple[str, str]:
    """Пытаемся угадать (artist, title) из 'Artist - Title'."""
    parts = [p.strip() for p in yt_title.split(" - ", 1)]
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    return "", yt_title.strip()

# ---------- Target folder ----------

def choose_target_folder(default_name: str, base_music_dir: str) -> Optional[str]:
    """
    Выбор подпапки ВНУТРИ base_music_dir. Возвращает путь или None, если выбрали «Назад».
    """
    base = base_music_dir or os.getcwd()
    try:
        dirs = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
    except Exception:
        dirs = []
    dirs_sorted = sorted(dirs, key=str.lower)

    page("Куда сохранить трек?", f"[dim]{base}[/dim]")
    table = Table(show_header=True, header_style="title", box=BOX_STYLE)
    table.add_column("#", justify="right")
    table.add_column("Подпапка")
    table.add_row("0", "⟵ Назад")

    choices = {}
    idx = 1
    for d in dirs_sorted:
        table.add_row(str(idx), d)
        choices[str(idx)] = d
        idx += 1

    new_idx = str(idx)
    table.add_row(new_idx, f"[italic]Создать новую: {default_name}[/italic]")
    console.print(table)

    pick = Prompt.ask("Выбери номер", default=new_idx)
    if pick == "0":
        return None
    if pick == new_idx:
        target = os.path.join(base, default_name)
        os.makedirs(target, exist_ok=True)
        return target
    picked_name = choices.get(pick)
    if picked_name:
        return os.path.join(base, picked_name)
    
    target = os.path.join(base, default_name)
    os.makedirs(target, exist_ok=True)
    return target


def _fetch_cover_bytes(url: str) -> tuple[bytes, str, str]:
    """
    Качаем обложку и нормализуем в 'spotify-совместимый' JPEG:
    - квадрат 640x640 (центр-кроп)
    - RGB, без альфы
    - baseline JPEG (progressive=False)
    - при необходимости ужимаем < COVER_MAX_BYTES
    Возвращаем (bytes, mime, ext).
    """
    if not url:
        return b"", "", ""

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
            
    except Exception:
        return b"", "", ""

    try:
        from PIL import Image, ImageOps 
        img = Image.open(BytesIO(data))

        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass

        if img.mode not in ("RGB",):
            img = img.convert("RGB")

        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top  = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))

        if img.size != (COVER_SIZE, COVER_SIZE):
            img = img.resize((COVER_SIZE, COVER_SIZE), Image.LANCZOS)

        quality = 88
        def encode(q: int) -> bytes:
            buf = BytesIO()
            img.save(
                buf,
                format="JPEG",
                quality=q,
                optimize=True,
                progressive=False,
                subsampling="4:2:0",
            )
            return buf.getvalue()

        out = encode(quality)
        
        while len(out) > COVER_MAX_BYTES and quality > 60:
            quality -= 6
            out = encode(quality)

        return out, "image/jpeg", "jpg"

    except ImportError:
        return b"", "", ""
    except Exception:
        return b"", "", ""

def _write_metadata_unified(audio_path: str, track_info: dict):
    ext = os.path.splitext(audio_path)[1].lower()
    title  = track_info.get("title", "") or ""
    artist = track_info.get("artist", "") or ""
    album  = track_info.get("album", "") or ""
    cover_url = track_info.get("cover_url") or ""

    # нормализуем обложку (у тебя уже есть _fetch_cover_bytes -> JPEG 640x640)
    data, mime, _ = _fetch_cover_bytes(cover_url)

    if ext == ".mp3":
        audio = MP3(audio_path, ID3=ID3)
        try: audio.add_tags()
        except error: pass
        try:
            for k in list(audio.tags.keys()):
                if k.startswith("APIC"): del audio.tags[k]
        except Exception: pass
        audio.tags.add(TIT2(encoding=3, text=title))
        audio.tags.add(TPE1(encoding=3, text=artist))
        audio.tags.add(TALB(encoding=3, text=album))
        if data and mime:
            try: audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            except Exception: pass
        audio.save(v2_version=3)

    elif ext == ".flac":
        audio = FLAC(audio_path)
        audio["title"]  = title
        audio["artist"] = artist
        audio["album"]  = album
        try: audio.clear_pictures()
        except Exception: pass
        if data and mime:
            try:
                pic = Picture()
                pic.type = 3
                pic.desc = "Cover"
                pic.mime = mime
                pic.data = data
                audio.add_picture(pic)
            except Exception:
                pass
        audio.save()

def download_audio_from_entry(track_info: dict, entry: dict, out_dir: str,
                              cookies_file: Optional[str],
                              bitrate_kbps: int,
                              audio_format: str) -> Tuple[bool, Optional[str]]:
    video_url = normalize_youtube_url(entry.get("url") or "")
    if not video_url:
        return False, "У выбранного результата нет URL"

    final_ext = "mp3" if audio_format == "mp3" else "flac"
    outtmpl = os.path.join(out_dir, f"{sanitize_filename(track_info['artist'])} - {sanitize_filename(track_info['title'])}.%(ext)s")

    pp = {"key": "FFmpegExtractAudio", "preferredcodec": audio_format}
    if audio_format == "mp3":
        pp["preferredquality"] = str(bitrate_kbps)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "postprocessors": [pp],
        "quiet": True, "no_warnings": True,
        "retries": 3, "fragment_retries": 3, "continuedl": True,
        "skip_unavailable_fragments": True, "socket_timeout": 30,
        "prefer_ipv4": True, "noplaylist": True,
    }
    
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file

    try:
        with youtube_dl.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
    except Exception as e:
        return False, str(e)

    # итоговый путь с выбранным расширением
    out_path = os.path.join(out_dir, f"{sanitize_filename(track_info['artist'])} - {sanitize_filename(track_info['title'])}.{final_ext}")
    if os.path.exists(out_path):
        try:
            _write_metadata_unified(out_path, track_info)  # см. ниже
        except Exception as e:
            return False, f"Скачалось, но метаданные не записались: {e}"
        return True, None
    else:
        return False, f"Файл не найден после скачивания ({final_ext})"

def download_audio_by_url(youtube_url: str, track_info: dict, out_dir: str,
                          cookies_file: Optional[str],
                          bitrate_kbps: int, audio_format: str) -> Tuple[bool, Optional[str]]:
    youtube_url = normalize_youtube_url(youtube_url)
    return download_audio_from_entry(track_info, {"url": youtube_url}, out_dir, cookies_file, bitrate_kbps, audio_format)


# ---------- Top-level CLI ----------

def cli_download_single_track(
    cookies_file: Optional[str],
    sanitize_filename_func,
    client_id: str,
    client_secret: str,
    base_music_dir: str,
    audio_bitrate_kbps: int,
    audio_format: str,
):
    global sanitize_filename
    sanitize_filename = sanitize_filename_func

    page("Скачать одиночный трек", "Выбери источник. В любой момент вводи 0 — чтобы вернуться.")
    src = IntPrompt.ask("Источник (0 — назад, 1 = Spotify URL, 2 = YouTube URL)", choices=["0", "1", "2"], default="1")
    if src == 0:
        return

    # ---------- Ветка 1: Spotify URL ----------
    if src == 1:
        while True:
            page("Один трек • Spotify", "Вставь ссылку на трек Spotify (или 0 — назад).")
            sp_url = Prompt.ask("URL", default="")
            if sp_url.strip() == "0" or not sp_url.strip():
                return
            sp_url = sp_url.strip()

            page("Один трек • Spotify", "[muted]Получаю метаданные трека...[/muted]")
            try:
                sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=client_id, client_secret=client_secret))
                tr = sp.track(sp_url)
                artist = ", ".join(a["name"] for a in tr["artists"])
                title  = tr["name"]
                album  = tr["album"]["name"]
                duration_ms = int(tr.get("duration_ms") or 0)
                cover_url   = (tr["album"]["images"][0]["url"] if tr["album"]["images"] else "")
            except Exception as e:
                page("Один трек • Spotify", f"[red]Ошибка Spotify API:[/red] {e}\n\n[dim]Enter для возврата[/dim]")
                Prompt.ask("", default="", show_default=False)
                return

            meta = Table(show_header=False, box=BOX_STYLE)
            meta.add_row("[muted]Артист:[/muted]", artist)
            meta.add_row("[muted]Название:[/muted]", title)
            meta.add_row("[muted]Альбом:[/muted]", album)
            meta.add_row("[muted]Длительность:[/muted]", format_duration(duration_ms // 1000))
            meta.add_row("[muted]Обложка:[/muted]", cover_url or "—")
            console.clear()
            console.print(Panel(meta, title="Мета из Spotify", border_style="title"))

            track_info = {
                "artist": artist,
                "title": title,
                "album": album,
                "duration_ms": duration_ms,
                "cover_url": cover_url,
            }

            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
                t = progress.add_task("Ищу на YouTube...", total=None)
                candidates = []
                try:
                    ydl_opts = {
                        "quiet": True,
                        "no_warnings": True,
                        "extract_flat": True,
                        "noplaylist": True,
                        "prefer_ipv4": True,
                        "socket_timeout": 15,
                        "extractor_args": {"youtube": {"player_client": ["web"]}},
                    }
                    if cookies_file and os.path.exists(cookies_file):
                        ydl_opts["cookiefile"] = cookies_file
                    query = f"{artist} - {title}"
                    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
                        res = ydl.extract_info(f"ytsearch10:{query}", download=False)
                        for e in (res.get("entries") or []):
                            if e:
                                candidates.append(e)
                except Exception:
                    candidates = []
                progress.update(t, completed=1)

            if not candidates:
                page("Один трек • Spotify", "[yellow]Ничего не нашёл на YouTube по этому треку[/yellow]\n\n[dim]Enter для возврата[/dim]")
                Prompt.ask("", default="", show_default=False)
                return

            table = Table(show_header=True, header_style="title", box=BOX_STYLE)
            table.add_column("#", justify="right", style="muted")
            table.add_column("Название", style="ok")
            table.add_column("Канал", style="muted")
            table.add_column("Длит.", style="muted")
            table.add_row("0", "⟵ Назад", "", "")
            for i, e in enumerate(candidates, 1):
                title_e = (e.get("title") or "").strip()
                uploader = e.get("uploader") or ""
                dur = e.get("duration")
                dur_s = format_duration(int(dur)) if isinstance(dur, (int, float)) else "—"
                table.add_row(str(i), title_e, uploader, dur_s)
            console.print(table)

            idx = IntPrompt.ask("Выбери номер", choices=[str(i) for i in range(0, len(candidates)+1)])
            if idx == 0:
                return
            chosen = candidates[int(idx)-1]

            default_dir = f"{sanitize_filename(track_info['artist'])} - {sanitize_filename(track_info['title'])}"
            target_dir  = choose_target_folder(default_dir, base_music_dir)
            if target_dir is None:
                return

            page("Один трек • Spotify", "[muted]Скачивание и тегирование...[/muted]")
            ok, err = download_audio_from_entry(track_info, chosen, target_dir, cookies_file,
                                    audio_bitrate_kbps, audio_format)
            if ok:
                page("Один трек • Spotify", f"[bold green]Готово![/bold green]\n[dim]Файл в папке:[/dim] {target_dir}\n\n[dim]Enter для возврата[/dim]")
                Prompt.ask("", default="", show_default=False)
            else:
                page("Один трек • Spotify", f"[red]Ошибка:[/red] {err}\n\n[dim]Enter для возврата[/dim]")
                Prompt.ask("", default="", show_default=False)
            return

    # ---------- Ветка 2: YouTube URL ----------
    while True:
        page("Один трек • YouTube", "Вставь ссылку на видео (или 0 — назад).")
        yt_url = Prompt.ask("URL", default="")
        if yt_url.strip() == "0" or not yt_url.strip():
            return
        yt_url = yt_url.strip()

        page("Один трек • YouTube", "[muted]Извлекаю информацию о видео...[/muted]")
        info = yt_get_video_info(yt_url, cookies_file)
        if not info:
            page("Один трек • YouTube", "[red]Не удалось извлечь информацию о видео[/red]\n\n[dim]Enter для возврата[/dim]")
            Prompt.ask("", default="", show_default=False)
            return

        artist_guess, title_guess = parse_title_guess(info["title"])
        default_artist = artist_guess or info["uploader"] or "Unknown Artist"
        default_title  = title_guess  or info["title"]     or "Unknown Title"
        default_album  = info["uploader"] or "YouTube"

        meta = Table(show_header=False, box=BOX_STYLE)
        meta.add_row("[muted]Видео:[/muted]", info["title"])
        meta.add_row("[muted]Канал:[/muted]", info["uploader"])
        meta.add_row("[muted]Длительность:[/muted]", format_duration(info["duration"]))
        meta.add_row("[muted]Предполагаемый артист:[/muted]", default_artist)
        meta.add_row("[muted]Предполагаемое название:[/muted]", default_title)
        console.clear()
        console.print(Panel(meta, title="Инфо YouTube", border_style="title"))

        keep = Confirm.ask("Оставить эти теги?", default=True)
        if keep:
            track_info = {
                "artist": default_artist,
                "title":  default_title,
                "album":  default_album,
                "duration_ms": info["duration"] * 1000,
                "cover_url": info.get("thumbnail"),
            }
        else:
            artist = Prompt.ask("Автор",   default=default_artist or "Unknown Artist")
            if artist.strip() == "0":
                return
            title  = Prompt.ask("Название", default=default_title  or "Unknown Title")
            if title.strip() == "0":
                return
            track_info = {
                "artist": artist.strip(),
                "title":  title.strip(),
                "album":  default_album,
                "duration_ms": info["duration"] * 1000,
                "cover_url": info.get("thumbnail"),
            }

        default_dir = f"{sanitize_filename(track_info['artist'])} - {sanitize_filename(track_info['title'])}"
        target_dir  = choose_target_folder(default_dir, base_music_dir)
        if target_dir is None:
            return

        page("Один трек • YouTube", "[muted]Скачивание и тегирование...[/muted]")
        ok, err = download_audio_by_url(info["url"], track_info, target_dir, cookies_file,
                                audio_bitrate_kbps, audio_format)
        if ok:
            page("Один трек • YouTube", f"[bold green]Готово![/bold green]\n[dim]Файл в папке:[/dim] {target_dir}\n\n[dim]Enter для возврата[/dim]")
            Prompt.ask("", default="", show_default=False)
        else:
            page("Один трек • YouTube", f"[red]Ошибка:[/red] {err}\n\n[dim]Enter для возврата[/dim]")
            Prompt.ask("", default="", show_default=False)
        return

