# -*- coding: utf-8 -*-
"""
 爱听音乐播放器 (双音轨KTV+本地缓存+高阶运镜+K歌字幕)
基于 https://www.2t58.com 接口
依赖: pip install requests pycryptodome PyQt6 pillow mutagen audio-separator[cpu]
"""
import sys, os, requests, binascii, re, tempfile, json, subprocess, random, math, shutil, traceback
import urllib.parse

# ✅ CPU 多线程加速：必须在导入 audio_separator / torch 之前设置
os.environ["OMP_NUM_THREADS"] = str(os.cpu_count() or 8)
os.environ["MKL_NUM_THREADS"] = str(os.cpu_count() or 8)

os.chdir(os.path.dirname(os.path.abspath(__file__)))
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QLineEdit, QPushButton, QListWidget,
                             QListWidgetItem, QLabel, QSlider, QTextEdit,
                             QSplitter, QMessageBox, QScrollArea, QFrame,
                             QFileDialog, QMenu, QDialog, QComboBox, QDialogButtonBox,
                             QProgressBar, QCheckBox, QSpinBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QUrl, QTimer
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtGui import QPixmap, QTextCursor
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineCookieStore

# ============ 接口配置 ============
BASE = "https://www.2t58.com"
PLAY_API = BASE + "/js/play.php"
LRC_API = "https://js.eev3.com/lrc.php"
AES_KEY = binascii.unhexlify("8a5a0271acadce264fbd5492e999a25f19abd19258fbc8252e1d9fcd69adb7bf")

INIT_COOKIES = {"PHPSESSID": "qnoffj1294ob1ocaiim85hi4mc"}
COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.json")
FFMPEG_PATH = "ffmpeg"

UVR_MODEL_DIR = r"F:\卡拉OK\AI-KTV-MV-Maker-main\.uvr5_models"
DEFAULT_UVR_MODEL = "UVR-MDX-NET-Inst_HQ_4.onnx"

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ktv_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": BASE + "/",
})


def _load_cookies():
    try:
        if os.path.exists(COOKIE_FILE):
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                session.cookies.set(k, v, domain=".2t58.com")
    except:
        pass


_load_cookies()
for k, v in INIT_COOKIES.items():
    if k not in session.cookies:
        session.cookies.set(k, v, domain=".2t58.com")


def _save_cookies():
    try:
        data = {}
        for c in session.cookies:
            data[c.name] = c.value
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except:
        pass


CHARTS = [
    ("热门", "/"), ("新歌", "/list/new.html"), ("TOP榜", "/list/top.html"),
    ("DJ舞曲", "/list/djwuqu.html"), ("抖音热歌", "/list/douyin.html"),
    ("网络红歌", "/list/hot.html"), ("粤语金曲", "/list/yytop.html"),
    ("欧美金曲", "/list/ustop.html"), ("古风", "/list/gfhot.html"),
    ("影视金曲", "/list/ystop.html"), ("ACG", "/list/newacg.html"),
    ("说唱", "/list/sctop.html"), ("80后", "/list/blhot.html"),
    ("网红", "/list/wlhot.html"), ("电音", "/list/dytop.html"),
    ("酷我飙升", "/list/kuwo.html"), ("车载", "/list/cztop.html"),
    ("KTV", "/list/ktvtop.html"), ("睡前", "/list/sqtop.html"),
    ("跑步", "/list/jstop.html"),
]


# ============ 核心接口 ============
def decrypt_url(enc_hex):
    ct = binascii.unhexlify(enc_hex)
    pt = unpad(AES.new(AES_KEY, AES.MODE_ECB).decrypt(ct), AES.block_size)
    return pt.decode("utf-8")


def get_song_info(song_id):
    r = session.post(PLAY_API, data={"id": song_id, "type": "music"},
                     headers={"X-Requested-With": "XMLHttpRequest"}, timeout=10)
    j = r.json()
    if j.get("msg") != 1:
        raise Exception(j.get("error", "获取播放地址失败"))
    return {"url": decrypt_url(j["url"]), "pic": j["pic"], "lkid": j["lkid"]}


def get_lyrics(lkid):
    try:
        r = session.get(LRC_API, params={"cid": lkid}, timeout=10)
        return r.json().get("lrc", "")
    except:
        return ""


def parse_lrc(lrc_text):
    result = []
    for line in lrc_text.split("\n"):
        m = re.match(r"^\s*\[(\d+):(\d+(?:\.\d+)?)\](.*)$", line)
        if m:
            t = int(m.group(1)) * 60 + float(m.group(2))
            result.append((t, m.group(3).strip()))
    result.sort(key=lambda x: x[0])
    return result


def _extract_songs(html):
    songs, seen = [], set()
    pat = re.compile(r'href="/song/([^"]+)\.html"[^>]*>([^<]+)</a>')
    for m in pat.finditer(html):
        sid, name = m.group(1), m.group(2).strip()
        if sid not in seen and name:
            seen.add(sid)
            songs.append({"id": sid, "title": name})
    return songs


def _get_with_retry(url, **kw):
    kw.setdefault("timeout", 30)
    last = None
    for i in range(3):
        try:
            return session.get(url, **kw)
        except Exception as e:
            last = e
    raise last


def fetch_url_songs(path):
    r = _get_with_retry(BASE + path)
    r.encoding = r.apparent_encoding or "utf-8"
    if "安全验证" in r.text and len(r.text) < 3000:
        raise Exception("需要人机验证，请在浏览器打开 2t58.com")
    return _extract_songs(r.text)


def search_songs(keyword):
    r = _get_with_retry(f"{BASE}/so/{requests.utils.quote(keyword)}.html")
    r.encoding = r.apparent_encoding or "utf-8"
    return _extract_songs(r.text)


def split_karaoke_text(text, duration):
    if not text:
        return "{\\k0}"
    pattern = r'([\u4e00-\u9fa5]|[a-zA-Z]+|\s+|.)'
    tokens = re.findall(pattern, text)
    tokens = [t for t in tokens if t]
    if not tokens:
        return "{\\k0}"

    per_token = duration / len(tokens)
    res = ""
    accumulated_error = 0
    for i, token in enumerate(tokens):
        c_time = round(per_token + accumulated_error)
        accumulated_error = (per_token + accumulated_error) - c_time
        res += f"{{\\kf{c_time}}}{token}"
    return res


