import os
import re
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import yt_dlp as youtube_dl
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, error
import urllib.request
import concurrent.futures
import argparse
import time
from difflib import SequenceMatcher
import threading
import json
from http.cookiejar import MozillaCookieJar
import selenium
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
import undetected_chromedriver as uc
import tempfile
import shutil
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, IntPrompt, Confirm
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.theme import Theme

# Настройки Spotify API
CLIENT_ID = '77bb678c39844763a230d7452c3b3f5e'
CLIENT_SECRET = '942b953998a4486f91febf938aa06989'

# Глобальная переменная для отладки
DEBUG = False

THEME = Theme({
    "ok": "bold green",
    "warn": "bold yellow",
    "err": "bold red",
    "title": "bold cyan",
    "muted": "dim",
})
console = Console(theme=THEME)

# настройка «по умолчанию» — можно менять через меню
CLI_SETTINGS = {
    "threads": 3,
    "debug": False,
}

# Кэш для уже найденных треков
SEARCH_CACHE = {}

# Блокировка для работы с куки
cookies_lock = threading.Lock()
cookies_last_checked = 0
COOKIES_CHECK_INTERVAL = 1800  # Проверять куки каждые 30 минут

# Глобальный флаг для обновления куки
COOKIES_NEED_REFRESH = False

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name)



def automate_youtube_login(driver, email, password, timeout=30):
    """Устойчивый вход в YouTube/Google-аккаунт"""
    from selenium.common.exceptions import TimeoutException
    driver.get(
        "https://accounts.google.com/signin/v2/identifier"
        "?service=youtube&hl=ru&passive=true&continue=https://www.youtube.com/"
    )
    wait = WebDriverWait(driver, timeout)

    def safe_click_any(selectors):
        for by, sel in selectors:
            try:
                el = wait.until(EC.element_to_be_clickable((by, sel)))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                el.click()
                return True
            except Exception:
                continue
        return False

    def accept_consents():
        # Попытки закрыть разные варианты оверлеев/куки/консенса
        selectors = [
            (By.CSS_SELECTOR, "button#accept-button"),                   # youtube cookie
            (By.CSS_SELECTOR, "button[aria-label*='Accept']"),
            (By.CSS_SELECTOR, "button[aria-label*='Принять']"),
            (By.ID, "introAgreeButton"),                                # старый consent
            (By.XPATH, "//button[contains(., 'I agree')]"),
            (By.XPATH, "//button[contains(., 'Accept all')]"),
            (By.XPATH, "//button[contains(., 'Я принимаю')]"),
            (By.XPATH, "//button[contains(., 'Принять все')]"),
        ]
        safe_click_any(selectors)

    def js_set_value(el, value):
        driver.execute_script("arguments[0].value = arguments[1];", el, value)

    try:
        accept_consents()

        # — Email —
        email_box = wait.until(EC.visibility_of_element_located((By.ID, "identifierId")))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", email_box)
        try:
            email_box.clear()
            email_box.send_keys(email)
        except Exception:
            js_set_value(email_box, email)

        safe_click_any([(By.ID, "identifierNext")])

        # Часто после Next снова возникает оверлей
        accept_consents()

        # — Password —
        passwd_box = wait.until(EC.visibility_of_element_located((By.NAME, "Passwd")))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", passwd_box)
        try:
            passwd_box.clear()
            passwd_box.send_keys(password)
        except Exception:
            js_set_value(passwd_box, password)

        safe_click_any([(By.ID, "passwordNext")])

        # Ждём, пока действительно окажемся на YouTube и появится меню-аватар.
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#avatar-btn")))
        return True
    except TimeoutException as e:
        print(f"Ошибка автоматического входа (таймаут): {e}")
        return False
    except Exception as e:
        print(f"Ошибка автоматического входа: {e}")
        return False


