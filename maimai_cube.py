#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
maimai_cube.py — 舞萌（maimai）谱面可视化播放器「立方体版」

idea.txt 的实现：
  * 屏幕中间是一个正方体 ABCD-A1B1C1D1；
  * 正方体中心就是音符向外飞出的起点；
  * 八个外键（1~8）与触摸区 A1~A8 位置完全重合；
  * 立方体顶点按规则映射：A→A1→外键1，B→A2→外键2，C→A3→外键3，D→A4→外键4，
    A1→A5→外键5，B1→A6→外键6，C1→A7→外键7，D1→A8→外键8；
  * 其余触摸区（B / C / D / E）按方位角就近映射到同一个外键（见 KEY_OF_ZONE）。

用法：
    python maimai_cube.py                       # 自动播放（看谱，默认）
    python maimai_cube.py --play                # 键盘游玩
    python maimai_cube.py --selftest            # 仅解析谱面并输出统计
    python maimai_cube.py --preview out.png --time 12.5   # 渲染单帧 PNG
"""
from __future__ import annotations

import argparse
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

try:                              # Pillow 只用于贴图/曲绘/离线预览，没有也能跑
    from PIL import Image as _PILImage  # noqa: F401
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

# --------------------------------------------------------------------------
# 路径与常量
# --------------------------------------------------------------------------
BASE = os.path.dirname(os.path.abspath(__file__))
CHART_DIR = os.path.join(BASE, "11663_系ぎて")
SFX_DIR = os.path.join(BASE, "maimai音效")
MAIDATA = os.path.join(CHART_DIR, "maidata.txt")
TRACK = os.path.join(CHART_DIR, "track.mp3")
BGIMG = os.path.join(CHART_DIR, "bg.png")

DIFFICULTY = 6            # 谱面写在 &inote_6（readme 说明：344 行以后的部分）

# 颜色
C_BG = "#05060f"
C_TILE = "#151547"
C_TILE_EDGE = "#33337f"
C_TILE_HOT = "#2b2b8f"
C_TEXT = "#ffffff"
C_DIM = "#8d94c8"
C_CUBE = "#8ea2ff"
C_TAP = "#ff5f8f"
C_HOLD = "#ffb347"
C_SLIDE = "#35d0ff"
C_BREAK = "#ffd700"
C_EX = "#ff2d55"
C_TOUCH = "#7dffb0"
C_TOUCH_HOLD = "#38f5c8"

KEY_COLORS = ["#ff6b6b", "#ffa94d", "#ffd43b", "#a9e34b",
              "#38d9a9", "#4dabf7", "#9775fa", "#f783ac"]


# --------------------------------------------------------------------------
# 触摸区几何（与 舞萌触摸区图解.xlsx 完全一致，半径以盘面半径 R 为单位）
# --------------------------------------------------------------------------
A_HALF = 14.0          # A 花瓣半角
PETAL_KNEE = 0.062     # 花瓣内沿朝圆心凹陷的幅度

ZONES: dict[str, dict] = {}
for _k in range(8):
    ZONES[f"A{_k + 1}"] = dict(kind="A", theta=22.5 + 45 * _k, r=0.80, k=_k)
    ZONES[f"B{_k + 1}"] = dict(kind="B", theta=22.5 + 45 * _k, r=0.405, k=_k)
    ZONES[f"D{_k + 1}"] = dict(kind="D", theta=45 * _k, r=0.80, k=_k)
    ZONES[f"E{_k + 1}"] = dict(kind="E", theta=45 * _k, r=0.575, k=_k)
ZONES["C1"] = dict(kind="C", theta=90.0, r=0.095, k=0)
ZONES["C2"] = dict(kind="C", theta=270.0, r=0.095, k=0)
ZONES["C"] = dict(kind="C", theta=0.0, r=0.0, k=0)      # 中心触摸（谱面里的 C）

# 外键（1~8）与 A1~A8 同位置
KEY_TO_ZONE = {i + 1: f"A{i + 1}" for i in range(8)}
# 立方体顶点 → 外键
CORNER_KEY = {"A": 1, "B": 2, "C": 3, "D": 4,
              "A1": 5, "B1": 6, "C1": 7, "D1": 8}

# 其余触摸区就近映射到外键（idea.txt：剩下的触摸区自行映射）
def _nearest_key(theta: float) -> int:
    best, bd = 1, 1e9
    for k in range(1, 9):
        d = abs((theta - (22.5 + 45 * (k - 1)) + 180) % 360 - 180)
        if d < bd:
            best, bd = k, d
    return best


KEY_OF_ZONE = {z: _nearest_key(ZONES[z]["theta"]) for z in ZONES}
for _k in range(8):                      # A 区与外键严格同号
    KEY_OF_ZONE[f"A{_k + 1}"] = _k + 1
KEY_OF_ZONE["C1"] = 3                    # 中心左右各归到东西两侧的外键
KEY_OF_ZONE["C2"] = 7
KEY_OF_ZONE["C"] = 1


def pol2xy(r: float, deg: float) -> tuple[float, float]:
    a = math.radians(deg)
    return (r * math.sin(a), -r * math.cos(a))


def zone_xy(name: str) -> tuple[float, float]:
    z = ZONES[name]
    return pol2xy(z["r"], z["theta"])


def zone_screen(name: str) -> tuple[float, float]:
    x, y = zone_xy(name)
    return (CX + x * R, CY + y * R)


def zone_key(name: str) -> int:
    if name in KEY_OF_ZONE:
        return KEY_OF_ZONE[name]
    return _nearest_key(ZONES[name]["theta"]) if name in ZONES else 1


# --------------------------------------------------------------------------
# 谱面解析（simai 子集，覆盖本谱全部写法）
# --------------------------------------------------------------------------
@dataclass
class Note:
    t: float                      # 判定时刻（秒）
    kind: str                     # tap / hold / slide / touch / touchhold
    zones: list[str]              # 起始触摸区（同押时会有多个）
    end: float = 0.0              # 结束时刻（hold / slide）
    path: list[str] = field(default_factory=list)   # slide 经过的触摸区
    is_break: bool = False
    is_ex: bool = False
    group: int = 0                # 同押标记
    hit: bool = False
    judged: str = ""
    judge_t: float = 0.0

    @property
    def key(self) -> int:
        return zone_key(self.zones[0]) if self.zones else 1


def _dur_seconds(txt: str, bpm: float) -> tuple[float, float]:
    """[4:1] / [8:3] / [0.6757##4.2255] → (时长秒, 是否精确秒)"""
    txt = txt.strip()
    if "##" in txt:
        a, b = txt.split("##", 1)
        try:
            return float(a), float(b)
        except ValueError:
            return 0.5, 0.5
    if ":" in txt:
        a, b = txt.split(":", 1)
        try:
            beats = float(a) / max(float(b), 1e-6)
        except ValueError:
            beats = 1.0
        return beats * 60.0 / bpm, 0.0
    try:
        return float(txt) * 60.0 / bpm, 0.0
    except ValueError:
        return 0.0, 0.0


def _zone_of_token(tok: str) -> str:
    """A1/B3/C1/C/E5/D8 → 触摸区名"""
    if tok in ("C", "C1", "C2"):
        return "C" if tok == "C" else tok
    if tok in ZONES:
        return tok
    return "A1"


def _parse_note(body: str, t: float, bpm: float) -> list[Note]:
    """解析一个不含 '/' 的音符片段（可含 * 分叉的 each 滑条）"""
    out: list[Note] = []
    segs = body.split("*")
    cur_head: str | None = None
    cur_pos: str | None = None
    for si, seg in enumerate(segs):
        seg = seg.strip()
        if not seg:
            continue
        i = 0
        head: str | None = None
        if seg[0] in "ABCDE":
            j = 1
            while j < len(seg) and seg[j].isdigit():
                j += 1
            head = seg[:j]
            i = j
        elif seg[0].isdigit():
            head = seg[0]
            i = 1
        if head is not None:
            cur_head = head
            cur_pos = head
        elif cur_pos is not None and seg[0] in "-<>^vVpqszw":
            head = cur_pos

        flags, dur, waypoints = set(), 0.0, []
        while i < len(seg):
            c = seg[i]
            if c == "b":
                flags.add("b"); i += 1
            elif c == "x":
                flags.add("x"); i += 1
            elif c == "f":
                flags.add("f"); i += 1
            elif c == "h":
                i += 1
                if i < len(seg) and seg[i] == "[":
                    j = seg.find("]", i)
                    d, _ = _dur_seconds(seg[i + 1:j], bpm)
                    dur = max(dur, d)
                    i = j + 1
            elif c == "[":
                j = seg.find("]", i)
                d, _ = _dur_seconds(seg[i + 1:j], bpm)
                dur = max(dur, d)
                i = j + 1
            elif c in "-<>^vVpqszw":
                i += 1
                digits = ""
                while i < len(seg) and seg[i].isdigit():
                    digits += seg[i]; i += 1
                for d in digits:
                    waypoints.append(d)
            else:
                i += 1

        if head is None:
            continue
        is_touch = head[0] in "ABCDE"
        hz = head if is_touch else f"A{head}"
        if hz not in ZONES:          # 谱面末尾的孤立字母等无效记号
            continue
        if waypoints:
            path = [f"A{d}" for d in waypoints]
            out.append(Note(t=t, kind="slide", zones=[hz], end=t + max(dur, 0.25),
                            path=path, is_break="b" in flags, is_ex="x" in flags,
                            group=si))
            cur_pos = f"A{waypoints[-1]}"
        elif is_touch:
            kind = "touchhold" if dur > 0 else "touch"
            out.append(Note(t=t, kind=kind, zones=[hz], end=t + max(dur, 0.0),
                            is_break="b" in flags, is_ex="x" in flags, group=si))
        else:
            kind = "hold" if dur > 0 else "tap"
            out.append(Note(t=t, kind=kind, zones=[hz], end=t + max(dur, 0.0),
                            is_break="b" in flags, is_ex="x" in flags, group=si))
    return out


def parse_chart(path: str = MAIDATA, difficulty: int = DIFFICULTY):
    """返回 (notes, 总时长, 标题, 艺术家, 等级)"""
    raw = open(path, "rb").read()
    for enc in ("utf-8", "utf-8-sig", "gbk"):
        try:
            text = raw.decode(enc); break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")

    title = artist = level = ""
    for line in text.splitlines():
        if line.startswith("&title="):
            title = line[7:]
        elif line.startswith("&artist="):
            artist = line[8:]
        elif line.startswith(f"&lv_{difficulty}="):
            level = line.split("=", 1)[1]
    # 谱面内容从 &inote_N= 一直延续到文件末尾（readme：344 行以后的部分）
    _key = f"&inote_{difficulty}="
    _pos = text.find(_key)
    body = text[_pos + len(_key):] if _pos >= 0 else ""

    global CHART_BPM
    notes: list[Note] = []
    bpm, div, t, group = 120.0, 4, 0.0, 0
    first_bpm = None
    for chunk in body.split(","):
        s = chunk.strip()
        while True:
            if s.startswith("("):
                j = s.find(")")
                if 0 < j < 9:
                    try:
                        bpm = float(s[1:j])
                    except ValueError:
                        pass
                    if first_bpm is None:
                        first_bpm = bpm
                    s = s[j + 1:].strip()
                    continue
            if s.startswith("{"):
                j = s.find("}")
                if 0 < j < 5:
                    try:
                        div = max(int(s[1:j]), 1)
                    except ValueError:
                        pass
                    s = s[j + 1:].strip()
                    continue
            break
        if s:
            for part in s.split("/"):
                part = part.strip()
                if part:
                    group += 1
                    notes.extend(_parse_note(part, t, bpm))
        t += (240.0 / bpm) / div

    notes.sort(key=lambda n: (n.t, n.key))
    CHART_BPM = first_bpm or 120.0
    return notes, t, title, artist, level


# --------------------------------------------------------------------------
# 音频（Windows MCI：mp3 与 wav 均可，多路混音）
# --------------------------------------------------------------------------
class Audio:
    SFX = {
        "tap": "正解音.wav",
        "perfect": "perfect判定音.wav",
        "break": "绝赞(BREAK)判定音.wav",
        "ex": "EX音符判定音.wav",
        "touch": "touch判定音.wav",
        "touch_hold": "touchhold蓄力音.wav",
        "slide": "普通星星滑动音(SLIDE).wav",
        "slide_break": "绝赞星星滑动音(BREAKSLIDE).wav",
        "firework": "烟花touch判定音.wav",
        "metronome": "预备拍节拍器.wav",
    }
    SLOTS = 4

    def __init__(self, enabled: bool = True):
        self.ok = False
        self.slots: dict[str, list[tuple[int, str]]] = {}
        self._rr: dict[str, int] = {}
        # MCI 单次调用约 5~6ms，放在主线程会直接吃掉帧预算，这里改成后台线程播放
        self._q: "queue.Queue[str]" = queue.Queue(maxsize=64)
        self._worker = None
        if not enabled or sys.platform != "win32":
            return
        try:
            import ctypes
            self._mci = ctypes.windll.winmm.mciSendStringW
            self.ok = True
        except Exception:
            self.ok = False
            return
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _run(self) -> None:
        while True:
            name = self._q.get()
            if name is None:
                return
            slots = self.slots.get(name)
            if not slots:
                continue
            i = self._rr[name] % len(slots)
            self._rr[name] = i + 1
            self._send(f"play {slots[i][1]} from 0")

    def _send(self, cmd: str) -> int:
        try:
            return self._mci(cmd, None, 0, 0)
        except Exception:
            return -1

    def open_music(self, path: str) -> bool:
        """优先直接交给 MCI（老系统支持 mp3）；失败则用 ffmpeg 转 wav 再播。"""
        if not self.ok or not os.path.exists(path):
            return False
        self._send("close mm_music")
        if self._send(f'open "{path}" type mpegvideo alias mm_music') == 0:
            self._send("set mm_music time format milliseconds")
            return True
        wav = self._mp3_to_wav(path)
        if wav and self._send(f'open "{wav}" type waveaudio alias mm_music') == 0:
            self._send("set mm_music time format milliseconds")
            return True
        return False

    def _mp3_to_wav(self, path: str) -> str | None:
        import shutil, subprocess, tempfile, hashlib
        if not path.lower().endswith(".mp3"):
            return None
        tag = hashlib.md5(os.path.abspath(path).encode("utf-8")).hexdigest()[:8]
        out = os.path.join(tempfile.gettempdir(), f"maimai_cube_{tag}.wav")
        try:
            if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(path):
                return out
        except OSError:
            pass
        ff = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
        if not os.path.exists(ff):
            print("[音频] 未找到 ffmpeg，无法把 mp3 转成 wav，音乐将静音（音效正常）。")
            return None
        print("[音频] 首次运行：正在把 track.mp3 转换为 wav（约需数秒）…")
        try:
            subprocess.run([ff, "-y", "-i", path, "-ac", "2", "-ar", "44100",
                            "-loglevel", "error", out], check=True,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
            return out
        except Exception as exc:
            print("[音频] 转换失败：", exc)
            return None

    def load_sfx(self, folder: str) -> None:
        if not self.ok:
            return
        for name, fname in self.SFX.items():
            path = os.path.join(folder, fname)
            if not os.path.exists(path):
                continue
            slots = []
            for i in range(self.SLOTS):
                alias = f"mm_{name}_{i}"
                self._send(f"close {alias}")
                if self._send(f'open "{path}" type waveaudio alias {alias}') == 0:
                    slots.append((i, alias))
            if slots:
                self.slots[name] = slots
                self._rr[name] = 0

    def play_music(self, from_ms: int = 0) -> None:
        if not self.ok:
            return
        self._send(f"seek mm_music to {int(from_ms)}")
        self._send("play mm_music")

    def stop_music(self) -> None:
        if self.ok:
            self._send("stop mm_music")

    def sfx(self, name: str) -> None:
        if not self.ok or name not in self.slots:
            return
        try:
            self._q.put_nowait(name)     # 队列满就丢，绝不让主线程等
        except queue.Full:
            pass

    def close(self) -> None:
        if not self.ok:
            return
        self._send("close mm_music")
        for slots in self.slots.values():
            for _, alias in slots:
                self._send(f"close {alias}")


# --------------------------------------------------------------------------
# 音符贴图（音符素材/）
# --------------------------------------------------------------------------
SPRITE_DIR = os.path.join(BASE, "音符素材")
SPRITE_FILES = {
    "tap": "Tap_00.png", "tap_each": "Tap_Each_00.png", "break": "Break_01.png",
    "hold": "Hold_00.png", "hold_ex": "Hold_EX_00.png",
    "star": "Star_01.png", "star_each": "Star_Each_00.png", "star_break": "BreakStar_00.png",
    "perfect": "UI_GAM_Perfect.png",
}


class Sprites:
    """按 (名字, 尺寸, 角度) 缓存放缩/旋转后的贴图，PIL 与 tkinter 各一份。"""

    def __init__(self, folder: str):
        self.folder = folder
        self.ok = HAVE_PIL and os.path.isdir(folder)
        self._pil: dict = {}
        self._tk: dict = {}
        self._raw: dict = {}

    def _open(self, name):
        if name not in self._raw:
            from PIL import Image
            path = os.path.join(self.folder, SPRITE_FILES.get(name, name))
            try:
                self._raw[name] = Image.open(path).convert("RGBA")
            except Exception:
                self._raw[name] = None
        return self._raw[name]

    def pil(self, name, size, angle=0.0):
        if not self.ok:
            return None
        from PIL import Image
        key = (name, int(size), int(round(angle / 11.25)) % 32)
        if key in self._pil:
            return self._pil[key]
        im = self._open(name)
        if im is None:
            return None
        w, h = im.size
        f = size / max(w, h)
        im = im.resize((max(1, int(w * f)), max(1, int(h * f))), Image.LANCZOS)
        deg = key[2] * 11.25
        if deg:
            im = im.rotate(-deg, expand=True, resample=Image.BICUBIC)
        self._pil[key] = im
        return im

    def tk(self, name, size, angle=0.0):
        if not self.ok:
            return None
        from PIL import ImageTk
        key = (name, int(size), int(round(angle / 11.25)) % 32)
        if key in self._tk:
            return self._tk[key]
        im = self.pil(name, size, angle)
        if im is None:
            return None
        photo = ImageTk.PhotoImage(im)
        self._tk[key] = photo
        return photo


SPRITES = Sprites(SPRITE_DIR)


class VideoBG:
    """用 ffmpeg 解码 pv.mp4 当背景：后台线程读帧，主线程只做一次 paste。"""

    def __init__(self, path: str, size: int, fps: int = 30, time_fn=None, dark: float = -0.34):
        self.ok = False
        self.path = path
        self.size = size
        self.fps = fps
        self.time_fn = time_fn or (lambda: 0.0)
        self.dark = dark
        self.proc = None
        self.frame = None
        self.idx = -1
        self.lock = threading.Lock()
        self.running = False
        if not (HAVE_PIL and os.path.exists(path)):
            return
        import shutil
        self.ff = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
        if not os.path.exists(self.ff):
            print("[视频] 未找到 ffmpeg，背景改用曲绘。")
            return
        self.ok = True

    def _detect_crop(self) -> str:
        """用 cropdetect 找出画面内容区域，去掉上下/左右黑边"""
        try:
            cmd = [self.ff, "-v", "info", "-ss", "25", "-i", self.path, "-t", "6",
                   "-vf", "cropdetect=24:2:0", "-f", "null", "-"]
            r = subprocess.run(cmd, capture_output=True, timeout=25,
                               creationflags=0x08000000 if sys.platform == "win32" else 0)
            err = (r.stderr or b"").decode("utf-8", "replace")
            hits = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", err)
            if not hits:
                return ""
            w, h, x, y = map(int, hits[-1])
            if w < 160 or h < 160:
                return ""
            if w > h:                       # 取中间的正方形，避免拉伸变形
                x += (w - h) // 2
                w = h
            elif h > w:
                y += (h - w) // 2
                h = w
            return f"crop={w}:{h}:{x}:{y},"
        except Exception:
            return ""

    def start(self) -> bool:
        if not self.ok:
            return False
        crop = self._detect_crop()
        vf = (f"{crop}scale={self.size}:{self.size}:flags=fast_bilinear,"
              f"eq=brightness={self.dark}:saturation=0.65,gblur=sigma=1.4")
        cmd = [self.ff, "-v", "error", "-i", self.path, "-an", "-vf", vf,
               "-r", str(self.fps), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL,
                                         creationflags=0x08000000 if sys.platform == "win32" else 0)
        except Exception as exc:
            print("[视频] 启动 ffmpeg 失败：", exc)
            self.ok = False
            return False
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()
        return True

    def _loop(self) -> None:
        from PIL import Image
        n = self.size * self.size * 3
        i = 0
        while self.running:
            # 跟着谱面时间走，避免解码跑飞（暂停时自然停住）
            while self.running and i / self.fps > self.time_fn() + 0.25:
                time.sleep(0.004)
            try:
                buf = self.proc.stdout.read(n)
            except Exception:
                break
            if not buf or len(buf) < n:
                break
            im = Image.frombytes("RGB", (self.size, self.size), buf)
            with self.lock:
                self.frame = im
                self.idx = i
            i += 1

    def take(self):
        with self.lock:
            return self.idx, self.frame

    def stop(self) -> None:
        self.running = False
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass


# --------------------------------------------------------------------------
# 绘制后端：tkinter Canvas 与 PIL 共用同一套绘制指令
# --------------------------------------------------------------------------
class TkBackend:
    def __init__(self, canvas, tag: str = "all"):
        self.c = canvas
        self.tag = tag

    def _t(self, **kw):
        if self.tag:
            kw["tags"] = self.tag
        return kw

    def clear(self):
        if self.tag and self.tag != "all":
            self.c.delete(self.tag)

    def poly(self, pts, fill=None, outline=None, width=1):
        flat = [v for p in pts for v in p]
        self.c.create_polygon(flat, fill=fill or "", outline=outline or "",
                              width=width, **self._t())

    def line(self, pts, fill="#fff", width=1, dash=None):
        if len(pts) < 2:
            return
        flat = [v for p in pts for v in p]
        kw = self._t()
        if dash:
            kw["dash"] = dash
        self.c.create_line(flat, fill=fill, width=width, capstyle="round", **kw)

    def ellipse(self, cx, cy, rx, ry=None, fill=None, outline=None, width=1):
        ry = rx if ry is None else ry
        self.c.create_oval(cx - rx, cy - ry, cx + rx, cy + ry,
                           fill=fill or "", outline=outline or "", width=width, **self._t())

    def text(self, x, y, s, size=14, fill="#fff", anchor="center", bold=False):
        self.c.create_text(x, y, text=s, fill=fill, anchor=anchor,
                           font=("Microsoft YaHei UI", max(int(size * 0.75), 6),
                                 "bold" if bold else "normal"), **self._t())

    def image(self, img, x, y):
        self.c.create_image(x, y, image=img, anchor="nw", **self._t())

    def sprite(self, name, cx, cy, size, angle=0.0):
        try:
            photo = SPRITES.tk(name, size, angle)
        except Exception:
            return False
        if photo is not None:
            self.c.create_image(cx, cy, image=photo, **self._t())
            return True
        return False


class PilBackend:
    def __init__(self, size, bg_color=(5, 6, 15)):
        from PIL import Image, ImageDraw
        self.img = Image.new("RGB", size, bg_color)
        self.d = ImageDraw.Draw(self.img)
        self._font_cache = {}
        self.size = size

    def _font(self, px, bold):
        from PIL import ImageFont
        key = (px, bold)
        if key in self._font_cache:
            return self._font_cache[key]
        names = ["msyhbd.ttc" if bold else "msyh.ttc", "arialbd.ttf" if bold else "arial.ttf"]
        f = None
        for n in names:
            for base in (r"C:\Windows\Fonts\\", ""):
                try:
                    f = ImageFont.truetype(base + n, px); break
                except Exception:
                    f = None
            if f:
                break
        if f is None:
            f = ImageFont.load_default()
        self._font_cache[key] = f
        return f

    def clear(self):
        pass

    def poly(self, pts, fill=None, outline=None, width=1):
        fill = fill or None
        outline = outline or None
        if len(pts) >= 3:
            self.d.polygon(pts, fill=fill, outline=outline)
            if outline and width > 1:
                self.d.line(list(pts) + [pts[0]], fill=outline, width=width, joint="curve")

    def line(self, pts, fill="#fff", width=1, dash=None):
        if len(pts) >= 2:
            self.d.line(pts, fill=fill or None, width=max(int(width), 1), joint="curve")

    def ellipse(self, cx, cy, rx, ry=None, fill=None, outline=None, width=1):
        ry = rx if ry is None else ry
        box = [cx - rx, cy - ry, cx + rx, cy + ry]
        self.d.ellipse(box, fill=fill or None, outline=outline or None,
                       width=max(int(width), 1))

    def text(self, x, y, s, size=14, fill="#fff", anchor="center", bold=False):
        f = self._font(max(int(size), 8), bold)
        amap = {"center": "mm", "w": "lm", "e": "rm", "n": "ma", "s": "ms",
                "nw": "la", "sw": "ld", "ne": "ra", "se": "rd"}
        self.d.text((x, y), s, font=f, fill=fill, anchor=amap.get(anchor, "mm"))

    def image(self, img, x, y):
        if img is not None:
            self.img.paste(img, (int(x), int(y)))

    def sprite(self, name, cx, cy, size, angle=0.0):
        try:
            im = SPRITES.pil(name, size, angle)
        except Exception:
            return False
        if im is None:
            return False
        w, h = im.size
        self.img.paste(im, (int(cx - w / 2), int(cy - h / 2)), im)
        return True

    def save(self, path):
        self.img.save(path)


# --------------------------------------------------------------------------
# 画面布局与绘制：屏幕中间只有一个正方体，音符在立方体内流动
# --------------------------------------------------------------------------
WIN = 1000
CX, CY = 500.0, 548.0
CUBE_HALF = 205.0                   # 立方体屏幕半径（半棱长，像素）
CUBE_YAW, CUBE_PITCH = 16.0, -26.0  # 初始视角
LEAD = 1.35                         # 音符提前出现的秒数

CUBE_V = {"A": (-1, -1, 1), "B": (1, -1, 1), "C": (1, 1, 1), "D": (-1, 1, 1),
          "A1": (-1, -1, -1), "B1": (1, -1, -1), "C1": (1, 1, -1), "D1": (-1, 1, -1)}
CUBE_E = [("A", "B"), ("B", "C"), ("C", "D"), ("D", "A"),
          ("A1", "B1"), ("B1", "C1"), ("C1", "D1"), ("D1", "A1"),
          ("A", "A1"), ("B", "B1"), ("C", "C1"), ("D", "D1")]
CUBE_F = [("A", "B", "C", "D"), ("A1", "B1", "C1", "D1"),
          ("A", "B", "B1", "A1"), ("B", "C", "C1", "B1"),
          ("C", "D", "D1", "C1"), ("D", "A", "A1", "D1")]

# 外键 → 立方体顶点（idea.txt）
KEY_CORNER = {1: "A", 2: "B", 3: "C", 4: "D", 5: "A1", 6: "B1", 7: "C1", 8: "D1"}
CORNER_ZONE = {c: f"A{k}" for k, c in KEY_CORNER.items()}

ADJ = {c: set() for c in CUBE_V}
for _a, _b in CUBE_E:
    ADJ[_a].add(_b)
    ADJ[_b].add(_a)

A_R, B_R, E_R = 0.86, 0.46, 0.58     # A/B/E 触摸区在立方体内的相对位置


def clamp01(x):
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def vscale(p, s):
    return (p[0] * s, p[1] * s, p[2] * s)


def vmid(a, b):
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2)


def vlerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def zone_point(name):
    """触摸区在立方体里的坐标（单位立方体，|坐标| ≤ 1）。

    A 区   → 靠近对应的立方体顶点（外键位置）
    B 区   → 同一顶点方向上更靠里
    D 区   → 相邻两个顶点连线的中点（棱中点或面心，落在立方体表面）
    E 区   → 同一位置再往回收
    C 区   → 立方体中心两侧
    """
    z = ZONES[name]
    kind, k = z["kind"], z["k"]
    if kind == "C":
        return (0.12 if name == "C1" else (-0.12 if name == "C2" else 0.0), 0.0, 0.0)
    c1 = CUBE_V[KEY_CORNER[k + 1]]
    if kind == "A":
        return vscale(c1, A_R)
    if kind == "B":
        return vscale(c1, B_R)
    c2 = CUBE_V[KEY_CORNER[(k + 1) % 8 + 1]]
    m = vmid(c1, c2)
    return m if kind == "D" else vscale(m, E_R)


def project(pt, spin=0.0, half=CUBE_HALF):
    """立方体坐标 → 屏幕坐标（含透视），返回 (x, y, 深度)"""
    x, y, z = pt
    ry = math.radians(CUBE_YAW + spin)
    rx = math.radians(CUBE_PITCH)
    x1 = x * math.cos(ry) + z * math.sin(ry)
    z1 = -x * math.sin(ry) + z * math.cos(ry)
    y2 = y * math.cos(rx) - z1 * math.sin(rx)
    z2 = y * math.sin(rx) + z1 * math.cos(rx)
    f = 6.0 / (6.0 + z2 * 0.85)
    return (CX + x1 * half * f, CY - y2 * half * f, z2)


def screen_angle(pt, spin=0.0):
    x, y, _ = project(pt, spin)
    return math.degrees(math.atan2(x - CX, -(y - CY)))


def corner_route(a, b):
    """两个 A 区之间沿立方体棱的最短折线，返回中间的顶点序列（含终点）"""
    ca, cb = KEY_CORNER[ZONES[a]["k"] + 1], KEY_CORNER[ZONES[b]["k"] + 1]
    if ca == cb:
        return []
    prev = {ca: None}
    q = [ca]
    while q:
        cur = q.pop(0)
        if cur == cb:
            break
        for nb in ADJ[cur]:
            if nb not in prev:
                prev[nb] = cur
                q.append(nb)
    path = []
    cur = cb
    while cur is not None and cur != ca:
        path.append(cur)
        cur = prev.get(cur)
    path.reverse()
    return path


def slide_points3d(zones):
    """滑条真实轨迹：A→A 沿立方体棱走，其余走直线"""
    pts = [zone_point(zones[0])]
    for a, b in zip(zones[:-1], zones[1:]):
        if ZONES[a]["kind"] == "A" and ZONES[b]["kind"] == "A":
            for c in corner_route(a, b):
                pts.append(vscale(CUBE_V[c], A_R))
        else:
            pts.append(zone_point(b))
    return pts


def polyline_at(pts, u):
    """按弧长取折线 u∈[0,1] 处的点，返回 (点, 所在段下标)"""
    if len(pts) < 2:
        return pts[0], 0
    lens = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    total = sum(lens)
    if total <= 0:
        return pts[0], 0
    target = total * clamp01(u)
    acc = 0.0
    for i, d in enumerate(lens):
        if d > 0 and acc + d >= target:
            t = (target - acc) / d
            a, b = pts[i], pts[i + 1]
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), i
        acc += d
    return pts[-1], len(pts) - 1