# ============ UVR 音轨分离模块 ============
def separate_audio_with_uvr(input_path, output_dir, model_name, progress_cb=None):
    try:
        if progress_cb: progress_cb(10, "正在准备音频环境...")
        temp_work_dir = tempfile.mkdtemp(prefix="uvr_safe_")
        safe_input = os.path.join(temp_work_dir, "input.wav")

        subprocess.run(
            [FFMPEG_PATH, "-y", "-i", input_path, "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", safe_input],
            capture_output=True, startupinfo=subprocess.STARTUPINFO() if os.name == 'nt' else None)

        from audio_separator.separator import Separator
        if progress_cb: progress_cb(25, f"正在加载模型: {model_name}")

        separator = Separator(log_level=40, model_file_dir=UVR_MODEL_DIR, output_dir=temp_work_dir, output_format="WAV")
        separator.load_model(model_filename=model_name)
        if progress_cb: progress_cb(40, "模型加载完成，正在分离...")

        raw_result = separator.separate(safe_input)

        candidates = []

        def _collect(x):
            if isinstance(x, str):
                candidates.append(os.path.join(temp_work_dir, os.path.basename(x)))
            elif isinstance(x, dict):
                [_collect(v) for v in x.values()]
            elif isinstance(x, (list, tuple)):
                [_collect(v) for v in x]

        _collect(raw_result)

        for f in os.listdir(temp_work_dir):
            full = os.path.join(temp_work_dir, f)
            if os.path.isfile(full): candidates.append(full)

        uniq = list(set(candidates))
        vocals_path, inst_path = None, None
        for p in uniq:
            base = os.path.basename(p).lower()
            if "vocal" in base:
                vocals_path = p
            elif "instrumental" in base:
                inst_path = p

        if not inst_path: raise Exception(f"未找到伴奏文件。候选: {[os.path.basename(p) for p in uniq]}")

        os.makedirs(output_dir, exist_ok=True)
        dest_inst = os.path.join(output_dir, "uvr_inst.wav")
        shutil.move(inst_path, dest_inst)

        dest_vocals = None
        if vocals_path:
            dest_vocals = os.path.join(output_dir, "uvr_vocals.wav")
            shutil.move(vocals_path, dest_vocals)

        shutil.rmtree(temp_work_dir, ignore_errors=True)
        if progress_cb: progress_cb(100, "UVR 分离完成")
        return {"vocals": dest_vocals, "inst": dest_inst}

    except Exception as e:
        raise Exception(f"UVR 分离失败: {e}\n{traceback.format_exc()}")


# ============ 后台线程 ============
class LoadSongsThread(QThread):
    done = pyqtSignal(list)
    fail = pyqtSignal(str)

    def __init__(self, path=None, keyword=None):
        super().__init__()
        self.path = path
        self.keyword = keyword

    def run(self):
        try:
            songs = search_songs(self.keyword) if self.keyword else fetch_url_songs(self.path)
            self.done.emit(songs)
        except Exception as e:
            self.fail.emit(str(e))


class PlayThread(QThread):
    done = pyqtSignal(dict)
    fail = pyqtSignal(str)

    def __init__(self, song):
        super().__init__()
        self.song = song

    def run(self):
        try:
            info = get_song_info(self.song["id"])
            ar = session.get(info["url"], headers={"Referer": "https://www.kuwo.cn/"}, timeout=60, stream=True)
            tmp_raw = tempfile.NamedTemporaryFile(suffix=".m4a", delete=False).name

            total = 0
            with open(tmp_raw, "wb") as f:
                for chunk in ar.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        total += len(chunk)
            if total < 50000:
                if os.path.exists(tmp_raw): os.unlink(tmp_raw)
                raise Exception("音频下载失败(403或不完整)")

            # ✅ 修复闪退：播放前先强制用 FFmpeg 洗掉 HE-AAC 格式，转成标准 MP3 供播放器读取
            tmp_mp3 = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False).name
            subprocess.run([FFMPEG_PATH, "-y", "-i", tmp_raw, "-vn", "-acodec", "libmp3lame", "-b:a", "320k", tmp_mp3],
                           capture_output=True, startupinfo=subprocess.STARTUPINFO() if os.name == 'nt' else None)
            if os.path.exists(tmp_raw): os.unlink(tmp_raw)

            lrc = get_lyrics(info["lkid"])
            pic_data = b""
            try:
                pr = session.get(info["pic"], headers={"Referer": "https://www.kuwo.cn/"}, timeout=10)
                if pr.status_code == 200 and len(pr.content) > 500:
                    pic_data = pr.content
            except:
                pass
            self.done.emit({"song": self.song, "info": info, "path": tmp_mp3, "lrc": lrc, "pic": pic_data})
        except Exception as e:
            self.fail.emit(str(e))


class LocalSearchLrcThread(QThread):
    done = pyqtSignal(str)

    def __init__(self, filepath):
        super().__init__()
        self.filepath = filepath

    def run(self):
        base_name = os.path.splitext(os.path.basename(self.filepath))[0]
        lrc_text = ""
        if " - " in base_name:
            parts = base_name.split(" - ", 1)
            for artist, track in [(parts[0], parts[1]), (parts[1], parts[0])]:
                try:
                    url = f"https://lrclib.net/api/get?artist_name={urllib.parse.quote(artist)}&track_name={urllib.parse.quote(track)}"
                    resp = requests.get(url, timeout=5)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("syncedLyrics"):
                            lrc_text = data["syncedLyrics"]
                            break
                except:
                    pass
        self.done.emit(lrc_text)