def export_cookies_selenium(driver, cookies_path):
    """Экспортирует cookies из Selenium в Netscape-формат для yt-dlp"""
    try:
        cookies = driver.get_cookies()
        with open(cookies_path, 'w', encoding='utf-8') as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write("# This file was generated by Spotify Downloader\n\n")

            for c in cookies:
                domain = c.get('domain', '')
                # Netscape: домен без ведущей точки -> ставим точку, иначе поддомены не покроет
                if not domain.startswith('.'):
                    domain = '.' + domain

                path = c.get('path', '/')
                secure = 'TRUE' if c.get('secure', False) else 'FALSE'
                # expiry должен быть int или 0
                expiry = c.get('expiry')
                try:
                    expiry = int(expiry)
                except Exception:
                    expiry = 0
                name = c.get('name', '')
                value = c.get('value', '')

                # Флаг includeSubdomains (TRUE/FALSE) — для домена с точкой TRUE
                include_subdomains = 'TRUE' if domain.startswith('.') else 'FALSE'

                # Формат: domain \t includeSubdomains \t path \t secure \t expiry \t name \t value
                f.write(f"{domain}\t{include_subdomains}\t{path}\t{secure}\t{expiry}\t{name}\t{value}\n")
        return True
    except Exception as e:
        print(f"Ошибка экспорта cookies: {e}")
        return False

def setup_selenium_driver():
    """Настраивает и возвращает Selenium WebDriver (uc -> ChromeDriver)"""
    from selenium.webdriver.chrome.service import Service
    from shutil import which

    chrome_options = Options()
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--window-size=1920,1080")

    # 1) Пытаемся запустить undetected-chromedriver
    try:
        driver = uc.Chrome(options=chrome_options)
        return driver
    except Exception as e:
        print(f"Ошибка при настройке undetected-chromedriver: {e}")
        print("Пробуем использовать стандартный ChromeDriver...")

    # 2) Резерв: обычный ChromeDriver (нужен chromedriver в PATH)
    try:
        chromedriver_path = which("chromedriver")
        service = Service(executable_path=chromedriver_path) if chromedriver_path else Service()
        driver = webdriver.Chrome(service=service, options=chrome_options)
        return driver
    except Exception as e2:
        print(f"Ошибка при настройке ChromeDriver: {e2}")
        return None

def automated_cookies_refresh():
    """Полу-ручное обновление cookies через Selenium: ты логинишься сам, мы только сохраняем."""
    print("\n" + "="*70)
    print("Автоматическое обновление cookies (ручной вход)...")
    print("="*70)

    driver = setup_selenium_driver()
    if not driver:
        print("Не удалось настроить Selenium. Пожалуйста, обновите cookies вручную.")
        return False

    try:
        # Открываем YouTube – логин выполняешь сам
        driver.get("https://www.youtube.com/")
        print("\nВ открывшемся окне браузера войди в свой аккаунт YouTube/Google.")
        print("После успешного входа вернись в консоль и нажми Enter — я выгружу cookies.")
        input("Нажми Enter, когда войдёшь... ")

        # Проверим, что вход выполнен (есть аватар)
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#avatar-btn"))
            )
            print("Похоже, ты залогинился. Экспортирую cookies...")
        except Exception:
            print("Не нашёл иконку профиля. Всё равно попробую сохранить cookies...")

        cookies_path = "cookies.txt"
        if export_cookies_selenium(driver, cookies_path):
            print("Cookies успешно обновлены и сохранены в cookies.txt!")
            return True
        else:
            print("Не удалось экспортировать cookies.")
            return False
    finally:
        # НЕ закрываем мгновенно — иногда полезно оставить окно на пару секунд
        try:
            time.sleep(2)
            driver.quit()
        except Exception:
            pass


def check_cookies_validity(cookies_file):
    """Проверяет валидность куки файла"""
    if not os.path.exists(cookies_file):
        return False
    
    try:
        # Пробуем загрузить куки
        cj = MozillaCookieJar(cookies_file)
        cj.load(ignore_discard=True, ignore_expires=True)
        
        # Проверяем наличие основных YouTube куки
        required_cookies = ['SID', 'HSID', 'SSID', 'LOGIN_INFO']
        has_required = any(cookie.name in required_cookies for cookie in cj)
        
        # Проверяем срок действия куки
        now = time.time()
        for cookie in cj:
            if cookie.expires and cookie.expires < now:
                return False
                
        return has_required
    except Exception:
        return False

