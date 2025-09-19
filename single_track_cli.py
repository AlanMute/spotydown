# single_track_cli.py
import os
from typing import Optional, List, Tuple
from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt, IntPrompt, Confirm
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, error
import urllib.request
import yt_dlp as youtube_dl
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from urllib.parse import urlparse, parse_qs
from io import BytesIO
from urllib.parse import urlparse

console: Console
sanitize_filename = None

COVER_SIZE = 640                
COVER_MAX_BYTES = 400 * 1024

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
        # youtu.be/ID
        if u.netloc.endswith("youtu.be"):
            vid = u.path.lstrip("/")
            if vid:
                return f"https://www.youtube.com/watch?v={vid}"
    except Exception:
        pass
    # если не распознали, вернём как есть
    return url

def set_console(c: Console):
    """Вызывается из host-приложения, чтобы модуль пользовался его Console."""
    global console
    console = c

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
    else:
        # мягкий фолбэк — возьмём куки из локального браузера (Windows/Chrome по умолчанию)
        opts["cookiesfrombrowser"] = ("chrome",)

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
    url = normalize_youtube_url(url)  # <- убираем &list=...
    u = urlparse(url)
    host = u.netloc.lower()
    if "youtube.com" not in host and "youtu.be" not in host:
        return None
    
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,          # <- ВАЖНО
        "socket_timeout": 15,
        "prefer_ipv4": True,
        # Небольшая помощь парсеру YouTube
        "extractor_args": {"youtube": {"player_client": ["web"]}},
    }
    if cookies_file and os.path.exists(cookies_file):
        opts["cookiefile"] = cookies_file
    else:
        opts["cookiesfrombrowser"] = ("chrome",)

    try:
        with youtube_dl.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            # Если вдруг всё равно пришёл плейлист — возьмём первый элемент
            if info.get("_type") == "playlist":
                entries = info.get("entries") or []
                if entries:
                    info = entries[0]
            thumb = info.get("thumbnail")
            thumbs = info.get("thumbnails") or []
            if thumbs:
                thumb = thumbs[-1].get("url") or thumb
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
    # fallback: всё кладём в title
    return "", yt_title.strip()

# ---------- Target folder ----------

def choose_target_folder(default_name: str, base_music_dir: str) -> str:
    """
    Выбор подпапки назначения ВНУТРИ base_music_dir: существующая или новая.
    """
    base = base_music_dir or os.getcwd()
    try:
        dirs = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
    except Exception:
        dirs = []
    dirs_sorted = sorted(dirs, key=str.lower)

    table = Table(title=f"Куда сохранить трек?  [dim]{base}[/dim]")
    table.add_column("#", justify="right")
    table.add_column("Подпапка")

    choices = {}
    idx = 1
    for d in dirs_sorted:
        table.add_row(str(idx), d)
        choices[str(idx)] = d
        idx += 1

    new_idx = str(idx)
    table.add_row(new_idx, f"[italic]Создать новую: {default_name}[/italic]")
    console.print(table)

    pick = Prompt.ask("Выбери номер папки", default=new_idx)
    if pick == new_idx:
        target = os.path.join(base, default_name)
        os.makedirs(target, exist_ok=True)
        return target
    # существующая подпапка
    picked_name = choices.get(pick)
    if picked_name:
        return os.path.join(base, picked_name)
    # fallback — создаём новую по умолчанию
    target = os.path.join(base, default_name)
    os.makedirs(target, exist_ok=True)
    return target

# ---------- Download + tag ----------

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

    # ==== качаем ====
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
            # ctype = (resp.headers.get("Content-Type") or "").split(";")[0].lower()
    except Exception:
        return b"", "", ""

    # ==== нормализуем через Pillow ====
    try:
        from PIL import Image, ImageOps  # pip install pillow
        img = Image.open(BytesIO(data))

        # учтём EXIF-ориентацию
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass

        # в RGB (убираем альфу/индексные палитры/CMYK)
        if img.mode not in ("RGB",):
            img = img.convert("RGB")

        # центр-кроп до квадрата
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top  = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))

        # ресайз до COVER_SIZE
        if img.size != (COVER_SIZE, COVER_SIZE):
            img = img.resize((COVER_SIZE, COVER_SIZE), Image.LANCZOS)

        # сохраняем baseline JPEG, без progressive
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
        # ужимаем, если нужно
        while len(out) > COVER_MAX_BYTES and quality > 60:
            quality -= 6
            out = encode(quality)

        return out, "image/jpeg", "jpg"

    except ImportError:
        return b"", "", ""
    except Exception:
        return b"", "", ""

