# hooks/rth_path_ffmpeg.py
import os, sys
# Когда EXE собран --onefile, PyInstaller распаковывает всё во временную папку _MEIPASS.
# Наши ffmpeg.exe/ffprobe.exe мы сложим в подпапку "bin".
base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
bin_dir = os.path.join(base, "bin")
os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