def refresh_cookies():
    """Просит пользователя обновить куки или делает это автоматически"""
    print("\n" + "="*70)
    print("Обнаружена проблема с куки файлом!")
    print("="*70)
    print("Выберите вариант:")
    print("1. Автоматическое обновление через Selenium (требует учетные данные YouTube)")
    print("2. Ручное обновление (экспорт через расширение браузера)")
    print("3. Продолжить без куки")
    print("="*70)
    
    response = input("Ваш выбор (1/2/3): ").strip()
    
    if response == '1':
        return automated_cookies_refresh()
    elif response == '2':
        print("\nПожалуйста, обновите куки файл вручную:")
        print("1. Убедитесь, что вы вошли в аккаунт YouTube в браузере")
        print("2. Экспортируйте куки с помощью расширения 'Get cookies.txt LOCALLY'")
        print("3. Сохраните файл как 'cookies.txt' в папке со скриптом")
        print("4. Нажмите Enter для продолжения")
        input()
        
        if os.path.exists('cookies.txt') and check_cookies_validity('cookies.txt'):
            print("Новые куки успешно загружены!")
            return True
        else:
            print("Не удалось найти valid куки файл. Продолжаем без куки...")
            return False
    else:
        print("Продолжаем без куки...")
        return False

def get_spotify_playlist_info(playlist_url):
    auth_manager = SpotifyClientCredentials(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    sp = spotipy.Spotify(auth_manager=auth_manager)
    
    playlist = sp.playlist(playlist_url)
    playlist_name = sanitize_filename(playlist['name'])
    owner_name = sanitize_filename(playlist['owner']['display_name'])
    tracks = []
    
    # Получаем все треки с учетом пагинации
    results = sp.playlist_items(playlist_url)
    while results:
        for item in results['items']:
            track = item['track']
            if track:  # Пропускаем удаленные треки
                tracks.append({
                    'artist': ', '.join([artist['name'] for artist in track['artists']]),
                    'title': track['name'],
                    'album': track['album']['name'],
                    'duration_ms': track['duration_ms'],
                    'cover_url': track['album']['images'][0]['url'] if track['album']['images'] else None
                })
        # Проверяем, есть ли еще треки
        if results['next']:
            results = sp.next(results)
        else:
            break
    
    return playlist_name, owner_name, tracks

def similarity(a, b):
    """Вычисляет схожесть между двумя строками"""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def find_best_match(track_info, ydl_opts, cookies_file=None):
    # Проверяем кэш
    cache_key = f"{track_info['artist']} - {track_info['title']}"
    if cache_key in SEARCH_CACHE:
        if DEBUG:
            print(f"Используем кэшированный результат для: {cache_key}")
        return SEARCH_CACHE[cache_key]
    
    # Пробуем разные варианты запросов для улучшения результатов
    queries = [
        f"{track_info['artist']} - {track_info['title']} official audio",
        f"{track_info['artist']} - {track_info['title']}",
        f"{track_info['title']} {track_info['artist']}",
        f"{track_info['title']}"  # Иногда лучше искать только по названию трека
    ]
    
    # Добавляем cookies если есть
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts['cookiefile'] = cookies_file
    
    all_results = []
    
    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
        for query in queries:
            try:
                # Получаем информацию о первых 5 результатах для каждого запроса
                search_results = ydl.extract_info(f"ytsearch5:{query}", download=False)
                
                if search_results and 'entries' in search_results:
                    for entry in search_results['entries']:
                        if entry and entry not in all_results:
                            all_results.append(entry)
            except Exception as e:
                if DEBUG:
                    print(f"Ошибка поиска для запроса '{query}': {str(e)}")
                continue
    
    if not all_results:
        if DEBUG:
            print(f"Не найдено результатов для всех запросов: {track_info['artist']} - {track_info['title']}")
        return None
    
    best_match = None
    best_score = -1
    spotify_duration = track_info['duration_ms'] / 1000  # конвертируем в секунды
    
    if DEBUG:
        print(f"\nПоиск для: {track_info['artist']} - {track_info['title']}")
        print(f"Длительность Spotify: {spotify_duration:.2f} сек")
        print("Найденные варианты:")
    
    for i, entry in enumerate(all_results):
        if not entry:
            continue
            
        entry_duration = entry.get('duration', 0)
        duration_diff = abs(entry_duration - spotify_duration)
        
        # Вычисляем схожесть названия
        title_similarity = similarity(entry['title'], track_info['title'])
        
        # Вычисляем схожесть с артистом (если артист упоминается в названии)
        artist_in_title = similarity(entry['title'], track_info['artist'])
        
        # Вычисляем общий балл
        # Приоритет: схожесть названия > схожесть с артистом > длительность
        score = (title_similarity * 0.6 + artist_in_title * 0.3 + (1 / (1 + duration_diff)) * 0.1)
        
        # Бонус за ключевые слова
        title_lower = entry['title'].lower()
        if any(keyword in title_lower for keyword in ['official', 'original', 'audio', 'lyrics']):
            score += 0.1
        if any(keyword in title_lower for keyword in ['cover', 'remix', 'speed up']):
            score -= 0.2
        
        if DEBUG:
            print(f"{i+1}. {entry['title']} (длительность: {entry_duration} сек, разница: {duration_diff:.2f} сек, score: {score:.3f})")
        
        if score > best_score and duration_diff <= 20:  # Максимальная разница 20 секунд
            best_score = score
            best_match = entry
    
    if DEBUG and best_match:
        print(f"Выбран вариант: {best_match['title']} (score: {best_score:.3f})")
    
    # Сохраняем в кэш
    SEARCH_CACHE[cache_key] = best_match
    
    return best_match

def download_audio(track_info, output_dir, cookies_file=None):
    global COOKIES_NEED_REFRESH
    
    # Настройки для получения информации
    info_ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
    }
    
    # Находим лучшее совпадение по длительности
    best_match = find_best_match(track_info, info_ydl_opts, cookies_file)
    
    if not best_match or 'url' not in best_match:
        print(f"Не удалось найти видео для: {track_info['artist']} - {track_info['title']}")
        return False
    
    video_url = best_match['url']
    
    # Настройки для скачивания
    download_ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(output_dir, f"{sanitize_filename(track_info['artist'])} - {sanitize_filename(track_info['title'])}.%(ext)s"),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30,
        'retries': 3,
        'fragment_retries': 3,
        'skip_unavailable_fragments': True,
        'continuedl': True,
    }
    
    # Добавляем cookies, если указаны
    if cookies_file and os.path.exists(cookies_file):
        download_ydl_opts['cookiefile'] = cookies_file
    
    try:
        with youtube_dl.YoutubeDL(download_ydl_opts) as ydl:
            ydl.download([video_url])
        return True
    except Exception as e:
        error_msg = str(e)
        if "Sign in to confirm your age" in error_msg:
            print(f"Обнаружена ошибка возрастного ограничения для: {track_info['title']}")
            COOKIES_NEED_REFRESH = True
            return "age_restricted"
        else:
            print(f"Ошибка загрузки {track_info['title']}: {error_msg}")
            return False

