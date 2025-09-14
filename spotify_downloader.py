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

# Настройки Spotify API
CLIENT_ID = '77bb678c39844763a230d7452c3b3f5e'
CLIENT_SECRET = '942b953998a4486f91febf938aa06989'

# Добавим глобальную переменную для отладки
DEBUG = True  # Установите False чтобы отключить подробные логи

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name)

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
    
    return best_match

def download_audio(track_info, output_dir, cookies_file=None):
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
    }
    
    # Добавляем cookies, если указаны
    if cookies_file and os.path.exists(cookies_file):
        download_ydl_opts['cookiefile'] = cookies_file
    
    try:
        with youtube_dl.YoutubeDL(download_ydl_opts) as ydl:
            ydl.download([video_url])
        return True
    except Exception as e:
        print(f"Ошибка загрузки {track_info['title']}: {str(e)}")
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
            with urllib.request.urlopen(track_info['cover_url']) as img:
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
    
    if download_audio(track, output_dir, cookies_file):
        file_name = f"{sanitize_filename(track['artist'])} - {sanitize_filename(track['title'])}.mp3"
        file_path = os.path.join(output_dir, file_name)
        if os.path.exists(file_path):
            add_metadata(track, file_path)
            return None
        else:
            return f"{track['artist']} - {track['title']} (файл не создан)"
    else:
        return f"{track['artist']} - {track['title']} (ошибка загрузки)"

def export_youtube_cookies_instructions():
    print("\n" + "="*50)
    print("ИНСТРУКЦИЯ ПО ЭКСПОРТУ COOKIES YOUTUBE:")
    print("1. Установите расширение 'Get cookies.txt' для браузера")
    print("2. Перейдите на YouTube и войдите в аккаунт")
    print("3. Нажмите на расширение и экспортируйте cookies в файл")
    print("4. Сохраните файл как 'cookies.txt' в папке со скриптом")
    print("5. Перезапустите скрипт")
    print("="*50 + "\n")

def main():
    parser = argparse.ArgumentParser(description='Скачивание плейлистов Spotify')
    parser.add_argument('--cookies', help='Путь к файлу cookies YouTube для обхода ограничений', default=None)
    parser.add_argument('--debug', help='Включить подробное логирование', action='store_true')
    args = parser.parse_args()
    
    global DEBUG
    DEBUG = args.debug
    
    # Автоматически ищем cookies.txt в текущей директории
    cookies_file = args.cookies
    if cookies_file is None and os.path.exists('cookies.txt'):
        cookies_file = 'cookies.txt'
        print("Найден файл cookies.txt в текущей директории. Используем его.")
    
    if cookies_file and not os.path.exists(cookies_file):
        print(f"Файл cookies не найден: {cookies_file}")
        export_youtube_cookies_instructions()
        cookies_file = None
    
    print("Spotify Playlist Downloader")
    playlist_url = input("Введите URL плейлиста: ").strip()
    
    print("Получение информации о плейлисте...")
    try:
        playlist_name, owner_name, tracks = get_spotify_playlist_info(playlist_url)
        print(f"Найдено треков в плейлисте: {len(tracks)}")
    except Exception as e:
        print(f"Ошибка получения информации о плейлисте: {str(e)}")
        return
    
    base_dir = f"{playlist_name} ({owner_name})"
    output_dir = base_dir
    counter = 1
    while os.path.exists(output_dir):
        output_dir = f"{base_dir}_{counter}"
        counter += 1
    os.makedirs(output_dir)
    
    # Используем многопоточность для ускорения загрузки
    failed_tracks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        # Подготавливаем аргументы для каждого трека
        args_list = [(idx, track, len(tracks), output_dir, cookies_file) for idx, track in enumerate(tracks, 1)]
        
        # Запускаем обработку треков в нескольких потоках
        results = list(executor.map(process_track, args_list))
        
        # Собираем неудавшиеся загрузки
        for result in results:
            if result:
                failed_tracks.append(result)
    
    if failed_tracks:
        print("\nНе удалось скачать:")
        for track in failed_tracks:
            print(f" - {track}")
        
        # Предлагаем экспорт cookies, если были ошибки возрастного ограничения
        age_restricted = any("confirm your age" in track for track in failed_tracks)
        if age_restricted and not cookies_file:
            export_youtube_cookies_instructions()

if __name__ == "__main__":
    main()