def mix(c1, c2, f):
    f = max(0.0, min(1.0, f))
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * f) for i in range(3))


def draw_arena(be, spin=0.0):
    """立方体本体：暗色实体面 + 发光棱 + 顶点/触摸区标记"""
    faces = []
    for f in CUBE_F:
        d = sum(project(CUBE_V[n], spin)[2] for n in f) / 4.0
        faces.append((d, f))
    faces.sort(key=lambda t: -t[0])
    for d, f in faces:
        sh = clamp01(1.0 - (d + 2.0) / 4.0)
        pts = [(project(CUBE_V[n], spin)[0], project(CUBE_V[n], spin)[1]) for n in f]
        be.poly(pts, fill=mix("#080b22", "#1b2461", sh),
                outline=mix("#1d2660", "#aebcff", sh), width=2)
    for a, b in CUBE_E:
        pa, pb = project(CUBE_V[a], spin), project(CUBE_V[b], spin)
        sh = clamp01(1.0 - ((pa[2] + pb[2]) / 2 + 2.0) / 4.0)
        be.line([(pa[0], pa[1]), (pb[0], pb[1])],
                fill=mix("#1d2660", "#dde6ff", sh), width=1 + int(2 * sh))

    # 内圈触摸区：小点 + 名字
    for name, z in ZONES.items():
        if z["kind"] in ("A", "C"):
            continue
        x, y, _ = project(zone_point(name), spin)
        col = "#2f366e" if z["kind"] in "BD" else "#39407c"
        be.ellipse(x, y, 4, 4, fill=col, outline="")
        be.text(x, y + 12, name, 9, "#3f4682")

    # 中心 C 区
    for name in ("C1", "C2"):
        x, y, _ = project(zone_point(name), spin)
        be.ellipse(x, y, 3, 3, fill="#39407c", outline="")

    # 顶点：外键位置
    for corner, zone in CORNER_ZONE.items():
        key = 1 + [k for k, c in KEY_CORNER.items() if c == corner][0] - 1
        x, y, _ = project(CUBE_V[corner], spin)
        col = KEY_COLORS[key - 1]
        dx, dy = x - CX, y - CY
        n = math.hypot(dx, dy) or 1.0
        be.ellipse(x, y, 9, 9, fill=col, outline="#ffffff", width=1)
        tx, ty = x + dx / n * 30, y + dy / n * 26
        be.text(tx, ty, zone, 15, col, bold=True)
        be.text(tx, ty + 15, f"{key}", 11, mix(col, "#05060f", 0.25))