def add_metadata(track_info, file_path):
    try:
        audio = MP3(file_path, ID3=ID3)
        try:
            audio.add_tags()
        except error:
            pass
        
        audio.tags.add(TIT2(encoding=3, text=track_info['title']))
        audio.tags.add(TPE1(encoding=3, text=track_info['artist']))
        audio.tags.add(TALB(encoding=3, text=track_info['album']))
        
        if track_info['cover_url']:
            # Кэшируем обложки, чтобы не скачивать повторно
            cover_path = os.path.join(os.path.dirname(file_path), "covers")
            os.makedirs(cover_path, exist_ok=True)
            cover_filename = f"{sanitize_filename(track_info['artist'])} - {sanitize_filename(track_info['title'])}.jpg"
            cover_filepath = os.path.join(cover_path, cover_filename)
            
            if not os.path.exists(cover_filepath):
                with urllib.request.urlopen(track_info['cover_url']) as img:
                    with open(cover_filepath, 'wb') as f:
                        f.write(img.read())
            
            with open(cover_filepath, 'rb') as img:
                audio.tags.add(APIC(
                    encoding=3,
                    mime='image/jpeg',
                    type=3,
                    desc='Cover',
                    data=img.read()
                ))
        audio.save()
    except Exception as e:
        print(f"Ошибка добавления метаданных для {track_info['title']}: {str(e)}")