def _write_metadata(mp3_path: str, track_info: dict):
    audio = MP3(mp3_path, ID3=ID3)
    try:
        audio.add_tags()
    except error:
        pass

    try:
        for key in list(audio.tags.keys()):
            if key.startswith("APIC"):
                del audio.tags[key]
    except Exception:
        pass

    title  = track_info.get("title", "") or ""
    artist = track_info.get("artist", "") or ""
    album  = track_info.get("album", "") or ""

    audio.tags.add(TIT2(encoding=3, text=title))
    audio.tags.add(TPE1(encoding=3, text=artist))
    audio.tags.add(TALB(encoding=3, text=album))

    cover_url = track_info.get("cover_url")
    if cover_url:
        data, mime, ext = _fetch_cover_bytes(cover_url)
        if data and mime:
            covers_dir = os.path.join(os.path.dirname(mp3_path), "covers")
            os.makedirs(covers_dir, exist_ok=True)
            cover_file = os.path.join(
                covers_dir,
                f"{sanitize_filename(artist)} - {sanitize_filename(title)}.{ext}"
            )
            try:
                with open(cover_file, "wb") as f:
                    f.write(data)
            except Exception:
                pass
            try:
                audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            except Exception:
                pass

    audio.save(v2_version=3)

def download_audio_from_entry(track_info: dict, entry: dict, out_dir: str, cookies_file: Optional[str]) -> Tuple[bool, Optional[str]]:
    video_url = normalize_youtube_url(entry.get("url") or "")
    if not video_url:
        return False, "У выбранного результата нет URL"

    outtmpl = os.path.join(out_dir, f"{sanitize_filename(track_info['artist'])} - {sanitize_filename(track_info['title'])}.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}],
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "continuedl": True,
        "skip_unavailable_fragments": True,
        "socket_timeout": 30,
        "prefer_ipv4": True,
        "noplaylist": True,    # <- ВАЖНО
    }
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file
    else:
        ydl_opts["cookiesfrombrowser"] = ("chrome",)

    try:
        with youtube_dl.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
    except Exception as e:
        return False, str(e)

    mp3_path = os.path.join(out_dir, f"{sanitize_filename(track_info['artist'])} - {sanitize_filename(track_info['title'])}.mp3")
    if os.path.exists(mp3_path):
        try:
            _write_metadata(mp3_path, track_info)
        except Exception as e:
            return False, f"Скачалось, но метаданные не записались: {e}"
        return True, None
    else:
        return False, "Файл mp3 не найден после скачивания"

def download_audio_by_url(youtube_url: str, track_info: dict, out_dir: str, cookies_file: Optional[str]) -> Tuple[bool, Optional[str]]:
    youtube_url = normalize_youtube_url(youtube_url)
    return download_audio_from_entry(track_info, {"url": youtube_url}, out_dir, cookies_file)


# ---------- Top-level CLI ----------