def note_color(n):
    if n.kind == "slide":
        return C_BREAK if n.is_break else C_SLIDE
    if n.kind.startswith("touch"):
        return C_TOUCH
    if n.is_break:
        return C_BREAK
    if n.kind == "hold":
        return C_HOLD
    return C_TAP


def note_sprite_name(n):
    if n.kind == "slide":
        return "star_break" if n.is_break else "star"
    if n.kind == "hold":
        return "hold_ex" if n.is_ex else "hold"
    if n.kind == "tap":
        if n.is_break:
            return "break"
        return "tap_each" if n.is_ex else "tap"
    return None


def draw_note_shape(be, n, x, y, scale=1.0, glow=0.0, angle=0.0):
    name = note_sprite_name(n)
    if name:
        base = 66 if n.kind == "hold" else (54 if n.kind == "slide" else 48)
        if be.sprite(name, x, y, base * scale, angle):
            return
    col = note_color(n)
    if glow > 0:
        col = mix(col, "#ffffff", glow * 0.6)
    rad = 16 * scale
    if n.kind == "tap":
        be.ellipse(x, y, rad, rad, fill=col, outline="#ffffff", width=1)
    elif n.kind == "hold":
        be.ellipse(x, y, rad, rad, fill=col, outline="#ffffff", width=2)
        be.ellipse(x, y, rad * 0.45, rad * 0.45, fill="#ffffff", outline="")
    elif n.kind == "slide":
        pts = []
        for i in range(10):
            a = 2 * math.pi * i / 10 - math.pi / 2
            rr = rad if i % 2 == 0 else rad * 0.45
            pts.append((x + rr * math.cos(a), y + rr * math.sin(a)))
        be.poly(pts, fill=col, outline="#ffffff", width=1)
    else:
        be.poly([(x, y - rad), (x + rad, y), (x, y + rad), (x - rad, y)],
                fill=col, outline="#ffffff", width=1)