def process_track(args):
    idx, track, total, output_dir, cookies_file = args
    print(f"Скачивание [{idx}/{total}]: {track['artist']} - {track['title']}")
    
    result = download_audio(track, output_dir, cookies_file)
    if result is True:
        file_name = f"{sanitize_filename(track['artist'])} - {sanitize_filename(track['title'])}.mp3"
        file_path = os.path.join(output_dir, file_name)
        if os.path.exists(file_path):
            add_metadata(track, file_path)
            return None
        else:
            return f"{track['artist']} - {track['title']} (файл не создан)"
    elif result == "age_restricted":
        return f"{track['artist']} - {track['title']} (требуются куки)"
    else:
        return f"{track['artist']} - {track['title']} (ошибка загрузки)"

def main():
    # стартовая авто-подхват cookies.txt (как у тебя)
    cookies_file = None
    if not CLI_SETTINGS.get("no_cookies", False):
        if os.path.exists('cookies.txt'):
            cookies_file = 'cookies.txt'
            console.print("[muted]Найден cookies.txt[/muted]")
            if not check_cookies_validity(cookies_file):
                console.print("[warn]cookies.txt недействителен или устарел[/warn]")
                # не заставляем сразу обновлять — можно из меню

    # основной цикл
    while True:
        console.clear()
        print_banner()
        choice = show_main_menu()

        if choice == 1:
            cli_download_playlist(cookies_file)
            wait_enter()
        elif choice == 2:
            ok = automated_cookies_refresh()
            if ok:
                cookies_file = "cookies.txt"
            wait_enter()
        elif choice == 3:
            cli_check_cookies()
            wait_enter()
        elif choice == 4:
            cli_settings()
            wait_enter()
        elif choice == 5:
            cli_clear_cache()
            wait_enter()
        elif choice == 6:
            console.print("\n[ok]Пока![/ok]")
            break



# ==== NEW (CLI) ====
def print_banner():
    console.print(Panel.fit(
        "[title]Spotify Playlist Downloader[/title]\n"
        "[muted]YouTube via yt-dlp · Selenium cookies helper[/muted]",
        title="🎵",
        border_style="title"
    ))

def show_main_menu() -> int:
    table = Table(show_header=True, header_style="title")
    table.add_column("#", justify="right", style="muted")
    table.add_column("Действие", style="ok")
    table.add_row("1", "Скачать плейлист по URL")
    table.add_row("2", "Обновить cookies (ручной вход в YouTube)")
    table.add_row("3", "Проверить cookies.txt")
    table.add_row("4", "Настройки")
    table.add_row("5", "Очистить кеш поиска")
    table.add_row("6", "Выход")
    console.print(table)
    choice = IntPrompt.ask("[title]Выбери пункт[/title]", choices=[str(i) for i in range(1,7)])
    return choice

def wait_enter():
    Prompt.ask("\n[muted]Нажми Enter, чтобы вернуться в меню[/muted]", default="", show_default=False)

