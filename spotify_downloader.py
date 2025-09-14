import os
import re
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import yt_dlp as youtube_dl  # Это правильный импорт
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, error
import urllib.request
import concurrent.futures
import argparse
import time

# Настройки Spotify API
CLIENT_ID = '77bb678c39844763a230d7452c3b3f5e'
CLIENT_SECRET = '942b953998a4486f91febf938aa06989'

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

def find_best_match(track_info, ydl_opts, cookies_file=None):
    query = f"{track_info['artist']} - {track_info['title']}"
    
    # Добавляем cookies если есть
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts['cookiefile'] = cookies_file
    
    # ИСПРАВЛЕНИЕ: Используем правильное имя модуля youtube_dl вместо youtube_dlp
    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
        try:
            # Получаем информацию о первых 10 результатах
            search_results = ydl.extract_info(f"ytsearch7:{query}", download=False)
            
            if not search_results or 'entries' not in search_results:
                return None
                
            best_match = None
            min_duration_diff = float('inf')
            spotify_duration = track_info['duration_ms'] / 1000  # конвертируем в секунды
            
            for entry in search_results['entries']:
                if not entry:
                    continue
                    
                # Сравниваем длительность с погрешностью 15 секунд
                if entry.get('duration'):
                    duration_diff = abs(entry.get('duration', 0) - spotify_duration)
                    
                    # Отдаем предпочтение видео с наиболее близкой длительностью
                    if duration_diff < min_duration_diff and duration_diff <= 15:
                        min_duration_diff = duration_diff
                        best_match = entry
                 
            
            return best_match if best_match else search_results['entries'][0]
            
        except Exception as e:
            print(f"Ошибка поиска для {track_info['title']}: {str(e)}")
            return None

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
        # ИСПРАВЛЕНИЕ: Используем правильное имя модуля youtube_dl вместо youtube_dlp
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
    args = parser.parse_args()
    
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