KEY_HINT = "外键：1=A1(A)  2=A2(S)  3=A3(D)  4=A4(F)  5=A5(J)  6=A6(K)  7=A7(L)  8=A8(;)"
CORNER_HINT = "立方体顶点：A→A1→1  B→A2→2  C→A3→3  D→A4→4  A1→A5→5  B1→A6→6  C1→A7→7  D1→A8→8"


def draw_hud_static(be, meta, auto=True):
    """不随帧变化的部分，只在初始化时画一次"""
    title, artist, level = meta
    mode = "AUTO PLAY" if auto else "KEYBOARD"
    be.text(30, 32, "系ぎて", 26, C_TEXT, anchor="w", bold=True)
    be.text(30, 64, f"{artist}   MASTER {level}   ·   {mode}", 14, C_DIM, anchor="w")
    be.text(30, WIN - 92, KEY_HINT, 10, "#4d5390", anchor="w")
    be.text(30, WIN - 76, CORNER_HINT, 10, "#4d5390", anchor="w")
    be.line([(60, WIN - 30), (WIN - 60, WIN - 30)], fill="#1c2044", width=6)


def draw_hud(be, p, meta):
    title, artist, level = meta
    be.text(WIN - 30, 28, f"{int(p.score):07d}", 30, C_TEXT, anchor="e", bold=True)
    be.text(WIN - 30, 64, f"COMBO {p.combo}", 15, C_DIM, anchor="e")
    be.text(WIN - 30, 88, f"BPM {CHART_BPM:g}", 13, C_DIM, anchor="e")
    w = WIN - 120
    x0, y0 = 60, WIN - 30
    be.line([(x0, y0), (x0 + w * min(1.0, p.t / max(p.duration, 1e-6)), y0)], fill="#6c7bff", width=6)
    be.text(30, WIN - 54, f"{int(p.t // 60)}:{p.t % 60:05.2f}", 13, C_DIM, anchor="w")
    be.text(WIN - 30, WIN - 54, f"PERFECT {p.counts['perfect']}   MISS {p.counts['miss']}",
            13, C_DIM, anchor="e")
    if p.t < 3.0:                      # 前奏：倒计时 + 提示
        beat = 60.0 / max(CHART_BPM, 1e-6)
        left = max(0, int(math.ceil(4 - p.t / beat)))
        k = clamp01((3.0 - p.t) / 1.2)
        be.ellipse(CX, CY, 62, 62, fill="#070820",
                   outline=mix("#161a3c", "#ffe45e", k), width=2)
        be.text(CX, CY, str(left), 60, mix("#05060f", "#ffe45e", k), bold=True)
        be.text(CX, WIN - 130, "空格暂停 · Esc 退出" if p.auto else "A S D F J K L ; 击打八个顶点",
                15, mix("#05060f", "#c8d2ff", k))