def cli_download_single_track(
    cookies_file: Optional[str],
    sanitize_filename_func,
    client_id: str,
    client_secret: str,
    base_music_dir: str,
):
    """Верхнеуровневый сценарий: выбор источника, URL, выбор выдачи/папки, скачивание."""
    global sanitize_filename
    sanitize_filename = sanitize_filename_func  # привязываем переданную функцию

    console.print(Panel.fit("Скачать одиночный трек", title="🎯", border_style="title"))
    src = int(Prompt.ask(
        "Источник (1 = Spotify URL, 2 = YouTube URL)",
        choices=["1","2"],
        default="1"
    ))

    # ---------- Ветка 1: Spotify URL -> выдача с YouTube ----------
    if src == 1:
        sp_url = Prompt.ask("Вставь ссылку на трек Spotify").strip()
        if not sp_url:
            console.print("[red]URL пустой[/red]")
            return

        # 1) Тянем мету из Spotify
        try:
            sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=client_id, client_secret=client_secret))
            tr = sp.track(sp_url)
            artist = ", ".join(a["name"] for a in tr["artists"])
            title  = tr["name"]
            album  = tr["album"]["name"]
            duration_ms = int(tr.get("duration_ms") or 0)
            cover_url   = (tr["album"]["images"][0]["url"] if tr["album"]["images"] else "")
        except Exception as e:
            console.print(f"[red]Ошибка Spotify API:[/red] {e}")
            return

        track_info = {
            "artist": artist,
            "title": title,
            "album": album,
            "duration_ms": duration_ms,
            "cover_url": cover_url,  # мета и обложка из Spotify (как ты хотел для споти-кейса)
        }

        # 2) Ищем кандидатов на YouTube по метаданным
        query = f"{artist} - {title}"
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

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
            t = progress.add_task("Ищу на YouTube...", total=None)
            candidates = []
            try:
                with youtube_dl.YoutubeDL(ydl_opts) as ydl:
                    res = ydl.extract_info(f"ytsearch10:{query}", download=False)
                    for e in (res.get("entries") or []):
                        if e:
                            candidates.append(e)
            except Exception as e:
                candidates = []
            progress.update(t, completed=1)

        if not candidates:
            console.print("[yellow]Ничего не нашёл на YouTube по этому треку[/yellow]")
            return

        # 3) Покажем меню выбора кандидата
        table = Table(show_header=True, header_style="title")
        table.add_column("#", justify="right", style="muted")
        table.add_column("Название", style="ok")
        table.add_column("Канал", style="muted")
        table.add_column("Длит.", style="muted")
        for i, e in enumerate(candidates, 1):
            title_e = (e.get("title") or "").strip()
            uploader = e.get("uploader") or ""
            dur = e.get("duration")
            dur_s = format_duration(int(dur)) if isinstance(dur, (int, float)) else "—"
            table.add_row(str(i), title_e, uploader, dur_s)
        console.print(table)

        try:
            idx = IntPrompt.ask("Выбери номер варианта", choices=[str(i) for i in range(1, len(candidates)+1)])
        except Exception:
            return
        chosen = candidates[int(idx)-1]

        # 4) Куда сохраняем
        default_dir = f"{sanitize_filename(track_info['artist'])} - {sanitize_filename(track_info['title'])}"
        target_dir  = choose_target_folder(default_dir, base_music_dir)

        # 5) Скачиваем по выбранному YouTube URL + пишем мету из Spotify
        ok, err = download_audio_from_entry(track_info, chosen, target_dir, cookies_file)
        if ok:
            console.print(f"[bold green]Готово![/bold green] Файл в папке: [dim]{target_dir}[/dim]")
        else:
            console.print(f"[red]Ошибка:[/red] {err}")
        return


    # ---------- Ветка 2: YouTube URL напрямую ----------
    yt_url = Prompt.ask("Вставь ссылку на YouTube-видео").strip()
    if not yt_url:
        console.print("[red]URL пустой[/red]")
        return

    # Покажем индикатор извлечения
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        t = progress.add_task("Извлекаю информацию о видео...", total=None)
        info = yt_get_video_info(yt_url, cookies_file)
        progress.update(t, completed=1)

    if not info:
        console.print("[red]Не удалось извлечь информацию о видео[/red]")
        return

    console.print(f"[green]Видео:[/green] {info['title']}  [dim]({format_duration(info['duration'])})[/dim] • [cyan]{info['uploader']}[/cyan]")

    # Дефолтные теги из YouTube
    artist_guess, title_guess = parse_title_guess(info["title"])
    default_artist = artist_guess or info["uploader"] or "Unknown Artist"
    default_title  = title_guess  or info["title"]     or "Unknown Title"
    default_album  = info["uploader"] or "YouTube"  # альбом берём как канал

    # Разрешим пользователю при желании поправить автора/название вручную
    keep = Confirm.ask("Оставить теги из YouTube как есть?", default=True)
    if keep:
        track_info = {
            "artist": default_artist,
            "title":  default_title,
            "album":  default_album,
            "duration_ms": info["duration"] * 1000,
            "cover_url": info.get("thumbnail"),  # превью видео
        }
    else:
        artist = Prompt.ask("Автор",   default=default_artist or "Unknown Artist")
        title  = Prompt.ask("Название", default=default_title  or "Unknown Title")
        track_info = {
            "artist": artist,
            "title":  title,
            "album":  default_album,
            "duration_ms": info["duration"] * 1000,
            "cover_url": info.get("thumbnail"),
        }

    # Куда сохраняем
    default_dir = f"{sanitize_filename(track_info['artist'])} - {sanitize_filename(track_info['title'])}"
    target_dir  = choose_target_folder(default_dir, base_music_dir)

    # Скачиваем и тэгируем
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        t = progress.add_task("Скачивание и тегирование...", total=None)
        ok, err = download_audio_by_url(info["url"], track_info, target_dir, cookies_file)
        progress.update(t, completed=1)

    if ok:
        console.print(f"[bold green]Готово![/bold green] Файл в папке: [dim]{target_dir}[/dim]")
    else:
        console.print(f"[red]Ошибка:[/red] {err}")