# ==== NEW (CLI) ====
def cli_download_playlist(cookies_file: str | None):
    # спросим URL
    playlist_url = Prompt.ask("[title]Вставь URL плейлиста Spotify[/title]").strip()
    if not playlist_url:
        console.print("[err]URL пустой[/err]")
        return

    console.print("[muted]Получаю информацию о плейлисте...[/muted]")
    try:
        playlist_name, owner_name, tracks = get_spotify_playlist_info(playlist_url)
    except Exception as e:
        console.print(f"[err]Ошибка Spotify API:[/err] {e}")
        return

    console.print(f"[ok]Найдено треков:[/ok] {len(tracks)}")
    base_dir = f"{playlist_name} ({owner_name})"
    output_dir = base_dir
    counter = 1
    while os.path.exists(output_dir):
        output_dir = f"{base_dir}_{counter}"
        counter += 1
    os.makedirs(output_dir, exist_ok=True)

    # 1) Поиск кандидатов на YouTube (прогресс)
    info_ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True}
    if cookies_file and os.path.exists(cookies_file):
        info_ydl_opts['cookiefile'] = cookies_file

    console.print("\n[title]Поиск треков на YouTube[/title]")
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        t1 = progress.add_task("Поиск...", total=len(tracks))
        for track in tracks:
            find_best_match(track, info_ydl_opts, cookies_file)
            progress.update(t1, advance=1)

    # 2) Загрузка с прогрессом по количеству треков (не байты, а штуки)
    console.print("\n[title]Загрузка аудио[/title]")
    failed_tracks = []
    age_restricted_tracks = []

    def _worker(args):
        res = process_track(args)
        return res

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        t2 = progress.add_task("Скачивание...", total=len(tracks))

        with concurrent.futures.ThreadPoolExecutor(max_workers=CLI_SETTINGS["threads"]) as executor:
            args_list = [(idx, track, len(tracks), output_dir, cookies_file) for idx, track in enumerate(tracks, 1)]
            for res in executor.map(_worker, args_list):
                if res:
                    if "(требуются куки)" in res:
                        age_restricted_tracks.append(res)
                    else:
                        failed_tracks.append(res)
                progress.update(t2, advance=1)

    # Повторные попытки для age-restricted
    if age_restricted_tracks:
        console.print(f"\n[warn]Треки с возрастным ограничением: {len(age_restricted_tracks)}[/warn]")
        if Confirm.ask("Запустить обновление cookies и попробовать ещё раз?"):
            if refresh_cookies():
                # обновить cookies_file
                cookies_file = 'cookies.txt'
                retry_track_names = [t.split(' (требуются куки)')[0] for t in age_restricted_tracks]
                retry_tracks = [t for t in tracks if f"{t['artist']} - {t['title']}" in retry_track_names]
                age_restricted_tracks = []

                console.print("\n[title]Повторная загрузка[/title]")
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TextColumn("{task.completed}/{task.total}"),
                    TimeElapsedColumn(),
                    console=console,
                ) as progress:
                    t3 = progress.add_task("Скачивание...", total=len(retry_tracks))
                    with concurrent.futures.ThreadPoolExecutor(max_workers=CLI_SETTINGS["threads"]) as executor:
                        args_list = [(idx, track, len(retry_tracks), output_dir, cookies_file) for idx, track in enumerate(retry_tracks, 1)]
                        for res in executor.map(_worker, args_list):
                            if res:
                                if "(требуются куки)" in res:
                                    age_restricted_tracks.append(res)
                                else:
                                    failed_tracks.append(res)
                            progress.update(t3, advance=1)

    # Итоги
    if failed_tracks or age_restricted_tracks:
        console.print("\n[warn]Не удалось скачать:[/warn]")
        for t in failed_tracks + age_restricted_tracks:
            console.print(f" • {t}")
    else:
        console.print("\n[ok]Готово! Все треки скачаны.[/ok]")

    console.print(f"[muted]Папка: {output_dir}[/muted]")


# ==== NEW (CLI) ====
def cli_check_cookies():
    path = "cookies.txt"
    if not os.path.exists(path):
        console.print("[warn]cookies.txt не найден[/warn]")
        return
    ok = check_cookies_validity(path)
    console.print("[ok]cookies.txt валиден[/ok]" if ok else "[warn]cookies.txt недействителен или устарел[/warn]")

def cli_settings():
    console.print("\n[title]Настройки[/title]")
    console.print(f"Текущие: threads={CLI_SETTINGS['threads']}, debug={CLI_SETTINGS['debug']}")
    if Confirm.ask("Изменить число потоков?"):
        CLI_SETTINGS["threads"] = IntPrompt.ask("threads", default=CLI_SETTINGS["threads"])
    if Confirm.ask("Переключить DEBUG?"):
        CLI_SETTINGS["debug"] = not CLI_SETTINGS["debug"]
        global DEBUG
        DEBUG = CLI_SETTINGS["debug"]
    console.print(f"[ok]Сохранено: threads={CLI_SETTINGS['threads']}, debug={CLI_SETTINGS['debug']}[/ok]")

def cli_clear_cache():
    SEARCH_CACHE.clear()
    console.print("[ok]Кеш поиска очищен[/ok]")


if __name__ == "__main__":
    main()