# --------------------------------------------------------------------------
# 播放状态
# --------------------------------------------------------------------------
# 播放状态
# --------------------------------------------------------------------------
@dataclass
class Effect:
    zone: str
    t0: float
    color: str
    text: str = ""


class Player:
    def __init__(self, notes, duration, auto=True, audio=None):
        self.audio = audio
        self.notes = notes
        self.duration = duration
        self.auto = auto
        self.t = 0.0
        self.effects: list[Effect] = []
        self.combo = 0
        self.max_combo = 0
        self.counts = {"perfect": 0, "great": 0, "good": 0, "miss": 0}
        self.score = 0
        self._judged = 0
        self._cursor = 0
        self.finished = False

    # ---- 判定 ----
    def _sfx_for(self, n):
        if not self.audio:
            return
        if n.zones and n.zones[0] == "C":
            self.audio.sfx("firework"); return
        if n.kind == "slide":
            self.audio.sfx("slide_break" if n.is_break else "slide")
        elif n.kind == "touchhold":
            self.audio.sfx("touch_hold")
        elif n.kind.startswith("touch"):
            self.audio.sfx("touch")
        elif n.is_break:
            self.audio.sfx("break")
        elif n.is_ex:
            self.audio.sfx("ex")
        elif n.kind == "hold":
            self.audio.sfx("tap")
        else:
            self.audio.sfx("perfect")

    def _judge(self, n, verdict, t):
        n.hit = True
        n.judged = verdict
        n.judge_t = t
        self.counts[verdict] += 1
        if verdict != "miss":
            self._sfx_for(n)
        if verdict == "miss":
            self.combo = 0
            self.effects.append(Effect(n.zones[0], t, "#ff4d6d", "MISS"))
        else:
            self.combo += 1
            self.max_combo = max(self.max_combo, self.combo)
            w = {"perfect": 1.0, "great": 0.8, "good": 0.5}[verdict]
            self.score = min(1_000_000.0, self.score + 1_000_000.0 / max(len(self.notes), 1) * w)
            col = "#ffe45e" if verdict == "perfect" else ("#8be9ff" if verdict == "great" else "#a0ffb0")
            self.effects.append(Effect(n.zones[0], t, col, verdict.upper()))

    def update(self, t):
        self.t = t
        if self.auto:
            while self._cursor < len(self.notes) and self.notes[self._cursor].t <= t:
                n = self.notes[self._cursor]
                self._judge(n, "perfect", n.t)
                self._cursor += 1
        else:
            while self._cursor < len(self.notes) and self.notes[self._cursor].t + 0.20 < t:
                n = self.notes[self._cursor]
                if not n.hit:
                    self._judge(n, "miss", t)
                self._cursor += 1
        self.effects = [e for e in self.effects if t - e.t0 < 0.55]
        if t > self.duration + 2.0:
            self.finished = True

    def press(self, key: int, t: float):
        """键盘按下：命中该方向上最近的音符"""
        if self.auto:
            return
        best, bd = None, 0.22
        for n in self.notes[self._cursor:self._cursor + 40]:
            if n.hit or n.key != key:
                continue
            d = abs(n.t - t)
            if d < bd:
                best, bd = n, d
        if best:
            v = "perfect" if bd <= 0.075 else ("great" if bd <= 0.12 else "good")
            self._judge(best, v, t)

    def visible(self, now, lead=LEAD):
        lo = now - 0.6
        hi = now + lead + 0.05
        out = []
        for n in self.notes[max(0, self._cursor - 8):]:
            if n.t > hi:
                break
            if n.end > lo or n.t > lo:
                out.append(n)
        return out