class DownloadThread(QThread):
    done = pyqtSignal(str)
    fail = pyqtSignal(str)
    progress = pyqtSignal(int, str)

    def __init__(self, song, save_path, target_format, custom_media=None, transition="fade", mirror=False,
                 dual_audio=False, uvr_model=DEFAULT_UVR_MODEL, v_width=1920, v_height=1080,
                 local_path=None, local_lrc_path=None, local_lrc_text=None):
        super().__init__()
        self.song = song
        self.save_path = save_path
        self.target_format = target_format
        self.custom_media = custom_media
        self.transition = transition
        self.mirror = mirror
        self.dual_audio = dual_audio
        self.uvr_model = uvr_model
        self.v_width = v_width
        self.v_height = v_height
        self.local_path = local_path
        self.local_lrc_path = local_lrc_path
        self.local_lrc_text = local_lrc_text
        self.is_cancelled = False
        self._proc = None

    def cancel(self):
        self.is_cancelled = True
        self.requestInterruption()
        p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
                try:
                    p.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    p.kill()
            except:
                pass

    def _run_proc(self, cmd, timeout=None):
        if self.is_cancelled: return None
        startupinfo = subprocess.STARTUPINFO() if os.name == 'nt' else None
        if startupinfo: startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="ignore", startupinfo=startupinfo)
        self._proc = p
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill();
            out, err = p.communicate()
        finally:
            self._proc = None
        if self.is_cancelled: return None
        return subprocess.CompletedProcess(cmd, p.returncode, out, err)

    def run(self):
        try:
            self.progress.emit(5, "正在准备音轨...")
            info = {"lkid": ""}

            if self.local_path:
                # ✅ 本地文件不管三七二十一，先丢给 FFmpeg 洗成标准 320k MP3，杜绝 HE-AAC/怪视频音轨引发闪退
                tmp_m4a = os.path.join(CACHE_DIR, "current_local_hq.mp3")
                self.progress.emit(8, "正在清洗本地视频/音频...")
                self._run_proc(
                    [FFMPEG_PATH, "-y", "-i", self.local_path, "-vn", "-acodec", "libmp3lame", "-b:a", "320k", tmp_m4a])
                info["lkid"] = ""
                info["pic"] = ""
            else:
                info = get_song_info(self.song["id"])
                if self.is_cancelled: return
                self.progress.emit(8, "正在下载音频到本地缓存...")
                ar = session.get(info["url"], headers={"Referer": "https://www.kuwo.cn/"}, timeout=60, stream=True)
                tmp_raw = os.path.join(CACHE_DIR, "current_raw.m4a")
                total = 0
                with open(tmp_raw, "wb") as f:
                    for chunk in ar.iter_content(chunk_size=8192):
                        if self.is_cancelled: return
                        if chunk:
                            f.write(chunk);
                            total += len(chunk)
                if total < 50000:
                    os.unlink(tmp_raw);
                    raise Exception("下载失败或文件不完整")

                self.progress.emit(10, "正在转码到标准高码率 320kbps...")
                tmp_m4a = os.path.join(CACHE_DIR, "current_audio_hq.mp3")
                tr = self._run_proc(
                    [FFMPEG_PATH, "-y", "-i", tmp_raw, "-vn", "-ar", "44100", "-acodec", "libmp3lame", "-b:a", "320k",
                     tmp_m4a])
                if os.path.exists(tmp_raw): os.unlink(tmp_raw)

            cmd = [FFMPEG_PATH, "-y"]
            temp_files_to_clean = []

            is_audio_only_out = self.target_format in ["mka", "mp3", "wav", "flac", "m4a"]
            is_video_out = self.target_format in ["mp4", "mkv"]

            inst_path = None
            vocals_path = None

            if self.dual_audio:
                self.progress.emit(12, "正在调用 UVR 分离伴奏...")
                try:
                    temp_uvr_out_dir = tempfile.mkdtemp(prefix="uvr_out_", dir=CACHE_DIR)

                    def cb(p, t):
                        if not self.is_cancelled: self.progress.emit(12 + int((p / 100) * 38), t)

                    uvr_result = separate_audio_with_uvr(tmp_m4a, temp_uvr_out_dir, self.uvr_model, progress_cb=cb)
                    if self.is_cancelled: return

                    inst_path = os.path.join(CACHE_DIR, "current_inst.m4a")
                    temp_files_to_clean.append(inst_path)
                    r2 = self._run_proc(
                        [FFMPEG_PATH, "-y", "-i", uvr_result["inst"], "-c:a", "aac", "-b:a", "192k", inst_path])
                    if r2 is None or self.is_cancelled: return

                    if uvr_result.get("vocals"):
                        vocals_path = os.path.join(CACHE_DIR, "current_vocals.m4a")
                        temp_files_to_clean.append(vocals_path)
                        self._run_proc(
                            [FFMPEG_PATH, "-y", "-i", uvr_result["vocals"], "-c:a", "aac", "-b:a", "192k", vocals_path])

                    self.progress.emit(50, "UVR 伴奏提取完成。")
                except Exception as e:
                    if self.is_cancelled: return
                    self.progress.emit(50, f"UVR 分离失败({str(e)[:20]})，尝试备用声道相减...")
                    inst_path = os.path.join(CACHE_DIR, "current_inst.m4a")
                    temp_files_to_clean.append(inst_path)
                    self._run_proc(
                        [FFMPEG_PATH, "-y", "-i", tmp_m4a, "-af", "pan=stereo|c0=c0-c1|c1=c0-c1", "-c:a", "aac", "-b:a",
                         "192k", inst_path])
                    vocals_path = None

            if is_audio_only_out:
                self.progress.emit(75, "正在合成音频文件...")
                if self.dual_audio and inst_path:
                    # ✅ 原唱统一用原曲（带伴奏混音），而不是干声
                    orig_audio = tmp_m4a
                    # ✅ 新增伴奏音量增益 (1.5倍)
                    final_cmd = [FFMPEG_PATH, "-y", "-i", orig_audio, "-i", inst_path,
                                 "-map", "0:a", "-map", "1:a",
                                 "-filter:a:1", "volume=1.5",
                                 "-metadata:s:a:0", "title=原唱", "-metadata:s:a:1", "title=伴奏",
                                 "-c:a", "aac", "-b:a", "192k", self.save_path]
                else:
                    final_cmd = [FFMPEG_PATH, "-y", "-i", tmp_m4a, "-vn", self.save_path]

                res = self._run_proc(final_cmd)
                if res is None or self.is_cancelled: return
                if res.returncode != 0: raise Exception(f"音频合成失败: {(res.stderr or '')[-200:]}")

            elif is_video_out:
                probe_cmd = [FFMPEG_PATH, "-i", tmp_m4a]
                probe_res = self._run_proc(probe_cmd)
                dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", probe_res.stderr or "")
                if not dur_match: raise Exception("无法获取音频时长")
                audio_duration = int(dur_match.group(1)) * 3600 + int(dur_match.group(2)) * 60 + float(
                    dur_match.group(3))

                media_list = self.custom_media
                if media_list == "auto":
                    tmp_pic = os.path.join(CACHE_DIR, "current_pic.jpg")
                    temp_files_to_clean.append(tmp_pic)
                    if self.local_path and os.path.splitext(self.local_path)[1].lower() in ['.mp4', '.mkv', '.avi',
                                                                                            '.mov']:
                        self._run_proc([FFMPEG_PATH, "-y", "-ss", "3", "-i", self.local_path, "-vframes", "1", tmp_pic])
                    elif not self.local_path and info.get("pic"):
                        try:
                            pr = session.get(info["pic"], headers={"Referer": "https://www.kuwo.cn/"}, timeout=10)
                            with open(tmp_pic, "wb") as pf:
                                pf.write(pr.content)
                        except:
                            pass

                    if not os.path.exists(tmp_pic) or os.path.getsize(tmp_pic) == 0:
                        self._run_proc(
                            [FFMPEG_PATH, "-y", "-f", "lavfi", "-i", "color=c=black:s=1920x1080:d=1", "-vframes", "1",
                             tmp_pic])
                    media_list = [tmp_pic]

                if isinstance(media_list, str): media_list = [media_list]
                if not media_list: raise Exception("没有可用的视频素材")

                n_clips = len(media_list)
                fade_dur = 1.5
                clip_dur = (audio_duration + fade_dur * (n_clips - 1)) / n_clips if n_clips > 1 else audio_duration

                tmp_clips = []
                for i, m_path in enumerate(media_list):
                    if self.is_cancelled: return
                    self.progress.emit(50 + int(15 * (i + 1) / n_clips), f"正在处理第 {i + 1}/{n_clips} 个素材...")
                    ext = os.path.splitext(m_path)[1].lower()
                    is_video = ext in ['.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.webm']
                    clip_out = os.path.join(CACHE_DIR, f"clip_{i}.mp4")
                    tmp_clips.append(clip_out)
                    temp_files_to_clean.append(clip_out)

                    if is_video:
                        base_vf = f"scale={self.v_width}:{self.v_height}:force_original_aspect_ratio=decrease,pad={self.v_width}:{self.v_height}:(ow-iw)/2:(oh-ih)/2,fps=25"
                        c_cmd = [FFMPEG_PATH, "-y", "-stream_loop", "-1", "-i", m_path, "-t", str(clip_dur), "-vf",
                                 base_vf,
                                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", clip_out]
                    else:
                        zoom_vf = get_advanced_camera_movement(clip_dur, self.mirror, self.v_width, self.v_height)
                        c_cmd = [FFMPEG_PATH, "-y", "-loop", "1", "-i", m_path, "-t", str(clip_dur), "-vf", zoom_vf,
                                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", clip_out]

                    res = self._run_proc(c_cmd)
                    if res is None or self.is_cancelled: return
                    if res.returncode != 0 or not os.path.exists(clip_out) or os.path.getsize(clip_out) < 1000:
                        raise Exception(f"素材生成失败(第{i + 1}个)")

                self.progress.emit(65, "正在生成 K 歌字幕...")
                sub_filter = ""
                try:
                    lrc_text = ""
                    if self.local_lrc_path and os.path.exists(self.local_lrc_path):
                        with open(self.local_lrc_path, "r", encoding="utf-8", errors="ignore") as f:
                            lrc_text = f.read()
                    elif self.local_path and self.local_lrc_text:
                        lrc_text = self.local_lrc_text
                    elif self.local_path:
                        pass
                    else:
                        lrc_text = get_lyrics(info.get("lkid", ""))

                    if lrc_text.strip():
                        ass_path = os.path.join(CACHE_DIR, "current_sub.ass")
                        temp_files_to_clean.append(ass_path)
                        lrc_lines = parse_lrc(lrc_text)

                        ass_content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {self.v_width}
PlayResY: {self.v_height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,{int(self.v_height / 12.7)},&H00FFFF00,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,2,0,1,3,2,2,30,30,{int(self.v_height / 18)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
                        for i in range(len(lrc_lines)):
                            start_t = lrc_lines[i][0]
                            text = lrc_lines[i][1]
                            if not text: continue
                            end_t = lrc_lines[i + 1][0] if i + 1 < len(lrc_lines) else start_t + 5

                            def fmt_time(t):
                                h = int(t // 3600);
                                m = int((t % 3600) // 60);
                                s = t % 60
                                return f"{h}:{m:02d}:{s:05.2f}"

                            dur_cs = int((end_t - start_t) * 100)
                            k_text = split_karaoke_text(text, dur_cs)
                            ass_content += f"Dialogue: 0,{fmt_time(start_t)},{fmt_time(end_t)},Default,,0,0,0,,{k_text}\n"

                        with open(ass_path, "w", encoding="utf-8") as af:
                            af.write(ass_content)
                        sub_filter = f",ass='{ass_path.replace('\\', '/').replace(':', '\\:')}'"
                except Exception as e:
                    print("歌词生成失败:", e)

                self.progress.emit(75, "正在进行最终合成，请耐心等待...")

                if self.dual_audio and inst_path:
                    # ✅ 原唱统一用原曲（带伴奏混音），而不是干声
                    orig_audio = tmp_m4a
                    if n_clips == 1:
                        # ✅ 对伴奏音轨进行 1.5 倍音量增益 (filter:a:1)
                        final_cmd = [FFMPEG_PATH, "-y", "-i", tmp_clips[0], "-i", orig_audio, "-i", inst_path,
                                     "-vf", f"null{sub_filter}", "-map", "0:v", "-map", "1:a", "-map", "2:a",
                                     "-filter:a:1", "volume=1.5",
                                     "-metadata:s:a:0", "title=原唱", "-metadata:s:a:1", "title=伴奏",
                                     "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                                     "-c:a", "aac", "-b:a", "192k", "-shortest", self.save_path]
                    else:
                        inputs = []
                        for c in tmp_clips: inputs.extend(["-i", c])
                        filter_complex = ""
                        last_v = "0:v";
                        last_dur = 0
                        for i in range(1, n_clips):
                            offset = clip_dur - fade_dur + last_dur
                            out_v = f"v{i}"
                            filter_complex += f"[{last_v}][{i}:v]xfade=transition={self.transition}:duration={fade_dur}:offset={offset}[{out_v}];"
                            last_v = out_v;
                            last_dur = offset
                        filter_complex += f"[{last_v}]null{sub_filter}[final_v]"
                        filter_complex = filter_complex.rstrip(";")

                        final_cmd = [FFMPEG_PATH, "-y"] + inputs + ["-i", orig_audio, "-i", inst_path,
                                                                    "-filter_complex", filter_complex,
                                                                    "-map", "[final_v]", "-map", f"{n_clips}:a", "-map",
                                                                    f"{n_clips + 1}:a",
                                                                    "-filter:a:1", "volume=1.5",
                                                                    "-metadata:s:a:0", "title=原唱", "-metadata:s:a:1",
                                                                    "title=伴奏",
                                                                    "-c:v", "libx264", "-preset", "ultrafast",
                                                                    "-pix_fmt", "yuv420p",
                                                                    "-c:a", "aac", "-b:a", "192k", "-shortest",
                                                                    self.save_path]
                else:
                    if n_clips == 1:
                        final_cmd = [FFMPEG_PATH, "-y", "-i", tmp_clips[0], "-i", tmp_m4a,
                                     "-vf", f"null{sub_filter}",
                                     "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                                     "-c:a", "copy", "-shortest", self.save_path]
                    else:
                        inputs = []
                        for c in tmp_clips: inputs.extend(["-i", c])
                        filter_complex = ""
                        last_v = "0:v";
                        last_dur = 0
                        for i in range(1, n_clips):
                            offset = clip_dur - fade_dur + last_dur
                            out_v = f"v{i}"
                            filter_complex += f"[{last_v}][{i}:v]xfade=transition={self.transition}:duration={fade_dur}:offset={offset}[{out_v}];"
                            last_v = out_v;
                            last_dur = offset
                        filter_complex += f"[{last_v}]null{sub_filter}[final_v]"
                        filter_complex = filter_complex.rstrip(";")
                        final_cmd = [FFMPEG_PATH, "-y"] + inputs + ["-i", tmp_m4a,
                                                                    "-filter_complex", filter_complex,
                                                                    "-map", "[final_v]", "-map", f"{n_clips}:a",
                                                                    "-c:v", "libx264", "-preset", "ultrafast",
                                                                    "-pix_fmt", "yuv420p", "-c:a", "copy", "-shortest",
                                                                    self.save_path]

                result = self._run_proc(final_cmd)
                if result is None or self.is_cancelled: return
                if result.returncode != 0: raise Exception(f"合成失败:\n{(result.stderr or '')[-300:]}")

            for f_path in temp_files_to_clean:
                try:
                    os.unlink(f_path)
                except:
                    pass

            self.progress.emit(100, "✅ 制作完成！")
            self.done.emit(self.save_path)

        except Exception as e:
            if not self.is_cancelled:
                with open(os.path.join(CACHE_DIR, "debug_log.txt"), "w", encoding="utf-8") as lf:
                    lf.write(traceback.format_exc())
                self.fail.emit(str(e))


# ============ 高阶智能运镜算法 ============
def get_advanced_camera_movement(duration, mirror, v_width, v_height):
    mode = random.randint(1, 4)
    total_frames = int(duration * 25)
    w_scaled = int(v_width * 1.5)
    h_scaled = int(v_height * 1.5)
    base_filter = f"scale={w_scaled}:{h_scaled}:force_original_aspect_ratio=increase,crop={w_scaled}:{h_scaled},"

    if mode == 1:
        z_expr = f"min(1+0.25*on/{total_frames},1.25)"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    elif mode == 2:
        z_expr = f"max(1.25-0.25*on/{total_frames},1.0)"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    elif mode == 3:
        z_expr = "1.1"
        x_expr = f"(iw-iw/zoom)*(on/{total_frames})"
        y_expr = "ih/2-(ih/zoom/2)"
    else:
        z_expr = "1.15"
        x_expr = f"(iw-iw/zoom)*(on/{total_frames})"
        y_expr = f"(ih-ih/zoom)*(on/{total_frames})"

    zoompan_filter = f"{base_filter}zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d=1:s={v_width}x{v_height}:fps=25"
    if mirror:
        zoompan_filter += f",split[a][b];[b]vflip[mirror];[a][mirror]vstack=inputs=2,crop={v_width}:{v_height}:0:0"
    return zoompan_filter


# ============ 界面弹窗相关 ============
class FormatDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择下载格式")
        self.setFixedSize(340, 240)
        self.setStyleSheet("""
            QDialog { background: #1a1a2e; color: #e0e0e0; }
            QComboBox { background: #16213e; color: white; border: 1px solid #0f3460; padding: 5px; border-radius: 4px; }
            QLabel { font-size: 13px; }
            QPushButton { background: #e94560; color: white; padding: 6px 15px; border-radius: 4px; border: none; }
            QPushButton:hover { background: #ff5c77; }
        """)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("请选择要保存的格式:"))
        self.combo = QComboBox()
        self.combo.addItems([
            "mkv (双音轨/极佳兼容性)",
            "mp4 (双音轨/日常播放)",
            "mka (纯音频双音轨)",
            "mp4 (视频/单音轨)",
            "m4a (原格式/免转码)",
            "mp3 (兼容性极佳)",
            "wav (无损未压缩)",
            "flac (无损压缩)"
        ])
        layout.addWidget(self.combo)

        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        btn_box.button(QDialogButtonBox.StandardButton.Ok).setText("下一步")
        btn_box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        layout.addWidget(btn_box)

    def get_format(self):
        text = self.combo.currentText()
        if "mka" in text: return "mka"
        return text.split(" ")[0]


class MP4SourceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择视频画面素材与设置")
        self.setFixedSize(440, 640)
        self.setStyleSheet("""
            QDialog { background: #1a1a2e; color: #e0e0e0; }
            QLabel { font-size: 13px; }
            QComboBox, QLineEdit, QSpinBox { background: #16213e; color: white; border: 1px solid #0f3460; padding: 5px; border-radius: 4px; }
            QPushButton { background: #0f3460; color: white; padding: 6px 15px; border-radius: 4px; border: none; }
            QPushButton:hover { background: #163a5f; }
            QCheckBox { font-size: 13px; }
        """)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("1. 生成视频时，使用什么作为画面背景？"))
        self.btn_auto = QPushButton("🌐 使用歌曲自带封面/本地视频截图")
        self.btn_auto.clicked.connect(self._use_auto)
        layout.addWidget(self.btn_auto)

        self.btn_custom = QPushButton("📁 导入多张图片或视频")
        self.btn_custom.clicked.connect(self._use_custom)
        layout.addWidget(self.btn_custom)

        layout.addSpacing(10)
        layout.addWidget(QLabel("2. 选择导出分辨率:"))
        res_layout = QHBoxLayout()
        self.spin_w = QSpinBox()
        self.spin_w.setRange(480, 7680);
        self.spin_w.setValue(1920)
        self.spin_h = QSpinBox()
        self.spin_h.setRange(480, 7680);
        self.spin_h.setValue(1080)
        res_layout.addWidget(self.spin_w);
        res_layout.addWidget(QLabel("x"));
        res_layout.addWidget(self.spin_h)

        self.combo_preset = QComboBox()
        self.combo_preset.addItems([
            "自定义",
            "1920x1080 (横屏 1080P 标准)",
            "2560x1440 (横屏 2K 高清)",
            "3840x2160 (横屏 4K 极清)",
            "1280x720 (横屏 720P 流畅)",
            "1080x1920 (竖屏 1080P 标准)",
            "720x1280 (竖屏 720P 流畅)",
            "2160x3840 (竖屏 4K 极清)",
            "1080x1080 (正方形 朋友圈/INS)",
            "1440x1080 (传统 4:3 标清)"
        ])
        self.combo_preset.currentIndexChanged.connect(self._on_preset_changed)
        res_layout.addWidget(self.combo_preset)
        layout.addLayout(res_layout)

        layout.addSpacing(10)
        layout.addWidget(QLabel("3. 转场特效:"))
        self.combo_transition = QComboBox()
        self.combo_transition.addItems(
            ["fade", "wipeleft", "wiperight", "slideleft", "slideright", "circleopen", "circleclose", "dissolve",
             "pixelize"])
        layout.addWidget(self.combo_transition)

        layout.addSpacing(10)
        layout.addWidget(QLabel("4. 高级设置:"))
        self.chk_mirror = QCheckBox("开启镜面/倒影效果 (仅对图片有效)")
        layout.addWidget(self.chk_mirror)

        layout.addSpacing(10)
        layout.addWidget(QLabel("5. 选择分离模型 (支持 .onnx / .ckpt):"))
        self.combo_model = QComboBox()
        model_files = []
        if os.path.exists(UVR_MODEL_DIR):
            for f in os.listdir(UVR_MODEL_DIR):
                if f.lower().endswith((".onnx", ".ckpt")):
                    model_files.append(f)
        if not model_files: model_files = [DEFAULT_UVR_MODEL]
        model_files.sort(key=lambda x: (0 if "HQ_4" in x else 1, x))
        self.combo_model.addItems(model_files)
        layout.addWidget(self.combo_model)

        layout.addStretch()
        btn_cancel = QPushButton("取消下载")
        btn_cancel.setStyleSheet("background: #e94560;")
        btn_cancel.clicked.connect(self.reject)
        layout.addWidget(btn_cancel)

        self.source = None

    def _on_preset_changed(self, index):
        presets = {
            1: (1920, 1080),
            2: (2560, 1440),
            3: (3840, 2160),
            4: (1280, 720),
            5: (1080, 1920),
            6: (720, 1280),
            7: (2160, 3840),
            8: (1080, 1080),
            9: (1440, 1080)
        }
        if index in presets:
            self.spin_w.setValue(presets[index][0])
            self.spin_h.setValue(presets[index][1])

    def _use_auto(self):
        self.source = "auto"
        self.accept()

    def _use_custom(self):
        file_paths, _ = QFileDialog.getOpenFileNames(self, "选择多个素材", "",
                                                     "媒体文件 (*.jpg *.jpeg *.png *.bmp *.mp4 *.mkv *.avi *.mov *.wmv)")
        if file_paths:
            self.source = file_paths;
            self.accept()

    def get_transition(self):
        return self.combo_transition.currentText()

    def get_mirror(self):
        return self.chk_mirror.isChecked()

    def get_uvr_model(self):
        return self.combo_model.currentText()

    def get_resolution(self):
        return self.spin_w.value(), self.spin_h.value()


class VerifyDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("请完成人机验证")
        self.resize(800, 650)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.web = QWebEngineView()
        self.web.setUrl(QUrl(BASE + "/"))
        layout.addWidget(self.web, 1)
        bottom_bar = QHBoxLayout()
        tip = QLabel("勾选\"我不是人机\"，看到网站首页后，点下面按钮继续")
        tip.setStyleSheet("color: #888; font-size: 12px;")
        bottom_bar.addWidget(tip, 1)
        btn_ok = QPushButton("验证完成，继续")
        btn_ok.setStyleSheet("background: #e94560; color: white; padding: 6px 20px;")
        btn_ok.clicked.connect(self._finish)
        bottom_bar.addWidget(btn_ok)
        layout.addLayout(bottom_bar)
        self.cookies = {}
        self._pending = 0

    def _finish(self):
        self._pending = 2
        try:
            self.web.page().runJavaScript("document.cookie", self._got_js_cookies)
        except:
            self._pending -= 1
        try:
            self.web.page().cookieStore().getAllCookies(self._got_store_cookies)
        except:
            self._pending -= 1
        if self._pending <= 0: self.accept()

    def _got_js_cookies(self, cookie_str):
        try:
            if cookie_str:
                for part in cookie_str.split(";"):
                    if "=" in part:
                        name, value = part.split("=", 1)
                        if name.strip(): self.cookies[name.strip()] = value.strip()
        except:
            pass
        self._check_done()

    def _got_store_cookies(self, cookies):
        try:
            for c in cookies:
                try:
                    name = bytes(c.name()).decode("utf-8", errors="ignore")
                    value = bytes(c.value()).decode("utf-8", errors="ignore")
                    if name and "2t58" in c.domain(): self.cookies[name] = value
                except:
                    pass
        except:
            pass
        self._check_done()

    def _check_done(self):
        self._pending -= 1
        if self._pending <= 0: self.accept()


# ============ 主窗口 ============
class PlayerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("2t58 音乐播放器 (双音轨KTV高阶版)")
        self.resize(960, 740)
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #1a1a2e; color: #e0e0e0; }
            QLineEdit { background: #16213e; border: 1px solid #0f3460; padding: 6px; border-radius: 4px; }
            QPushButton { background: #0f3460; border: none; padding: 5px 12px; border-radius: 4px; font-size: 12px; }
            QPushButton:hover { background: #163a5f; }
            QPushButton#chartBtn { background: #16213e; padding: 4px 10px; }
            QPushButton#chartBtn:hover { background: #e94560; }
            QPushButton#chartActive { background: #e94560; color: white; padding: 4px 10px; }
            QListWidget { background: #16213e; border: none; font-size: 13px; }
            QListWidget::item { padding: 6px; }
            QListWidget::item:selected { background: #e94560; color: white; }
            QSlider::groove:horizontal { height: 6px; background: #0f3460; border-radius: 3px; }
            QSlider::handle:horizontal { width: 14px; height: 14px; margin: -4px 0; background: #e94560; border-radius: 7px; }
            QTextEdit { background: #16213e; border: none; font-size: 14px; color: #a0a0b0; }
            QLabel { color: #e0e0e0; }
            QScrollArea { border: none; background: #1a1a2e; }
            QProgressBar { border: 1px solid #0f3460; border-radius: 4px; text-align: center; background: #16213e; }
            QProgressBar::chunk { background: #e94560; width: 10px; margin: 0.5px; }
        """)

        self.player = QMediaPlayer()
        self.audio = QAudioOutput()
        self.player.setAudioOutput(self.audio)
        self.audio.setVolume(0.5)

        self.playlist = []
        self.current_idx = -1
        self.lrc_lines = []
        self.lrc_idx = 0
        self.temp_path = None
        self.is_playing = False
        self.chart_btns = {}
        self.local_audio_path = None
        self.local_lrc_path = None
        self.cached_local_lrc_text = ""

        self._build_ui()
        self._load_chart("/", "热门")
        QTimer.singleShot(300, lambda: self.search_edit.setFocus())

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        top = QHBoxLayout()
        top.addWidget(QLabel("搜索:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入歌曲名 / 歌手，回车搜索")
        self.search_edit.returnPressed.connect(self._do_search)
        top.addWidget(self.search_edit, 1)

        btn_search = QPushButton("搜索")
        btn_search.clicked.connect(self._do_search)
        top.addWidget(btn_search)

        btn_import = QPushButton("📁 导入本地音视频")
        btn_import.setStyleSheet("background: #2b593f; color: white; padding: 5px 12px; border-radius: 4px;")
        btn_import.clicked.connect(self._import_local_file)
        top.addWidget(btn_import)

        btn_lrc = QPushButton("📄 加载歌词")
        btn_lrc.setStyleSheet("background: #4a3f6b; color: white; padding: 5px 12px; border-radius: 4px;")
        btn_lrc.clicked.connect(self._import_local_lrc)
        top.addWidget(btn_lrc)

        btn_dl_top = QPushButton("⬇ 下载选中")
        btn_dl_top.clicked.connect(self._download_selected)
        top.addWidget(btn_dl_top)
        root.addLayout(top)

        chart_scroll = QScrollArea()
        chart_scroll.setFixedHeight(44)
        chart_scroll.setWidgetResizable(True)
        chart_container = QWidget()
        chart_layout = QHBoxLayout(chart_container)
        chart_layout.setContentsMargins(0, 4, 0, 4)
        chart_layout.setSpacing(4)
        for name, path in CHARTS:
            btn = QPushButton(name)
            btn.setObjectName("chartBtn")
            btn.clicked.connect(lambda checked, p=path, n=name: self._load_chart(p, n))
            chart_layout.addWidget(btn)
            self.chart_btns[name] = btn
        chart_layout.addStretch()
        chart_scroll.setWidget(chart_container)
        root.addWidget(chart_scroll)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        list_toolbar = QHBoxLayout()
        btn_dl_list = QPushButton("⬇ 下载选中歌曲")
        btn_dl_list.setStyleSheet("background: #e94560; color: white; padding: 6px; font-weight: bold;")
        btn_dl_list.clicked.connect(self._download_selected)
        list_toolbar.addWidget(btn_dl_list)
        left_layout.addLayout(list_toolbar)

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(lambda i: self._play_at(self.list_widget.row(i)))
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_list_menu)
        left_layout.addWidget(self.list_widget)
        splitter.addWidget(left_widget)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.cover_label = QLabel()
        self.cover_label.setFixedSize(200, 200)
        self.cover_label.setStyleSheet("background: #16213e; border-radius: 8px;")
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setText("封面")
        right_layout.addWidget(self.cover_label, 0, Qt.AlignmentFlag.AlignHCenter)
        self.song_label = QLabel("未播放")
        self.song_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.song_label.setStyleSheet("font-size: 14px; padding: 4px;")
        right_layout.addWidget(self.song_label)
        self.lrc_view = QTextEdit()
        self.lrc_view.setReadOnly(True)
        right_layout.addWidget(self.lrc_view, 1)
        splitter.addWidget(right)
        splitter.setSizes([380, 560])
        root.addWidget(splitter, 1)

        prog_layout = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(16)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        prog_layout.addWidget(self.progress_bar)
        root.addLayout(prog_layout)

        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        self.btn_loop = QPushButton("🔁")
        self.btn_loop.setFixedSize(36, 36)
        self.btn_loop.setStyleSheet("QPushButton { border: none; font-size: 18px; background: transparent; }")
        self.btn_loop.setCheckable(True)
        bottom.addWidget(self.btn_loop)

        btn_prev = QPushButton("⏮")
        btn_prev.setFixedSize(40, 40)
        btn_prev.setStyleSheet("QPushButton { border: none; font-size: 20px; background: transparent; }")
        btn_prev.clicked.connect(self._prev)
        bottom.addWidget(btn_prev)

        self.btn_play = QPushButton("▶")
        self.btn_play.setFixedSize(56, 56)
        self.btn_play.setStyleSheet("""
            QPushButton { background: #f5c518; border: none; border-radius: 28px; font-size: 22px; color: #1a1a2e; }
            QPushButton:hover { background: #ffd633; }
        """)
        self.btn_play.clicked.connect(self._toggle_play)
        bottom.addWidget(self.btn_play)

        btn_next = QPushButton("⏭")
        btn_next.setFixedSize(40, 40)
        btn_next.setStyleSheet("QPushButton { border: none; font-size: 20px; background: transparent; }")
        btn_next.clicked.connect(self._next)
        bottom.addWidget(btn_next)

        self.time_label = QLabel("00:00 / 00:00")
        bottom.addWidget(self.time_label)
        self.progress = QSlider(Qt.Orientation.Horizontal)
        self.progress.sliderReleased.connect(self._seek)
        bottom.addWidget(self.progress, 1)

        vol_icon = QLabel("🔊")
        vol_icon.setStyleSheet("font-size: 16px;")
        bottom.addWidget(vol_icon)
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setFixedWidth(80)
        self.vol_slider.setValue(50)
        self.vol_slider.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        bottom.addWidget(self.vol_slider)
        root.addLayout(bottom)

        self.timer = QTimer()
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._tick)
        self.player.playbackStateChanged.connect(self._on_state_changed)
        self.player.mediaStatusChanged.connect(self._on_media_status)

    def _load_chart(self, path, name):
        self.search_edit.clear()
        self.song_label.setText(f"加载: {name}...")
        for n, btn in self.chart_btns.items():
            btn.setObjectName("chartActive" if n == name else "chartBtn")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self._pending_path = path
        self._pending_name = name
        self._th = LoadSongsThread(path=path)
        self._th.done.connect(self._fill_list)
        self._th.fail.connect(lambda e: self._on_load_fail(e))
        self._th.start()

    def _on_load_fail(self, err):
        self.song_label.setText("需要人机验证，请在弹出的窗口中勾选验证...")
        try:
            dlg = VerifyDialog(self)
            if dlg.exec():
                for k, v in dlg.cookies.items():
                    session.cookies.set(k, v, domain=".2t58.com")
                _save_cookies()
                self.song_label.setText(f"重新加载: {self._pending_name}...")
                self._th = LoadSongsThread(path=self._pending_path)
                self._th.done.connect(self._fill_list)
                self._th.fail.connect(lambda e: self.song_label.setText(f"加载失败: {e}"))
                self._th.start()
        except Exception as e:
            self.song_label.setText(f"验证出错: {e}")

    def _do_search(self):
        kw = self.search_edit.text().strip()
        if not kw: return
        self.song_label.setText(f"搜索: {kw}...")
        for btn in self.chart_btns.values():
            btn.setObjectName("chartBtn")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self._th = LoadSongsThread(keyword=kw)
        self._th.done.connect(self._fill_list)
        self._th.fail.connect(lambda e: self.song_label.setText(f"搜索失败: {e}"))
        self._th.start()
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def _fill_list(self, songs):
        self.list_widget.clear()
        self.playlist = songs
        for s in songs:
            QListWidgetItem(s["title"], self.list_widget)
        self.song_label.setText(f"共 {len(songs)} 首")
        self.local_audio_path = None
        self.local_lrc_path = None
        self.cached_local_lrc_text = ""

    def _import_local_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择本地歌曲/视频", "",
            "媒体文件 (*.mp4 *.mkv *.avi *.mov *.flv *.wmv *.mp3 *.wav *.flac *.m4a *.aac *.ogg)"
        )
        if not path: return

        self.local_audio_path = path
        title = os.path.basename(path)

        self.list_widget.insertItem(0, f"[本地导入] {title}")
        self.playlist.insert(0, {"id": "local", "title": f"[本地导入] {title}"})

        self.list_widget.setCurrentRow(0)
        self.song_label.setText(f"已导入本地音频: {title}")
        self.lrc_view.clear()
        self.lrc_view.append("正在智能匹配歌词，请稍后...")

        self._search_lrc_thread = LocalSearchLrcThread(path)
        self._search_lrc_thread.done.connect(self._on_local_lrc_searched)
        self._search_lrc_thread.start()

        QMessageBox.information(self, "导入成功",
                                f"已成功导入本地文件:\n{title}\n\n如果右侧没自动显示歌词，请点击上方【📄 加载歌词】手动绑定 LRC。")

    def _on_local_lrc_searched(self, lrc_text):
        if lrc_text:
            self.cached_local_lrc_text = lrc_text
            self.lrc_lines = parse_lrc(lrc_text)
            self.lrc_idx = -1
            self._show_lrc(0)
            self.song_label.setText("✅ 歌词匹配成功，可以开始制作！")
        else:
            self.lrc_view.clear()
            self.lrc_view.append('<p style="color:#e94560;">未自动匹配到歌词。</p>')
            self.lrc_view.append('<p style="color:#888; font-size:12px;">点击上方【📄 加载歌词】导入本地 .lrc 文件。</p>')

    def _import_local_lrc(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择歌词文件", "", "LRC 歌词 (*.lrc);;所有文件 (*.*)")
        if path:
            self.local_lrc_path = path
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                self.cached_local_lrc_text = text
                self.lrc_lines = parse_lrc(text)
                self.lrc_idx = -1
                self._show_lrc(0)
                self.song_label.setText("✅ 已手动绑定本地歌词！")
            except:
                pass
            QMessageBox.information(self, "提示", f"已绑定歌词文件:\n{os.path.basename(path)}")

    def _show_list_menu(self, pos):
        item = self.list_widget.itemAt(pos)
        if not item: return
        idx = self.list_widget.row(item)
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #16213e; color: #e0e0e0; } QMenu::item:selected { background: #e94560; }")
        act_play = menu.addAction("播放")
        act_dl = menu.addAction("下载/转换 (多格式选择)...")
        act = menu.exec(self.list_widget.viewport().mapToGlobal(pos))
        if act == act_play:
            self._play_at(idx)
        elif act == act_dl:
            self._download_song(idx)

    def _download_selected(self):
        idx = self.list_widget.currentRow()
        if idx < 0:
            QMessageBox.information(self, "提示", "请先在左侧列表中选中要下载的歌曲！")
            return
        self._download_song(idx)

    def _download_song(self, idx):
        if idx < 0 or idx >= len(self.playlist): return
        song = self.playlist[idx]
        is_local = (song["id"] == "local")

        fmt_dlg = FormatDialog(self)
        if fmt_dlg.exec() != QDialog.DialogCode.Accepted: return
        target_fmt = fmt_dlg.get_format()

        custom_media = None
        transition = "fade"
        mirror = False
        dual_audio = True
        uvr_model = DEFAULT_UVR_MODEL
        v_width, v_height = 1920, 1080

        if target_fmt in ["mp4", "mkv"]:
            src_dlg = MP4SourceDialog(self)
            if src_dlg.exec() != QDialog.DialogCode.Accepted: return
            custom_media = src_dlg.source
            transition = src_dlg.get_transition()
            mirror = src_dlg.get_mirror()
            uvr_model = src_dlg.get_uvr_model()
            v_width, v_height = src_dlg.get_resolution()
        elif target_fmt in ["m4a", "mp3", "wav", "flac"]:
            dual_audio = False

        safe_name = re.sub(r'[\\/:*?"<>|]', '_', song["title"].replace("[本地导入] ", ""))
        default_name = f"{safe_name}.{target_fmt}"
        filter_str = f"{target_fmt.upper()} 文件 (*.{target_fmt})"

        path, _ = QFileDialog.getSaveFileName(self, "保存歌曲", default_name, filter_str)
        if not path: return
        if not path.lower().endswith(f".{target_fmt}"):
            path += f".{target_fmt}"

        self.song_label.setText(f"准备制作...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

        self._dth = DownloadThread(song, path, target_fmt, custom_media, transition, mirror, dual_audio,
                                   uvr_model=uvr_model, v_width=v_width, v_height=v_height,
                                   local_path=self.local_audio_path if is_local else None,
                                   local_lrc_path=self.local_lrc_path if is_local else None,
                                   local_lrc_text=self.cached_local_lrc_text if is_local else None)
        self._dth.progress.connect(self._on_dl_progress)
        self._dth.done.connect(self._on_dl_done)
        self._dth.fail.connect(self._on_dl_fail)
        self._dth.start()

    def _on_dl_progress(self, val, text):
        self.progress_bar.setValue(val)
        self.song_label.setText(text)

    def _on_dl_done(self, path):
        self.progress_bar.setValue(100)
        self.song_label.setText(f"✅ 制作完成: {path}")
        QTimer.singleShot(3000, lambda: self.progress_bar.setVisible(False))

    def _on_dl_fail(self, err):
        self.progress_bar.setVisible(False)
        self.song_label.setText(f"❌ 制作失败: {err}")

    def _play_at(self, idx):
        if idx < 0 or idx >= len(self.playlist): return
        self.current_idx = idx
        song = self.playlist[idx]

        if song["id"] == "local" and self.local_audio_path:
            self.song_label.setText(song["title"])
            self.player.setSource(QUrl.fromLocalFile(self.local_audio_path))
            self.player.play()
            self.timer.start()
            self.cover_label.setText("本地播放")
            return

        self.song_label.setText(f"加载: {song['title']}")
        self.list_widget.setCurrentRow(idx)
        self._pth = PlayThread(song)
        self._pth.done.connect(self._start_play)
        self._pth.fail.connect(lambda e: self.song_label.setText(f"播放失败: {e}"))
        self._pth.start()

    def _start_play(self, data):
        if self.temp_path and os.path.exists(self.temp_path):
            try:
                os.unlink(self.temp_path)
            except:
                pass
        self.temp_path = data["path"]
        self.lrc_lines = parse_lrc(data["lrc"])
        self.lrc_idx = 0
        self.lrc_view.clear()

        if data["pic"]:
            pix = QPixmap()
            pix.loadFromData(data["pic"])
            if not pix.isNull():
                pix = pix.scaled(200, 200, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
                self.cover_label.setPixmap(pix)
            else:
                self.cover_label.setText("封面")
        else:
            self.cover_label.setText("封面")

        self.song_label.setText(data["song"]["title"])
        self.player.setSource(QUrl.fromLocalFile(self.temp_path))
        self.player.play()
        self.timer.start()

    def _toggle_play(self):
        if self.current_idx < 0:
            if self.playlist: self._play_at(0)
            return
        if self.is_playing:
            self.player.pause()
        else:
            self.player.play()

    def _prev(self):
        if self.current_idx > 0: self._play_index(self.current_idx - 1)

    def _next(self):
        if self.current_idx < len(self.playlist) - 1: self._play_index(self.current_idx + 1)

    def _play_index(self, idx):
        self._play_at(idx)

    def _seek(self):
        dur = self.player.duration()
        if dur > 0: self.player.setPosition(int(self.progress.value() / 100 * dur))

    def _on_state_changed(self, state):
        self.is_playing = (state == QMediaPlayer.PlaybackState.PlayingState)
        self.btn_play.setText("⏸" if self.is_playing else "▶")

    def _on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self.current_idx >= 0:
            if self.current_idx < len(self.playlist) - 1:
                self._play_at(self.current_idx + 1)
        elif status == QMediaPlayer.MediaStatus.InvalidMedia and self.current_idx >= 0:
            if self.current_idx < len(self.playlist) - 1:
                self.song_label.setText(f"跳过无法播放: {self.playlist[self.current_idx]['title']}")
                QTimer.singleShot(500, lambda: self._play_at(self.current_idx + 1))

    def _tick(self):
        dur = self.player.duration()
        pos = self.player.position()
        if dur > 0:
            self.progress.setValue(int(pos / dur * 100))
            self.time_label.setText(f"{self._fmt(pos / 1000)} / {self._fmt(dur / 1000)}")
            self._show_lrc(pos / 1000)

    def _show_lrc(self, cur):
        if not self.lrc_lines: return
        idx = 0
        for i, (t, _) in enumerate(self.lrc_lines):
            if t <= cur:
                idx = i
            else:
                break
        if idx != self.lrc_idx:
            self.lrc_idx = idx
            self.lrc_view.clear()
            start = max(0, idx - 5)
            end = min(len(self.lrc_lines), idx + 8)
            for i in range(start, end):
                line = self.lrc_lines[i][1] or "♪"
                if i == idx:
                    self.lrc_view.append(f'<p style="color:#e94560;font-weight:bold;">{line}</p>')
                else:
                    self.lrc_view.append(f'<p style="color:#888;">{line}</p>')
            self.lrc_view.moveCursor(QTextCursor.MoveOperation.End)

    @staticmethod
    def _fmt(sec):
        m = int(sec) // 60
        s = int(sec) % 60
        return f"{m:02d}:{s:02d}"

    def closeEvent(self, e):
        if hasattr(self, '_dth') and self._dth.isRunning():
            self._dth.cancel()
            self._dth.wait(3000)
        try:
            self.player.stop()
            if self.temp_path and os.path.exists(self.temp_path):
                os.unlink(self.temp_path)
        except:
            pass
        e.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = PlayerWindow()
    win.show()
    sys.exit(app.exec())