# --------------------------------------------------------------------------
# 动态层绘制：音符在立方体内流动
# --------------------------------------------------------------------------
def draw_frame(be, p, meta, spin=0.0):
    now = p.t

    # 命中特效
    for e in p.effects:
        age = now - e.t0
        if age < 0 or age > 0.55:
            continue
        k = 1 - age / 0.55
        x, y, _ = project(zone_point(e.zone), spin)
        r = 20 + 44 * (1 - k)
        be.ellipse(x, y, r, r, outline=mix("#1a2148", e.color, k), width=2)
        if e.text == "PERFECT":
            if not be.sprite("perfect", x, y - 42 - 12 * (1 - k), 84, 0.0):
                be.text(x, y - 40, "PERFECT", 13, mix("#2a2f5e", e.color, k), bold=True)
        elif e.text:
            be.text(x, y - 34 - 20 * (1 - k), e.text, 13, mix("#2a2f5e", e.color, k), bold=True)

    for n in p.visible(now):
        if n.hit and now > max(n.end, n.t) + 0.12:
            continue

        # ---- 滑条：沿真实 3D 轨迹移动 ----
        if n.kind == "slide" and now >= n.t:
            zones = [n.zones[0]] + n.path
            pts3 = slide_points3d(zones)
            pts2 = [(project(q, spin)[0], project(q, spin)[1]) for q in pts3]
            u = clamp01((now - n.t) / max(n.end - n.t, 1e-3))
            pos, idx = polyline_at(pts2, u)
            if len(pts2) >= 2:
                be.line(pts2[:max(2, idx + 2)], fill=mix(note_color(n), "#ffffff", 0.25), width=5)
            draw_note_shape(be, n, pos[0], pos[1], 1.0, 0.0)
            continue

        # ---- 长按：命中后在原位持续 ----
        if n.kind in ("hold", "touchhold") and now >= n.t:
            x, y, _ = project(zone_point(n.zones[0]), spin)
            frac = clamp01((n.end - now) / max(n.end - n.t, 1e-3))
            be.ellipse(x, y, 19, 19, outline=note_color(n), width=3)
            be.text(x, y + 30, f"{frac:.0%}", 10, note_color(n))
            continue

        # ---- 飞行中：从立方体中心飞向目标触摸区 ----
        prog = clamp01((now - (n.t - LEAD)) / LEAD)
        pos3 = vlerp((0.0, 0.0, 0.0), zone_point(n.zones[0]), prog)
        x, y, depth = project(pos3, spin)
        persp = 1.0 - depth * 0.07          # 近大远小
        ang = screen_angle(zone_point(n.zones[0]), spin) if n.kind == "hold" else 0.0
        draw_note_shape(be, n, x, y, (0.45 + 0.55 * prog) * persp, 0.0, ang)
        if prog >= 1.0:
            be.ellipse(x, y, 21, 21, outline=mix(note_color(n), "#05060f", 0.45), width=2)


# 应用
# --------------------------------------------------------------------------
CHART_BPM = 120.0
PVMP4 = os.path.join(CHART_DIR, "pv.mp4")


class App:
    def __init__(self, notes, duration, meta, auto, offset, audio,
                 spin_cube=False, use_video=True, stats=False):
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.title("maimai · 系ぎて · 立方体触手可视化")
        self.root.geometry(f"{WIN}x{WIN}+24+4")
        self.root.configure(bg=C_BG)
        self.canvas = tk.Canvas(self.root, width=WIN, height=WIN, bg=C_BG,
                                highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.notes, self.duration, self.meta = notes, duration, meta
        self.auto, self.offset, self.audio = auto, offset, audio
        self.spin_cube = spin_cube
        self.p = Player(notes, duration, auto, audio)
        self.static = TkBackend(self.canvas, "static")
        self.dyn = TkBackend(self.canvas, "dyn")

        self.t0 = time.perf_counter()
        self._next_tick = 0.0
        self.paused = False
        self.pause_t = 0.0
        self.flash = 0.0
        self.stats = stats
        self._frames = 0
        self._frame_ms = 0.0
        self._stat_t = time.perf_counter()

        # 背景：优先 pv.mp4，其次曲绘
        self.static.clear()
        self._bg_photo = None
        self.video = None
        self._video_idx = -1
        if use_video:
            self.video = VideoBG(PVMP4, WIN, 30, time_fn=lambda: self.p.t)
            if self.video.start():
                from PIL import Image, ImageTk
                self._bg_photo = ImageTk.PhotoImage(Image.new("RGB", (WIN, WIN), (6, 7, 16)))
            else:
                self.video = None
        if self._bg_photo is None:
            self._bg_photo = self._make_bg()
        if self._bg_photo is not None:
            self.static.image(self._bg_photo, 0, 0)
        if not spin_cube:
            draw_arena(self.static)
        draw_hud_static(self.static, meta, auto=auto)
        self._spin = 0.0

        self.root.bind("<Escape>", lambda e: self.quit())
        self.root.bind("<space>", lambda e: self.toggle_pause())
        self.root.bind("<KeyPress>", self.on_key)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.lift()
        self.root.focus_force()

        if self.audio:
            self.audio.play_music(0)
        self.root.after(16, self.tick)

    def _make_bg(self):
        try:
            from PIL import Image, ImageEnhance, ImageFilter, ImageTk
            if not os.path.exists(BGIMG):
                return None
            im = Image.open(BGIMG).convert("RGB")
            side = min(im.size)
            im = im.crop(((im.width - side) // 2, (im.height - side) // 2,
                          (im.width + side) // 2, (im.height + side) // 2)).resize((WIN, WIN))
            im = im.filter(ImageFilter.GaussianBlur(7))
            im = ImageEnhance.Brightness(im).enhance(0.30)
            return ImageTk.PhotoImage(im)
        except Exception:
            return None

    def toggle_pause(self):
        if self.paused:
            self.paused = False
            self.t0 += time.perf_counter() - self.pause_t
            if self.audio:
                self.audio.play_music(int(max(0.0, self.now()) * 1000))
        else:
            self.paused = True
            self.pause_t = time.perf_counter()
            if self.audio:
                self.audio.stop_music()

    def now(self):
        clock = self.pause_t if self.paused else time.perf_counter()
        return clock - self.t0 - self.offset

    def on_key(self, ev):
        ch = (ev.char or "").lower()
        table = {"a": 1, "s": 2, "d": 3, "f": 4, "j": 5, "k": 6, "l": 7, ";": 8,
                 "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8}
        if ch in table and not self.paused:
            self.p.press(table[ch], self.now())
            self.flash = 0.12
            if self.audio:
                self.audio.sfx("tap")

    def quit(self):
        try:
            if self.audio:
                self.audio.stop_music()
            if getattr(self, "video", None):
                self.video.stop()
        except Exception:
            pass
        if self.stats:
            print(f"[stats] 平均 {self._frame_ms / max(self._frames, 1):.2f} ms/帧"
                  f"（{1000 / max(self._frame_ms / max(self._frames, 1), 0.01):.0f} fps）", flush=True)
        self.root.destroy()

    def tick(self):
        if self.paused:
            self.root.after(30, self.tick)
            return
        t_start = time.perf_counter()
        t = self.now()
        while t >= self._next_tick and self._next_tick < 4 * 60.0 / max(CHART_BPM, 1e-6):
            if self._next_tick >= 0 and self.audio:
                self.audio.sfx("metronome")
            self._next_tick += 60.0 / max(CHART_BPM, 1e-6)
        self.p.update(t)

        # 立方体（缓慢旋转）
        if self.spin_cube:
            self.canvas.delete("cube")
            self._spin += 0.30
            draw_arena(TkBackend(self.canvas, "cube"), spin=self._spin)

        # 视频背景：只在有新帧时 paste 一次
        if self.video is not None:
            idx, frame = self.video.take()
            if idx != self._video_idx and frame is not None:
                self._bg_photo.paste(frame)
                self._video_idx = idx

        self.dyn.clear()
        draw_frame(self.dyn, self.p, self.meta, spin=self._spin)
        draw_hud(self.dyn, self.p, self.meta)

        if self.stats:
            self._frames += 1
            self._frame_ms += (time.perf_counter() - t_start) * 1000
            now_all = time.perf_counter()
            if now_all - self._stat_t >= 5.0:
                print(f"[stats] {self._frame_ms / self._frames:.2f} ms/帧  "
                      f"t={t:6.2f}s  notes={len(self.p.visible(t))}", flush=True)
                self._frames = 0
                self._frame_ms = 0.0
                self._stat_t = now_all

        if self.p.finished:
            self.dyn.text(CX, CY, "FINISH", 52, "#ffffff", bold=True)
            return
        self.root.after(16, self.tick)


# --------------------------------------------------------------------------
# 单帧渲染 / 自检 / 入口
# --------------------------------------------------------------------------
def render_preview(out_path, at, spin=0.0):
    notes, dur, title, artist, level = parse_chart()
    p = Player(notes, dur, auto=True)
    p.update(at)
    p.effects = [e for e in p.effects if at - e.t0 < 0.34]
    be = PilBackend((WIN, WIN), (5, 6, 15))
    try:
        from PIL import Image, ImageEnhance, ImageFilter
        if os.path.exists(BGIMG):
            im = Image.open(BGIMG).convert("RGB")
            side = min(im.size)
            im = im.crop(((im.width - side) // 2, (im.height - side) // 2,
                          (im.width + side) // 2, (im.height + side) // 2)).resize((WIN, WIN))
            im = im.filter(ImageFilter.GaussianBlur(7))
            im = ImageEnhance.Brightness(im).enhance(0.30)
            be.image(im, 0, 0)
    except Exception:
        pass
    draw_arena(be, spin)
    draw_hud_static(be, (title, artist, level), auto=True)
    draw_frame(be, p, (title, artist, level), spin=spin)
    draw_hud(be, p, (title, artist, level))
    be.save(out_path)
    return out_path


def selftest():
    notes, dur, title, artist, level = parse_chart()
    print(f"曲目：{title} - {artist}   MASTER {level}")
    print(f"音符：{len(notes)}   时长：{dur:.2f}s ({int(dur // 60)}:{dur % 60:05.2f})   起始 BPM：{CHART_BPM:g}")
    from collections import Counter
    print("类型：", dict(Counter(n.kind for n in notes)))
    print("同押/键位：", sorted(Counter(n.key for n in notes).items()))
    print("BREAK：", sum(n.is_break for n in notes), " EX：", sum(n.is_ex for n in notes))
    print("首个音符：", notes[0].zones, f"t={notes[0].t:.3f}s", " 末个音符：", f"t={notes[-1].t:.3f}s")
    print("触摸区统计：", Counter(n.zones[0] for n in notes).most_common(10))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="舞萌谱面可视化播放器（立方体版）")
    ap.add_argument("--play", action="store_true", help="键盘游玩（默认自动播放）")
    ap.add_argument("--selftest", action="store_true", help="只解析谱面并输出统计")
    ap.add_argument("--preview", metavar="PNG", help="渲染单帧到 PNG")
    ap.add_argument("--time", type=float, default=12.0, help="--preview 的时间点（秒）")
    ap.add_argument("--offset", type=float, default=0.0, help="音画偏移（秒，正数=画面延后）")
    ap.add_argument("--no-sound", action="store_true", help="静音")
    ap.add_argument("--spin", action="store_true", help="立方体缓慢旋转")
    ap.add_argument("--no-video", action="store_true", help="不播放 pv.mp4 背景")
    ap.add_argument("--stats", action="store_true", help="每 5 秒打印一次帧耗时")
    args = ap.parse_args(argv)

    global CHART_BPM
    if args.selftest:
        return selftest()

    notes, dur, title, artist, level = parse_chart()
    CHART_BPM = _first_bpm()
    if not HAVE_PIL:
        print("[提示] 当前 Python 没装 Pillow：音符改用内置图形绘制、曲绘背景省略。")
        print("       想要原版贴图请执行：  pip install pillow")
    if args.preview:
        if not HAVE_PIL:
            print("[错误] --preview 需要 Pillow：pip install pillow")
            return 2
        render_preview(args.preview, args.time)
        print("saved", args.preview)
        return 0

    audio = Audio(enabled=not args.no_sound)
    if audio.ok:
        audio.open_music(TRACK)
        audio.load_sfx(SFX_DIR)
    app = App(notes, dur, (title, artist, level), auto=not args.play,
              offset=args.offset, audio=audio, spin_cube=args.spin,
              use_video=not args.no_video, stats=args.stats)
    app.root.mainloop()
    audio.close()
    return 0


def _first_bpm() -> float:
    try:
        raw = open(MAIDATA, "rb").read().decode("utf-8", "replace")
        pos = raw.find(f"&inote_{DIFFICULTY}=")
        seg = raw[pos:pos + 200]
        i = seg.find("(")
        j = seg.find(")", i)
        return float(seg[i + 1:j])
    except Exception:
        return 120.0


if __name__ == "__main__":
    sys.exit(main())
