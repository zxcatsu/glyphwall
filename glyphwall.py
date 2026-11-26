"""
GlyphWall — живые обои из символов для Windows 11.

Видео превращается в цветную мозаику из символов. Процессор почти не участвует:
кадр декодируется PyAV, уходит на GPU в нативном YUV без конвертации в RGB,
а яркость ячейки, цвет и выбор символа считает шейдер.
"""
import argparse
import ctypes
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from fractions import Fraction

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont

APP_NAME = "GlyphWall"
LEGACY_APP_NAME = "ASCIIWallpaper"
APP_VERSION = "1.0.0"

GITHUB_OWNER = "zxcatsu"
GITHUB_REPO = "glyphwall"
PROJECT_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}"
RELEASES_URL = PROJECT_URL + "/releases"
UPDATE_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
UPDATE_FILE = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME,
                           "update.json")
UPDATE_EVERY = 6 * 3600

FROZEN = getattr(sys, "frozen", False)
SELF_CMD = [sys.executable] if FROZEN else [sys.executable, os.path.abspath(__file__)]

CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x4000
DPI_AWARENESS_PER_MONITOR_V2 = 2
WM_SPAWN_WORKER = 0x052C
SPI_SETDESKWALLPAPER = 20
SPI_SEND_CHANGE = 0x02
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
HWND_TOP = 0
HWND_BOTTOM = 1
GW_CHILD = 5
GW_HWNDNEXT = 2
GA_PARENT = 1
SHELL_CLASSES = ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                 "SHELLDLL_DefView")
EXPLORER_WAIT_TICKS = 60
EXPLORER_WAIT_STEP = 0.5
DEFVIEW_WAIT_TICKS = 40
DEFVIEW_WAIT_STEP = 0.05
GUARD_INTERVAL = 0.25

NEAREST = 9728
LINEAR = 9729
LINEAR_MIPMAP_LINEAR = 9987

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TEMP_DIR = tempfile.gettempdir()
PID_FILE = os.path.join(TEMP_DIR, f"{APP_NAME.lower()}.pid")
EXPORT_PROGRESS_FILE = os.path.join(TEMP_DIR, "glyphwall_export_progress.txt")
PROXY_PROGRESS_FILE = os.path.join(TEMP_DIR, "glyphwall_proxy_progress.txt")

ROAMING = os.environ.get("APPDATA", os.path.expanduser("~"))
LOCAL = os.environ.get("LOCALAPPDATA", ROAMING)
APPDATA = os.path.join(ROAMING, APP_NAME)
SETTINGS_FILE = os.path.join(APPDATA, "settings.json")
CACHE_DIR = os.path.join(LOCAL, APP_NAME, "proxy")
LEGACY_SETTINGS_FILE = os.path.join(ROAMING, LEGACY_APP_NAME, "settings.json")
LEGACY_CACHE_DIR = os.path.join(LOCAL, LEGACY_APP_NAME, "proxy")

CLASSIC = " .'`^\",:;Il!i><~+_-?][}{1)(|/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$"
BINARY = " 01"
CODE_SAMPLE = '''
import numpy as np
def galaxy_pos(t):
    if t < T_RE:
        P, u = explosion_pos(t)
        return P, u, np.ones(NP, bool), np.linalg.norm(P, axis=1)
    v = t - T_RE
    ue = v - 0.6 * (1 - math.exp(-v / 0.6))
    r = np.sqrt(np.maximum(D_RE ** 2 - K_RE * ue, 0))
    ang = A_RE + 0.5 * v + 0.6 * (4 / K_RE) * (np.sqrt(D_RE) - np.sqrt(r))
    P = (r * np.cos(ang))[:, None] * E1 + (r * np.sin(ang))[:, None] * E2
    alive = t < T_EATP; return P, _U_RE + v, alive, r
class Particle: __init__(self, x=0.0, y=1.5): self.pos = [x, y]; self.vel = {}
for i in range(N): bri[ri, ci] = b[win]; col[win] = rgb * 255 // 8 >> 3 & 0xFF
'''

RES_PRESETS = {"Авто (по экрану)": "auto", "1920×1080 (Full HD)": "1920x1080",
               "2560×1440 (2K)": "2560x1440", "3840×2160 (4K)": "3840x2160"}

PROXY_H = 720
PROXY_VER = 3
PROGRESS_POLL_MS = 200

UI_BG = "#14171e"
UI_CARD = "#1c2029"
UI_CARD_ALT = "#262c38"
UI_BORDER = "#333b4a"
UI_TEXT = "#e6e9ef"
UI_MUTED = "#8b95a7"
UI_ACCENT = "#4fd1c5"
UI_ACCENT_HOVER = "#38b2a7"


def load_font(size):
    for p in (r"C:\Windows\Fonts\consola.ttf", r"C:\Windows\Fonts\cour.ttf",
              "DejaVuSansMono.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def build_atlas(chars, size):
    """Рисует символы в битмапы и сортирует от пустого к самому плотному."""
    font = load_font(size)
    asc, desc = font.getmetrics()
    ch = asc + desc
    cw = max(1, int(round(font.getlength("M"))))
    uniq, seen = [], set()
    for c in " " + chars:
        if (c.isspace() and c != " ") or c in seen:
            continue
        seen.add(c)
        uniq.append(c)
    glyphs, dens = [], []
    for c in uniq:
        im = Image.new("L", (cw, ch), 0)
        ImageDraw.Draw(im).text((0, 0), c, font=font, fill=255)
        a = np.asarray(im, dtype=np.uint8)
        d = float(a.mean())
        if c != " " and d <= 0:
            continue
        glyphs.append(a)
        dens.append(d)
    order = np.argsort(dens, kind="stable")
    return np.stack([glyphs[i] for i in order]), cw, ch


def get_chars(a):
    if a.charset == "binary":
        return BINARY
    if a.charset == "custom" and a.chars:
        return a.chars
    if a.charset == "code":
        txt = CODE_SAMPLE
        if a.codefile and os.path.isfile(a.codefile):
            with open(a.codefile, encoding="utf-8", errors="ignore") as fh:
                txt = fh.read()
        cnt = Counter(c for c in txt if c.isprintable() and not c.isspace())
        return "".join(c for c, _ in cnt.most_common(220)) or CLASSIC
    return CLASSIC


def frame_matrix(Wc, Hc, p, w, h):
    """Аффинное отображение пикселей исходника (w×h) в пиксели холста (Wc×Hc).

    Возвращает (L, t, s): холст = L @ источник + t. Одна и та же матрица идёт
    и в шейдер (через обратную), и в превью в окне настроек.
    """
    rot = p.rot % 360
    rw, rh = (h, w) if int(round(rot / 90)) % 2 else (w, h)
    mode = p.fit
    if mode == "auto":
        mode = "cover" if 0.7 < (rw / rh) / (Wc / Hc) < 1.43 else "contain"
    s = (max(Wc / rw, Hc / rh) if mode == "cover" else min(Wc / rw, Hc / rh)) * p.zoom
    th = math.radians(rot)
    c, sn = math.cos(th), math.sin(th)
    L = s * np.array([[c, -sn], [sn, c]]) @ np.diag([-1.0 if p.flip else 1.0, 1.0])
    t = np.array([Wc / 2 + p.offx * Wc, Hc / 2 + p.offy * Hc]) - L @ np.array([w / 2, h / 2])
    return L, t, s


def cell_to_src(Wc, Hc, gx, gy, p, w, h):
    """Матрица «координаты ячейки сетки -> UV исходника» для шейдера."""
    L, t, s = frame_matrix(Wc, Hc, p, w, h)
    G = np.diag([1.0 / gx, 1.0 / gy]) @ np.hstack([L, t[:, None]])
    M = np.diag([1.0 / w, 1.0 / h, 1.0]) @ np.linalg.inv(np.vstack([G, [0, 0, 1]]))
    return M.astype("f4"), s


def _plane(pl, w, h):
    """Плоскость кадра как непрерывный буфер нужного размера (без копии, если можно)."""
    buf = np.frombuffer(pl, np.uint8)
    ls = pl.line_size
    if ls == w:
        return buf[:w * h].reshape(h, w)
    rows = buf[:ls * h].reshape(h, ls)[:, :w]
    return np.ascontiguousarray(rows)


class VideoSource:
    """Читает видео и отдаёт Y и UV как сырые байты, без конвертации в RGB.

    Аппаратный декодер сюда не подходит: кадр всё равно надо спустить из VRAM
    в обычную память, чтобы отдать в текстуру OpenGL, и эта копия дороже самого
    декодирования (замеры — в README).
    """

    def __init__(self, path):
        self.path = path
        self.cont = av.open(path)
        self.stream = self.cont.streams.video[0]
        self.stream.thread_type = "AUTO"
        self.width = self.stream.codec_context.width
        self.height = self.stream.codec_context.height
        self.fps = float(self.stream.average_rate or 30.0) or 30.0
        self._gen = self.cont.decode(self.stream)
        self.space = 2
        self.full = False

    def read(self):
        """Следующий кадр: (y, u, v, w, h) или None в конце.

        Плоскости отдаются как есть. Если строки выровнены (line_size == width),
        это просто memoryview над буфером декодера, и текстура заполняется прямо из него.
        """
        try:
            frame = next(self._gen)
        except StopIteration:
            return None
        self.space = int(getattr(frame, "colorspace", 0) or 0)
        self.full = int(getattr(frame, "color_range", 0) or 0) == 2
        if frame.format.name in ("yuv420p", "yuvj420p"):
            planes = frame.planes
            if len(planes) >= 3:
                w, h = frame.width, frame.height
                return (_plane(planes[0], w, h), _plane(planes[1], w // 2, h // 2),
                        _plane(planes[2], w // 2, h // 2), w, h)
        arr = frame.to_ndarray(format="yuv420p")
        w, h = frame.width, frame.height
        n = (w // 2) * (h // 2)
        rest = arr[h:].reshape(-1)
        return (arr[:h, :w], rest[:n].reshape(h // 2, w // 2),
                rest[n:].reshape(h // 2, w // 2), w, h)

    def grab(self, n=1):
        """Пропускает n-1 кадров, только распаковка, без копирования в numpy."""
        for _ in range(n - 1):
            try:
                next(self._gen)
            except StopIteration:
                return False
        return True

    def rewind(self):
        self.cont.seek(0)
        self._gen = self.cont.decode(self.stream)

    def close(self):
        try:
            self.cont.close()
        except Exception:
            pass


def proxy_path(src):
    try:
        st = os.stat(src)
    except OSError:
        return None
    key = hashlib.sha1(f"{os.path.abspath(src).lower()}|{st.st_size}|{int(st.st_mtime)}|"
                       f"{PROXY_H}|{PROXY_VER}".encode("utf-8")).hexdigest()[:20]
    return os.path.join(CACHE_DIR, key + ".mp4")


def make_proxy(src, dst, progress=None):
    """Пережимает видео в PROXY_H по высоте. Прогресс пишется в файл для GUI."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = os.path.join(os.path.dirname(dst), os.path.basename(dst) + ".part.mp4")
    inp = av.open(src)
    st = inp.streams.video[0]
    w0, h0 = st.codec_context.width, st.codec_context.height
    k = min(1.0, PROXY_H / max(1, h0))
    W = max(2, int(round(w0 * k / 2)) * 2)
    H = max(2, int(round(h0 * k / 2)) * 2)
    fps = float(st.average_rate or 30.0) or 30.0
    total = int(st.frames or 0)
    out = av.open(tmp, "w")
    ost = out.add_stream("libx264", rate=fps_frac(fps))
    ost.width, ost.height, ost.pix_fmt = W, H, "yuv420p"
    ost.options = {"preset": "veryfast", "crf": "21", "g": "60"}
    n = 0
    try:
        for frame in inp.decode(st):
            for pkt in ost.encode(frame.reformat(width=W, height=H, format="yuv420p")):
                out.mux(pkt)
            n += 1
            if progress and (n % 10 == 0 or n == total):
                _write(progress, f"{n}/{total or n}")
        for pkt in ost.encode():
            out.mux(pkt)
    finally:
        out.close()
        inp.close()
    os.replace(tmp, dst)
    return dst


def ensure_proxy(src, progress=None):
    """Возвращает путь к прокси, собирая его при необходимости."""
    dst = proxy_path(src)
    if not dst:
        return src
    if os.path.isfile(dst):
        return dst
    try:
        return make_proxy(src, dst, progress)
    except Exception:
        return src


def _write(path, text):
    try:
        with open(path, "w") as fh:
            fh.write(text)
    except OSError:
        pass


def fps_frac(v):
    """Частота кадров как дробь: PyAV не принимает float в add_stream."""
    try:
        return Fraction(v).limit_denominator(1001)
    except (TypeError, ValueError, ZeroDivisionError):
        return Fraction(30)


VERT = """#version 330
in vec2 pos; out vec2 uv;
void main(){ uv = pos * 0.5 + 0.5; gl_Position = vec4(pos, 0.0, 1.0); }"""

GRID_FRAG = """#version 330
uniform sampler2D ytex; uniform sampler2D utex; uniform sampler2D vtex;
uniform mat3 to_src;
uniform mat3 ymat; uniform vec3 yoff;
uniform float dark; uniform float gamma; uniform float gain; uniform float sat;
uniform int colormode;
uniform vec3 base;
out vec4 fc;

vec3 hsv_sat(vec3 c, float k){
  float mx = max(c.r, max(c.g, c.b));
  float mn = min(c.r, min(c.g, c.b));
  float d = mx - mn;
  if (d < 1e-5 || mx < 1e-5) return c;
  float s = min(1.0, d / mx * k);
  float mn2 = mx * (1.0 - s);
  return mix(vec3(mn2), c, (mx - mn2) / d);
}
void main(){
  vec2 cc = gl_FragCoord.xy - 0.5;
  vec3 s = to_src * vec3(cc, 1.0);
  if (any(lessThan(s.xy, vec2(0.0))) || any(greaterThan(s.xy, vec2(1.0)))) {
    fc = vec4(0.0, 0.0, 0.0, 0.0); return;
  }
  // footprint одной ячейки в UV = столбцы матрицы to_src; мип-уровень подбирает GPU
  vec2 dx = to_src[0].xy, dy = to_src[1].xy;
  float y = textureGrad(ytex, s.xy, dx, dy).r;
  vec2  c = vec2(textureGrad(utex, s.xy, dx, dy).r, textureGrad(vtex, s.xy, dx, dy).r);
  float lum = clamp((y - dark) / max(1e-4, 1.0 - dark), 0.0, 1.0);
  lum = pow(lum, gamma);
  vec3 col;
  if (colormode == 0) {
    col = clamp(ymat * vec3(y, c) + yoff, 0.0, 1.0);
    col = clamp(hsv_sat(col, sat) * gain, 0.0, 1.0);
  } else {
    col = base * (0.4 + 0.6 * lum);
  }
  fc = vec4(col, lum);
}"""

GLYPH_FRAG = """#version 330
uniform sampler2D grid; uniform sampler2D atlas;
uniform vec2 res; uniform vec2 cell; uniform int n;
uniform float t; uniform float rate; uniform float bw; uniform vec3 bg;
out vec4 fc;
uint pcg(uint v){ uint s = v * 747796405u + 2891336453u;
  uint w = ((s >> ((s >> 28u) + 4u)) ^ s) * 277803737u; return (w >> 22u) ^ w; }
void main(){
  vec2 p = vec2(gl_FragCoord.x, res.y - gl_FragCoord.y);
  ivec2 c = clamp(ivec2(floor(p / cell)), ivec2(0), textureSize(grid, 0) - 1);
  ivec2 loc = clamp(ivec2(p - vec2(c) * cell), ivec2(0), ivec2(cell) - 1);
  vec4 g = texelFetch(grid, c, 0);
  vec3 col = bg;
  if (g.a > 0.0) {
    uint cid = uint(c.x) + uint(c.y) * 8192u;
    float phase = float(pcg(cid) & 65535u) / 65536.0;
    uint e = uint(floor(t * rate + phase));
    float h = float(pcg(cid ^ (e * 2654435761u)) & 16777215u) / 16777216.0;
    float idx = clamp(floor(g.a * float(n - 1) + (h - 0.5) * bw + 0.5), 1.0, float(n - 1));
    float cov = texelFetch(atlas, ivec2(int(idx) * int(cell.x) + loc.x, loc.y), 0).r;
    col = max(bg, g.rgb * cov);
  }
  fc = vec4(col, 1.0);
}"""

BLIT = """#version 330
uniform sampler2D tex; in vec2 uv; out vec4 fc;
void main(){ fc = vec4(texture(tex, uv).rgb, 1.0); }"""


def yuv_matrix(space, full):
    """Матрица YUV->RGB с учётом цветового пространства и диапазона."""
    if space in (9, 10, 14):
        kr, kb = 0.2627, 0.0593
    elif space == 1:
        kr, kb = 0.2126, 0.0722
    else:
        kr, kb = 0.299, 0.114
    kg = 1.0 - kr - kb
    if full:
        sy, su, oy, ou = 1.0, 1.0, 0.0, 0.5
    else:
        sy, su, oy, ou = 255 / 219, 255 / 224, 16 / 255, 128 / 255
    cr, cb = 2 * (1 - kr), 2 * (1 - kb)
    col0 = np.array([sy, sy, sy])
    col1 = np.array([0.0, -(kb * cr / kg) * su, cb * su])
    col2 = np.array([cr * su, -(kr * cb / kg) * su, 0.0])
    off = -(col0 * oy + col1 * ou + col2 * ou)
    return np.stack([col0, col1, col2], axis=1).astype("f4"), off.astype("f4")


SOLID = {"green": (100, 255, 90), "white": (235, 235, 235),
         "amber": (255, 170, 60), "cyan": (80, 220, 255)}
COLOR_MODE = {"green": 1, "white": 2, "amber": 3, "cyan": 4}


class GpuRenderer:
    def __init__(self, ctx, W, H, a):
        self.ctx, self.W, self.H, self.a = ctx, W, H, a
        size = max(5, round(a.size * H / 1080))
        atlas, cw, ch = build_atlas(get_chars(a), size)
        self.n, self.cw, self.ch = len(atlas), cw, ch
        self.cols, self.rows = -(-W // cw), -(-H // ch)
        self.Wc, self.Hc = self.cols * cw, self.rows * ch

        self.prog = ctx.program(vertex_shader=VERT, fragment_shader=GRID_FRAG)
        self.glyph = ctx.program(vertex_shader=VERT, fragment_shader=GLYPH_FRAG)
        self.blit = ctx.program(vertex_shader=VERT, fragment_shader=BLIT)
        vbo = ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, 1, 1], "f4").tobytes())
        self.vao = ctx.vertex_array(self.prog, [(vbo, "2f", "pos")])
        self.vao_glyph = ctx.vertex_array(self.glyph, [(vbo, "2f", "pos")])
        self.vao_blit = ctx.vertex_array(self.blit, [(vbo, "2f", "pos")])

        row = np.ascontiguousarray(np.concatenate(list(atlas), axis=1))
        self.atlas_tex = ctx.texture((row.shape[1], row.shape[0]), 1, row.tobytes())
        self.atlas_tex.filter = (NEAREST, NEAREST)
        self.grid_tex = ctx.texture((self.cols, self.rows), 4)
        self.grid_tex.filter = (NEAREST, NEAREST)
        self.fbo_grid = ctx.framebuffer(color_attachments=[self.grid_tex])

        self.ytex = self.utex = self.vtex = None
        self.src = (0, 0)

        self.color_tex = ctx.texture((W, H), 4)
        self.color_tex.filter = (LINEAR, LINEAR)
        self.fbo = ctx.framebuffer(color_attachments=[self.color_tex])

        p = self.prog
        p["dark"].value = a.dark
        p["gamma"].value = 0.9
        p["gain"].value = a.gain
        p["sat"].value = 1.3
        p["colormode"].value = COLOR_MODE.get(a.color, 0)
        base = SOLID.get(a.color, (235, 235, 235))
        p["base"].value = tuple(v / 255.0 for v in base)
        p["ytex"].value, p["utex"].value, p["vtex"].value = 0, 1, 2

        g = self.glyph
        g["res"].value = (W, H)
        g["cell"].value = (float(cw), float(ch))
        g["n"].value = self.n
        g["rate"].value = a.flicker * 30.0
        g["bw"].value = max(1.0, self.n / 32.0)
        g["bg"].value = (18 / 255, 14 / 255, 12 / 255)
        g["grid"].value, g["atlas"].value = 0, 1
        self.blit["tex"].value = 3

    def _ensure_tex(self, w, h):
        """Создаёт или пересоздаёт текстуры под размер кадра (он может смениться на лету)."""
        if self.ytex is not None and self.src == (w, h):
            return
        for t in (self.ytex, self.utex, self.vtex):
            if t is not None:
                t.release()
        ctx = self.ctx
        self.ytex = ctx.texture((w, h), 1, dtype="f1")
        self.utex = ctx.texture((w // 2, h // 2), 1, dtype="f1")
        self.vtex = ctx.texture((w // 2, h // 2), 1, dtype="f1")
        for t in (self.ytex, self.utex, self.vtex):
            t.filter = (LINEAR_MIPMAP_LINEAR, LINEAR)
            t.repeat_x = t.repeat_y = False
            try:
                t.anisotropy = min(16.0, ctx.info.get("GL_MAX_TEXTURE_MAX_ANISOTROPY", 16.0))
            except Exception:
                pass
        self.src = (w, h)

    def draw(self, y, u, v, w, h, src_w, src_h, colorspace=2, full=False, t=0.0):
        """Считает сетку и рисует кадр в offscreen-буфер W×H.

        y/u/v — плоскости кадра как есть; если строки в них уже непрерывные,
        moderngl копирует прямо из буфера декодера без промежуточного Python-объекта.
        """
        ctx, p = self.ctx, self.prog
        self._ensure_tex(w, h)
        self.ytex.write(y)
        self.utex.write(u)
        self.vtex.write(v)
        self.ytex.build_mipmaps()
        self.utex.build_mipmaps()
        self.vtex.build_mipmaps()

        M, _ = cell_to_src(self.Wc, self.Hc, self.cw, self.ch, self.a, src_w, src_h)
        p["to_src"].write(M.flatten(order="F").tobytes())
        ymat, yoff = yuv_matrix(colorspace, full)
        p["ymat"].write(ymat.flatten(order="F").tobytes())
        p["yoff"].value = tuple(float(v) for v in yoff)

        self.ytex.use(0)
        self.utex.use(1)
        self.vtex.use(2)
        self.fbo_grid.use()
        ctx.viewport = (0, 0, self.cols, self.rows)
        self.vao.render(5)

        self.grid_tex.use(0)
        self.atlas_tex.use(1)
        self.fbo.use()
        ctx.viewport = (0, 0, self.W, self.H)
        self.glyph["t"].value = float(t % 100000.0)
        self.vao_glyph.render(5)

    def present(self, SW, SH):
        """Выводит буфер на экран, масштабируя, если разрешения различаются."""
        self.ctx.screen.use()
        self.ctx.viewport = (0, 0, SW, SH)
        self.color_tex.use(3)
        self.vao_blit.render(5)

    def read_bgr(self):
        raw = self.fbo.read(components=3, alignment=1)
        img = np.frombuffer(raw, np.uint8).reshape(self.H, self.W, 3)
        return np.ascontiguousarray(img[::-1, :, ::-1])


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class PWR(ctypes.Structure):
    _fields_ = [("AC", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("pct", ctypes.c_ubyte),
                ("sys", ctypes.c_ubyte), ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]


def screen_size():
    u = ctypes.windll.user32
    return u.GetSystemMetrics(0), u.GetSystemMetrics(1)


def on_battery():
    s = PWR()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
    return s.AC == 0


def fullscreen_app_active(SW, SH, own):
    u = ctypes.windll.user32
    u.GetForegroundWindow.restype = ctypes.c_void_p
    h = u.GetForegroundWindow()
    if not h or h == own:
        return False
    buf = ctypes.create_unicode_buffer(64)
    u.GetClassNameW(ctypes.c_void_p(h), buf, 64)
    if buf.value in SHELL_CLASSES:
        return False
    r = RECT()
    u.GetWindowRect(ctypes.c_void_p(h), ctypes.byref(r))
    return r.left <= 0 and r.top <= 0 and r.right >= SW and r.bottom >= SH


def _user32():
    """user32 с прототипами под 64-битные дескрипторы окон.

    Без argtypes ctypes режет HWND до 32 бит и вызовы молча уходят в никуда.
    """
    u = ctypes.windll.user32
    vp = ctypes.c_void_p
    u.FindWindowW.restype = vp
    u.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    u.FindWindowExW.restype = vp
    u.FindWindowExW.argtypes = [vp, vp, ctypes.c_wchar_p, ctypes.c_wchar_p]
    u.GetParent.restype = vp
    u.GetParent.argtypes = [vp]
    u.GetWindow.restype = vp
    u.GetWindow.argtypes = [vp, ctypes.c_uint]
    u.SetParent.restype = vp
    u.SetParent.argtypes = [vp, vp]
    u.SetWindowPos.argtypes = [vp, vp] + [ctypes.c_int] * 4 + [ctypes.c_uint]
    u.MoveWindow.argtypes = [vp] + [ctypes.c_int] * 4 + [ctypes.c_bool]
    u.IsWindow.argtypes = [vp]
    u.GetAncestor.restype = vp
    u.GetAncestor.argtypes = [vp, ctypes.c_uint]
    u.SendMessageTimeoutW.argtypes = [vp, ctypes.c_uint, vp, vp, ctypes.c_uint,
                                      ctypes.c_uint, vp]
    return u


def _parent_of(u, hwnd):
    """Родитель окна в иерархии рабочего стола.

    GetParent здесь не годится: окно обоев создаётся как WS_POPUP, и для таких
    окон GetParent отдаёт владельца, а не родителя — то есть NULL.
    """
    return u.GetAncestor(hwnd, GA_PARENT)


def _find_defview(u, progman):
    """Окно иконок и его родитель: на Win11 это прямой ребёнок Progman,
    на Win10 — лежит внутри WorkerW."""
    dv = u.FindWindowExW(progman, None, "SHELLDLL_DefView", None)
    if dv:
        return dv, progman
    hit = []
    Enum = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(h, _):
        d = u.FindWindowExW(h, None, "SHELLDLL_DefView", None)
        if d:
            hit.append((d, h))
        return True

    u.EnumWindows(Enum(cb), 0)
    return hit[0] if hit else (None, None)


def _place_behind(u, hwnd, dv):
    """Ставит окно вплотную под иконки.

    Именно SetWindowPos(hwnd, dv) — вставка сразу за окном иконок. Вариант
    «поднять окно наверх, потом поднять иконки» оставляет ярлыки закрытыми,
    если окно иконок в этот момент ещё не создано. Пока иконок нет, окно
    уходит на самый низ: пусть лучше обои на секунду пропадут, чем ярлыки.
    """
    flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
    if not dv:
        u.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0, flags)
        return
    ours, theirs = _parent_of(u, hwnd), _parent_of(u, dv)
    if ours == theirs:
        u.SetWindowPos(hwnd, dv, 0, 0, 0, 0, flags)
    elif ours and theirs and _parent_of(u, ours) == _parent_of(u, theirs):
        # Win10: обои и иконки в разных WorkerW — двигаем весь наш контейнер.
        u.SetWindowPos(ours, theirs, 0, 0, 0, 0, flags)
    else:
        u.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0, flags)


def _above(u, parent, target):
    """Окно, которое в z-порядке нарисовано прямо над target."""
    h, prev = u.GetWindow(parent, GW_CHILD), None
    while h:
        if h == target:
            return prev
        prev = h
        h = u.GetWindow(h, GW_HWNDNEXT)
    return None


def _directly_behind(u, hwnd, dv):
    """Стоим ли мы ровно под иконками — между ними и остальным рабочим столом.

    Проверять «иконки выше нас» мало: под нами может оказаться WorkerW, который
    закрашивает рабочий стол обычными обоями, и тогда символов не видно, хотя
    ярлыки на месте. Поэтому требуем, чтобы над нами было ровно окно иконок.
    """
    ours, theirs = _parent_of(u, hwnd), _parent_of(u, dv)
    if not ours or not theirs:
        return False
    if ours == theirs:
        return _above(u, ours, hwnd) == dv
    grand = _parent_of(u, ours)
    if not grand or grand != _parent_of(u, theirs):
        return False
    return _above(u, grand, ours) == theirs


def _same_desktop_layer(u, a, b):
    """Лежат ли окна в одном контейнере или в соседних (Win10: обои и иконки
    в разных WorkerW под общим Progman)."""
    pa, pb = _parent_of(u, a), _parent_of(u, b)
    if not pa or not pb:
        return False
    if pa == pb:
        return True
    return _parent_of(u, pa) == _parent_of(u, pb)


def keep_behind_icons(hwnd, progman, dv, W, H):
    """Сторож: держит окно обоев ровно под ярлыками.

    Проводник пересобирает окна рабочего стола при смене обоев, перезапуске и
    смене разрешения. После этого обои могут уехать выше иконок (тогда пропадают
    все ярлыки) или ниже фона (тогда пропадают сами обои). Чиним только когда
    порядок действительно сломан, чтобы не дёргать перерисовку каждый тик.
    Возвращает актуальное окно иконок (или None, если его сейчас нет).
    """
    u = _user32()
    if not progman or not u.IsWindow(progman):
        progman = u.FindWindowW("Progman", None)
        if not progman:
            return dv
    if not dv or not u.IsWindow(dv):
        dv, _ = _find_defview(u, progman)
        if not dv:
            return None
    if _directly_behind(u, hwnd, dv):
        return dv
    if not _same_desktop_layer(u, hwnd, dv):
        # Рабочий стол пересобрали, и наше окно осталось в старом контейнере.
        owner = _parent_of(u, dv)
        if not owner:
            return dv
        u.SetParent(hwnd, owner)
        u.MoveWindow(hwnd, 0, 0, W, H, True)
    _place_behind(u, hwnd, dv)
    return dv


def attach_to_desktop(hwnd, W, H):
    """Сажает окно за иконками рабочего стола: WorkerW на Win10, Progman на Win11 24H2+.

    Возвращает (Progman, окно иконок) для сторожа keep_behind_icons.
    """
    u = _user32()
    vp = ctypes.c_void_p

    progman = None
    for _ in range(EXPLORER_WAIT_TICKS):
        progman = u.FindWindowW("Progman", None)
        if progman:
            break
        time.sleep(EXPLORER_WAIT_STEP)
    u.SendMessageTimeoutW(progman, WM_SPAWN_WORKER, 0, 0, 0, 1000,
                          ctypes.byref(ctypes.c_ulong()))

    found = []
    Enum = ctypes.WINFUNCTYPE(ctypes.c_bool, vp, vp)

    def cb(h, _):
        if u.FindWindowExW(h, None, "SHELLDLL_DefView", None):
            w = u.FindWindowExW(None, h, "WorkerW", None)
            if w:
                found.append(w)
        return True

    u.EnumWindows(Enum(cb), 0)
    if found:
        # Win10: окно иконок сидит в WorkerW, а мы садимся в соседний WorkerW
        # сразу за ним — это и есть «позади иконок». Порядок внутри Progman
        # такой схеме не нужен, поэтому сторож здесь не работает.
        u.SetParent(hwnd, found[0])
        u.MoveWindow(hwnd, 0, 0, W, H, True)
        return progman, None

    # Иконки ищем с повторами: сразу после смены обоев Explorer их ещё пересоздаёт.
    dv, owner = None, progman
    for _ in range(DEFVIEW_WAIT_TICKS):
        dv, owner = _find_defview(u, progman)
        if dv:
            break
        time.sleep(DEFVIEW_WAIT_STEP)
    u.SetParent(hwnd, owner)
    u.MoveWindow(hwnd, 0, 0, W, H, True)
    _place_behind(u, hwnd, dv)
    return progman, dv


def stop_wallpaper():
    try:
        with open(PID_FILE) as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       creationflags=CREATE_NO_WINDOW, capture_output=True)
    except Exception:
        pass
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    ctypes.windll.user32.SystemParametersInfoW(SPI_SETDESKWALLPAPER, 0, None, SPI_SEND_CHANGE)


def parse_res(a, SW, SH):
    if a.res == "auto":
        return SW, SH
    w, h = a.res.lower().split("x")
    return int(w), int(h)


def run_player(a, wallpaper):
    import moderngl
    import pygame

    if wallpaper:
        time.sleep(a.delay)
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(DPI_AWARENESS_PER_MONITOR_V2)
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)
        except Exception:
            pass
        SW, SH = screen_size()
        with open(PID_FILE, "w") as fh:
            fh.write(str(os.getpid()))
        threading.Thread(target=notify_update_if_any, daemon=True).start()
    else:
        SW, SH = 1280, 720
    W, H = parse_res(a, SW, SH)

    video = ensure_proxy(a.video, a.progress) if (a.proxy and not a.no_proxy) else a.video

    pygame.init()
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 3)
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 3)
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_PROFILE_MASK,
                                    pygame.GL_CONTEXT_PROFILE_CORE)
    flags = pygame.OPENGL | pygame.DOUBLEBUF | (pygame.NOFRAME if wallpaper else 0)
    pygame.display.set_mode((SW, SH), flags, vsync=0)
    pygame.display.set_caption(f"{APP_NAME} — превью (Esc — выход)")
    hwnd = pygame.display.get_wm_info()["window"]
    progman, defview = (None, None)
    if wallpaper:
        progman, defview = attach_to_desktop(hwnd, SW, SH)

    r = GpuRenderer(moderngl.create_context(), W, H, a)
    src = VideoSource(video)
    vfps = src.fps or 30.0
    clock = pygame.time.Clock()
    t0, last_check, last_guard, paused, bat = time.time(), 0.0, 0.0, False, False
    src_w, src_h = src.width, src.height

    while True:
        for e in pygame.event.get():
            if e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE):
                src.close()
                pygame.quit()
                if wallpaper and os.path.exists(PID_FILE):
                    os.remove(PID_FILE)
                return
        now = time.time()
        if wallpaper and now - last_guard > GUARD_INTERVAL:
            last_guard = now
            defview = keep_behind_icons(hwnd, progman, defview, SW, SH)
        if wallpaper and now - last_check > 1.0:
            last_check = now
            paused = bool(a.pause_fs) and fullscreen_app_active(SW, SH, hwnd)
            bat = bool(a.battery) and on_battery()
        if paused:
            time.sleep(0.5)
            continue
        fps = min(a.fps, vfps) if a.fps > 0 else vfps
        if bat:
            fps = min(fps, 15)
        step = max(1, round(vfps / fps))
        got = src.read()
        if got is None:
            src.rewind()
            continue
        y, u, v, w, h = got
        if (w, h) != (src_w, src_h):
            src_w, src_h = w, h
        if step > 1:
            src.grab(step)
        r.draw(y, u, v, w, h, src_w, src_h, src.space, src.full, now - t0)
        r.present(SW, SH)
        pygame.display.flip()
        clock.tick(fps)


def run_export(a):
    import moderngl

    kw = {"backend": os.environ["ASCII_GL_BACKEND"]} if os.environ.get("ASCII_GL_BACKEND") else {}
    ctx = moderngl.create_standalone_context(**kw)
    W, H = parse_res(a, 1920, 1080)
    src = VideoSource(a.video)
    vfps = src.fps or 30.0
    out_fps = a.fps if a.fps > 0 else vfps
    step = max(1, round(vfps / out_fps))
    r = GpuRenderer(ctx, W, H, a)
    src_w, src_h = src.width, src.height

    out = av.open(a.out, "w")
    ost = out.add_stream("libx264", rate=fps_frac(out_fps))
    ost.width, ost.height, ost.pix_fmt = W, H, "yuv420p"
    ost.options = {"preset": "medium", "crf": "18"}
    i = 0
    try:
        while not a.max_frames or i < a.max_frames:
            got = src.read()
            if got is None:
                break
            y, u, v, w, h = got
            if step > 1:
                src.grab(step)
            r.draw(y, u, v, w, h, src_w, src_h, src.space, src.full, i / out_fps)
            frame = av.VideoFrame.from_ndarray(r.read_bgr(), format="bgr24")
            for pkt in ost.encode(frame.reformat(format="yuv420p")):
                out.mux(pkt)
            i += 1
            if a.progress and (i % 5 == 0):
                _write(a.progress, f"{i}/{a.max_frames or i}")
        for pkt in ost.encode():
            out.mux(pkt)
    finally:
        out.close()
        src.close()
    if a.progress:
        _write(a.progress, "DONE")


def run_proxy(a):
    """Отдельный процесс: собрать прокси-копию и отрапортовать о прогрессе."""
    if not a.video or not os.path.isfile(a.video):
        if a.progress:
            _write(a.progress, "ERR")
        return
    dst = proxy_path(a.video)
    if dst and os.path.isfile(dst):
        if a.progress:
            _write(a.progress, "DONE")
        return
    try:
        make_proxy(a.video, dst, a.progress)
        if a.progress:
            _write(a.progress, "DONE")
    except Exception:
        if a.progress:
            _write(a.progress, "ERR")


def preview_image(frame, Wt, Ht, PW, ph, p):
    """Кадр исходника -> картинка PW×ph, как он ляжет на экран."""
    h, w = frame.shape[:2]
    L, t, s = frame_matrix(Wt, Ht, p, w, h)
    gx, gy = Wt / PW, Ht / ph
    f = min(gx, gy) / s / 2
    if f > 1:
        sw, sh = max(1, int(w / f)), max(1, int(h / f))
        frame = np.asarray(Image.fromarray(frame).resize((sw, sh), Image.BOX))
        L = L @ np.diag([w / sw, h / sh])
        w, h = sw, sh
    Li = np.linalg.inv(L)
    a = Li[0, 0] * gx
    b = Li[0, 1] * gy
    c = -(Li[0, 0] * t[0] + Li[0, 1] * t[1])
    d = Li[1, 0] * gx
    e = Li[1, 1] * gy
    ff = -(Li[1, 0] * t[0] + Li[1, 1] * t[1])
    return Image.fromarray(frame).transform((PW, ph), Image.AFFINE, (a, b, c, d, e, ff),
                                            resample=Image.BILINEAR, fillcolor=(0, 0, 0))


def grab_frame(path):
    """Один кадр из середины видео для превью в настройках."""
    try:
        c = av.open(path)
        st = c.streams.video[0]
        n = int(st.frames or 0)
        if n > 4:
            c.seek(n // 4)
        frame = next(c.decode(st))
        img = frame.reformat(format="rgb24").to_ndarray()
        c.close()
        return img
    except Exception:
        return None


def migrate_legacy_data():
    """Переносит настройки и кэш прокси из ASCIIWallpaper после переименования."""
    import shutil

    if os.path.isfile(LEGACY_SETTINGS_FILE) and not os.path.isfile(SETTINGS_FILE):
        try:
            os.makedirs(APPDATA, exist_ok=True)
            shutil.move(LEGACY_SETTINGS_FILE, SETTINGS_FILE)
        except OSError:
            pass
    if os.path.isdir(LEGACY_CACHE_DIR) and not os.path.isdir(CACHE_DIR):
        try:
            os.makedirs(os.path.dirname(CACHE_DIR), exist_ok=True)
            shutil.move(LEGACY_CACHE_DIR, CACHE_DIR)
        except OSError:
            pass
    for leftover in (os.path.dirname(LEGACY_SETTINGS_FILE), os.path.dirname(LEGACY_CACHE_DIR)):
        try:
            os.rmdir(leftover)
        except OSError:
            pass
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, LEGACY_APP_NAME)
    except OSError:
        pass


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def set_window_icon(root):
    path = resource_path("glyphwall.ico")
    if os.path.isfile(path):
        try:
            root.iconbitmap(path)
        except Exception:
            pass


def ver_tuple(s):
    """'v1.2.3' -> (1, 2, 3). Всё, что не число, отбрасывается."""
    out = []
    for part in str(s).strip().lstrip("vV").split("."):
        num = "".join(c for c in part if c.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out[:4]) if out else (0,)


def _update_cache():
    try:
        with open(UPDATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_update_cache(data):
    try:
        os.makedirs(os.path.dirname(UPDATE_FILE), exist_ok=True)
        with open(UPDATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    except OSError:
        pass


def fetch_release(timeout=6):
    """Последний релиз с GitHub. None, если сети нет или репозиторий недоступен."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(UPDATE_API, headers={
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def update_info(force=False):
    """Есть ли версия новее. Кэш на UPDATE_EVERY, чтобы не дёргать API на каждый запуск.

    Возвращает dict с полями version/body/url или None.
    """
    now = time.time()
    cache = _update_cache()
    if not force and now - float(cache.get("checked", 0) or 0) < UPDATE_EVERY:
        return cache.get("info")

    rel = fetch_release()
    cache["checked"] = now
    if not rel:
        _save_update_cache(cache)
        return cache.get("info")

    tag = rel.get("tag_name") or rel.get("name") or ""
    info = None
    if ver_tuple(tag) > ver_tuple(APP_VERSION):
        info = {"version": tag.lstrip("vV"),
                "body": (rel.get("body") or "").strip(),
                "url": rel.get("html_url") or RELEASES_URL,
                "name": rel.get("name") or tag}
    cache["info"] = info
    _save_update_cache(cache)
    return info


def short_notes(body, limit=220):
    """Тело релиза в одну строку для всплывашки."""
    text = " ".join(line.strip() for line in body.splitlines() if line.strip())
    text = text.replace("**", "").replace("`", "").replace("#", "")
    for mark in ("- ", "* "):
        text = text.replace(mark, "• ")
    return text[:limit] + ("…" if len(text) > limit else "")


def open_url(url):
    try:
        os.startfile(url)
    except Exception:
        pass


def toast(title, text, seconds=6.0):
    """Всплывашка в области уведомлений Windows. Без сторонних библиотек.

    Живёт отдельным процессом: Shell_NotifyIcon требует окно с насосом сообщений,
    а держать его в потоке отрисовки обоев незачем.
    """
    import ctypes.wintypes as wt

    u = ctypes.windll.user32
    sh = ctypes.windll.shell32
    k = ctypes.windll.kernel32
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

    u.DefWindowProcW.restype = LRESULT
    u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    u.RegisterClassW.restype = wt.ATOM
    u.CreateWindowExW.restype = wt.HWND
    u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    sh.Shell_NotifyIconW.restype = wt.BOOL

    class WNDCLASS(ctypes.Structure):
        _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                    ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                    ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]

    class NOTIFYICONDATA(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
                    ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT),
                    ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                    ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
                    ("szInfo", wt.WCHAR * 256), ("uVersion", wt.UINT),
                    ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                    ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON)]

    NIM_ADD, NIM_DELETE = 0, 2
    NIF_ICON, NIF_TIP, NIF_INFO = 0x02, 0x04, 0x10
    NIIF_INFO = 0x01
    LR_LOADFROMFILE, IMAGE_ICON = 0x10, 1

    def wndproc(h, m, w, l):
        return u.DefWindowProcW(h, m, w, l)

    proc = WNDPROC(wndproc)
    wc = WNDCLASS()
    wc.lpfnWndProc = proc
    wc.hInstance = k.GetModuleHandleW(None)
    wc.lpszClassName = "GlyphWallToast"
    if not u.RegisterClassW(ctypes.byref(wc)):
        return False
    hwnd = u.CreateWindowExW(0, "GlyphWallToast", APP_NAME, 0, 0, 0, 0, 0,
                             None, None, wc.hInstance, None)
    if not hwnd:
        return False

    icon_path = resource_path("glyphwall.ico")
    hicon = None
    if os.path.isfile(icon_path):
        u.LoadImageW.restype = wt.HANDLE
        hicon = u.LoadImageW(None, icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
    if not hicon:
        u.LoadIconW.restype = wt.HICON
        hicon = u.LoadIconW(None, ctypes.c_wchar_p(32512))

    nid = NOTIFYICONDATA()
    nid.cbSize = ctypes.sizeof(nid)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_ICON | NIF_TIP | NIF_INFO
    nid.hIcon = hicon
    nid.szTip = APP_NAME
    nid.szInfoTitle = title[:63]
    nid.szInfo = text[:255]
    nid.dwInfoFlags = NIIF_INFO
    ok = sh.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

    deadline = time.time() + seconds
    msg = ctypes.wintypes.MSG()
    while ok and time.time() < deadline:
        while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.05)
    nid.uFlags = 0
    sh.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
    u.DestroyWindow(hwnd)
    return bool(ok)


def notify_update_if_any():
    """Тихий путь для автозапуска: проверить обновление и показать всплывашку.

    Один и тот же релиз всплывает один раз — версия запоминается в кэше.
    """
    info = update_info()
    if not info or _update_cache().get("notified") == info["version"]:
        return False
    cache = _update_cache()
    cache["notified"] = info["version"]
    _save_update_cache(cache)
    text = short_notes(info["body"]) or "Открыть страницу релиза на GitHub"
    return toast(f"{APP_NAME} {info['version']}", text)


def enable_dpi_awareness():
    """Без этого Windows врёт про размер экрана, а CustomTkinter сверху ещё раз
    масштабирует — окно вылезает за нижний край."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(DPI_AWARENESS_PER_MONITOR_V2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def autostart_active():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except OSError:
        return False


def gui():
    import tkinter as tk
    import customtkinter as ctk
    from tkinter import filedialog, messagebox
    from types import SimpleNamespace

    migrate_legacy_data()
    enable_dpi_awareness()
    ctk.set_appearance_mode("dark")

    root = ctk.CTk(fg_color=UI_BG)
    root.title(APP_NAME)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    scale = ctk.ScalingTracker.get_window_scaling(root) or 1.0
    w = min(1180, int((sw - 80) / scale))
    h = min(820, int((sh - 110) / scale))
    root.geometry(f"{w}x{h}")
    root.minsize(min(980, w), min(620, h))
    set_window_icon(root)

    f_title = ctk.CTkFont(size=21, weight="bold")
    f_card = ctk.CTkFont(size=11, weight="bold")
    f_label = ctk.CTkFont(size=13)
    f_small = ctk.CTkFont(size=11)
    f_btn = ctk.CTkFont(size=13)

    CS = {"Классика (ASCII)": "classic", "Код (буквы из файла)": "code",
          "Бинарный 0/1": "binary", "Свои символы": "custom"}
    CO = {"Цвета видео": "video", "Зелёный (матрица)": "green", "Белый": "white",
          "Янтарный": "amber", "Циан": "cyan"}
    FT = {"Авто": "auto", "Заполнить экран (обрезать)": "cover", "Вписать (чёрные поля)": "contain"}
    FPS = {"15": 15, "24": 24, "30": 30, "60": 60, "Как в видео": 0}

    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        saved = {}

    def sv(name, default, cls=tk.StringVar):
        return cls(value=saved.get(name, default))

    V = {"video": sv("video", ""), "size": sv("size", 10, tk.IntVar),
         "cs": sv("cs", "Классика (ASCII)"), "chars": sv("chars", ""),
         "codefile": sv("codefile", ""), "co": sv("co", "Цвета видео"),
         "ft": sv("ft", "Авто"), "flicker": sv("flicker", 0.04, tk.DoubleVar),
         "gain": sv("gain", 1.5, tk.DoubleVar), "dark": sv("dark", 0.15, tk.DoubleVar),
         "fps": sv("fps", "30"), "res": sv("res", "Авто (по экрану)"),
         "pause_fs": sv("pause_fs", True, tk.BooleanVar),
         "battery": sv("battery", True, tk.BooleanVar),
         "proxy": sv("proxy", True, tk.BooleanVar),
         "rot": sv("rot", 0.0, tk.DoubleVar), "zoom": sv("zoom", 1.0, tk.DoubleVar),
         "offx": sv("offx", 0.0, tk.DoubleVar), "offy": sv("offy", 0.0, tk.DoubleVar),
         "flip": sv("flip", False, tk.BooleanVar)}
    status = tk.StringVar(value="Выбери видео и нажми «Поставить обои»")

    head = ctk.CTkFrame(root, fg_color="transparent")
    head.pack(fill="x", padx=26, pady=(18, 12))
    ctk.CTkLabel(head, text=APP_NAME, font=f_title, text_color=UI_TEXT).pack(side="left")
    ctk.CTkLabel(head, text=f"v{APP_VERSION}", font=f_small, text_color=UI_ACCENT).pack(
        side="left", padx=(9, 0), pady=(7, 0))
    ctk.CTkLabel(head, text="живые обои из символов", font=f_small,
                 text_color=UI_MUTED).pack(side="left", padx=(12, 0), pady=(7, 0))

    def link(parent, text, url, font=None):
        lb = ctk.CTkLabel(parent, text=text, font=font or f_small, text_color=UI_MUTED,
                          cursor="hand2")
        lb.pack(side="left", padx=(14, 0))
        lb.bind("<Button-1>", lambda e: open_url(url))
        lb.bind("<Enter>", lambda e: lb.configure(text_color=UI_ACCENT))
        lb.bind("<Leave>", lambda e: lb.configure(text_color=UI_MUTED))
        return lb

    links = ctk.CTkFrame(head, fg_color="transparent")
    links.pack(side="right", pady=(6, 0))
    link(links, "GitHub", PROJECT_URL)
    link(links, f"@{GITHUB_OWNER}", f"https://github.com/{GITHUB_OWNER}")

    banner = ctk.CTkFrame(root, fg_color=UI_CARD, corner_radius=12, border_width=1,
                          border_color=UI_ACCENT)

    foot = ctk.CTkFrame(root, fg_color=UI_CARD, corner_radius=12, border_width=1,
                        border_color=UI_BORDER)
    foot.pack(side="bottom", fill="x", padx=26, pady=(12, 18))

    body = ctk.CTkFrame(root, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=26)
    left = ctk.CTkScrollableFrame(body, width=460, fg_color="transparent",
                                  scrollbar_button_color=UI_BORDER,
                                  scrollbar_button_hover_color=UI_MUTED)
    left.pack(side="left", fill="both")
    right = ctk.CTkScrollableFrame(body, fg_color="transparent",
                                   scrollbar_button_color=UI_BORDER,
                                   scrollbar_button_hover_color=UI_MUTED)
    right.pack(side="left", fill="both", expand=True, padx=(18, 0))

    def card(title, parent=left):
        outer = ctk.CTkFrame(parent, fg_color=UI_CARD, corner_radius=12,
                             border_width=1, border_color=UI_BORDER)
        outer.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(outer, text=title, font=f_card, text_color=UI_MUTED).pack(
            anchor="w", padx=16, pady=(12, 4))
        inner = ctk.CTkFrame(outer, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=(0, 12))
        return inner

    def field(parent, text, build):
        r = ctk.CTkFrame(parent, fg_color="transparent")
        r.pack(fill="x", pady=3)
        ctk.CTkLabel(r, text=text, font=f_label, text_color=UI_TEXT, width=150,
                     anchor="w").pack(side="left")
        return build(r)

    def slider(parent, text, var, lo, hi, step, fmt):
        r = ctk.CTkFrame(parent, fg_color="transparent")
        r.pack(fill="x", pady=4)
        top = ctk.CTkFrame(r, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkLabel(top, text=text, font=f_label, text_color=UI_TEXT).pack(side="left")
        val = ctk.CTkLabel(top, text=fmt(var.get()), font=f_small, text_color=UI_ACCENT)
        val.pack(side="right")
        steps = max(1, int(round((hi - lo) / step)))
        ctk.CTkSlider(r, from_=lo, to=hi, number_of_steps=steps, variable=var, height=16,
                      button_length=16, corner_radius=8, fg_color=UI_BORDER,
                      progress_color=UI_ACCENT, button_color=UI_ACCENT,
                      button_hover_color=UI_ACCENT_HOVER).pack(fill="x", pady=(2, 0))
        var.trace_add("write", lambda *_: val.configure(text=fmt(var.get())))

    def switch(parent, text, var, command=None):
        s = ctk.CTkSwitch(parent, text=text, variable=var, command=command, font=f_label,
                          text_color=UI_TEXT, progress_color=UI_ACCENT, fg_color=UI_BORDER,
                          button_color=UI_TEXT, button_hover_color=UI_ACCENT_HOVER)
        s.pack(anchor="w", pady=4)
        return s

    def option(parent, text, var, values):
        def build(r):
            ctk.CTkOptionMenu(r, variable=var, values=values, height=32, corner_radius=8,
                              fg_color=UI_CARD_ALT, button_color=UI_BORDER,
                              button_hover_color=UI_MUTED, text_color=UI_TEXT,
                              dropdown_fg_color=UI_CARD_ALT, dropdown_hover_color=UI_BORDER,
                              dropdown_text_color=UI_TEXT, font=f_small,
                              dropdown_font=f_small).pack(fill="x", expand=True)
        return field(parent, text, build)

    def file_picker(parent, text, var, types, extra=None):
        def build(r):
            ctk.CTkEntry(r, textvariable=var, height=32, corner_radius=8, fg_color=UI_CARD_ALT,
                         border_color=UI_BORDER, text_color=UI_TEXT,
                         font=f_small).pack(side="left", fill="x", expand=True)

            def go():
                p = filedialog.askopenfilename(filetypes=types)
                if p:
                    var.set(p)
                    if extra:
                        extra()

            ctk.CTkButton(r, text="…", width=40, height=32, corner_radius=8, command=go,
                          fg_color=UI_CARD_ALT, hover_color=UI_BORDER, text_color=UI_TEXT,
                          font=f_label).pack(side="left", padx=(8, 0))
        return field(parent, text, build)

    def button(parent, text, command, primary=False, width=150):
        return ctk.CTkButton(parent, text=text, command=command, width=width, height=38,
                             corner_radius=9, font=f_btn,
                             fg_color=UI_ACCENT if primary else UI_CARD,
                             hover_color=UI_ACCENT_HOVER if primary else UI_CARD_ALT,
                             text_color="#08201d" if primary else UI_TEXT,
                             border_width=0 if primary else 1, border_color=UI_BORDER)

    src_card = card("ИСТОЧНИК")
    file_picker(src_card, "Видео", V["video"],
                [("Видео", "*.mp4 *.mov *.mkv *.avi *.webm *.gif"), ("Все", "*.*")])
    option(src_card, "Разрешение", V["res"], list(RES_PRESETS))
    option(src_card, "Кадров в секунду", V["fps"], list(FPS))

    look_card = card("ВИД")
    slider(look_card, "Размер символа (для 1080p)", V["size"], 6, 28, 1, lambda v: f"{int(v)} px")
    option(look_card, "Набор символов", V["cs"], list(CS))
    file_picker(look_card, "Файл с кодом", V["codefile"], [("Текст/код", "*.*")],
                lambda: V["cs"].set("Код (буквы из файла)"))
    field(look_card, "Свои символы", lambda r: ctk.CTkEntry(
        r, textvariable=V["chars"], height=32, corner_radius=8, fg_color=UI_CARD_ALT,
        border_color=UI_BORDER, text_color=UI_TEXT, font=f_small).pack(fill="x", expand=True))
    option(look_card, "Цвет", V["co"], list(CO))
    slider(look_card, "Мерцание букв", V["flicker"], 0, 0.3, 0.01, lambda v: f"{v:.2f}")
    slider(look_card, "Яркость цвета", V["gain"], 0.8, 2.5, 0.1, lambda v: f"{v:.1f}")
    slider(look_card, "Порог тьмы", V["dark"], 0, 0.5, 0.01, lambda v: f"{v:.2f}")

    perf_card = card("ПРОИЗВОДИТЕЛЬНОСТЬ")
    switch(perf_card, "Быстрый режим (кэш 720p)", V["proxy"])
    switch(perf_card, "Пауза в полноэкранных играх и видео", V["pause_fs"])
    switch(perf_card, "Не больше 15 FPS от батареи", V["battery"])

    PW = 430
    pv = ctk.CTkLabel(right, text="", fg_color=UI_CARD, corner_radius=12,
                      border_width=1, border_color=UI_BORDER)
    pv.pack(pady=(0, 10))
    ctk.CTkLabel(right, text="Так видео ляжет на экран. Тяни мышью — двигать, колесо — масштаб.",
                 font=f_small, text_color=UI_MUTED).pack(pady=(0, 10))

    cache = {"path": None, "frame": None}
    img_ref = {}
    pending = [None]

    def load_frame():
        p = V["video"].get()
        if p == cache["path"]:
            return
        cache["path"], cache["frame"] = p, None
        if os.path.isfile(p):
            cache["frame"] = grab_frame(p)

    def target_res():
        r = RES_PRESETS[V["res"].get()]
        if r == "auto":
            return root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = r.split("x")
        return int(w), int(h)

    def params():
        return SimpleNamespace(fit=FT[V["ft"].get()], rot=V["rot"].get(), zoom=V["zoom"].get(),
                               offx=V["offx"].get() / 100, offy=V["offy"].get() / 100,
                               flip=V["flip"].get())

    def refresh():
        pending[0] = None
        load_frame()
        Wt, Ht = target_res()
        ph = max(1, round(PW * Ht / Wt))
        if cache["frame"] is not None:
            img = preview_image(cache["frame"], Wt, Ht, PW, ph, params())
        else:
            img = Image.new("RGB", (PW, ph), (26, 30, 38))
        im = np.array(img)
        im[0, :] = im[-1, :] = (58, 66, 82)
        im[:, 0] = im[:, -1] = (58, 66, 82)
        pil = Image.fromarray(im)
        img_ref["i"] = ctk.CTkImage(light_image=pil, dark_image=pil, size=(PW, ph))
        pv.configure(image=img_ref["i"])

    def schedule(*_):
        if pending[0]:
            root.after_cancel(pending[0])
        pending[0] = root.after(40, refresh)

    for k in ("video", "res", "ft", "rot", "zoom", "offx", "offy", "flip"):
        V[k].trace_add("write", schedule)

    drag = {}

    def press(e):
        drag["x"], drag["y"] = e.x, e.y

    def move(e):
        Wt, Ht = target_res()
        V["offx"].set(round(V["offx"].get() + (e.x - drag["x"]) / PW * 100, 1))
        V["offy"].set(round(V["offy"].get() + (e.y - drag["y"]) / (PW * Ht / Wt) * 100, 1))
        drag["x"], drag["y"] = e.x, e.y

    def wheel(e):
        z = V["zoom"].get() * (1.1 if e.delta > 0 else 1 / 1.1)
        V["zoom"].set(round(min(5.0, max(0.1, z)), 2))

    pv.bind("<ButtonPress-1>", press)
    pv.bind("<B1-Motion>", move)
    pv.bind("<MouseWheel>", wheel)

    def rot_by(d):
        V["rot"].set(((V["rot"].get() + d + 180) % 360) - 180)

    def reset_frame():
        for k, v in (("rot", 0.0), ("zoom", 1.0), ("offx", 0.0), ("offy", 0.0)):
            V[k].set(v)
        V["flip"].set(False)

    frame_card = card("КАДР", right)
    fb = ctk.CTkFrame(frame_card, fg_color="transparent")
    fb.pack(fill="x", pady=(0, 8))
    for text, fn in (("↺ 90°", lambda: rot_by(-90)), ("↻ 90°", lambda: rot_by(90)),
                     ("Сбросить", reset_frame)):
        ctk.CTkButton(fb, text=text, command=fn, height=32, corner_radius=8, font=f_small,
                      fg_color=UI_CARD_ALT, hover_color=UI_BORDER,
                      text_color=UI_TEXT).pack(side="left", expand=True, fill="x", padx=3)
    switch(frame_card, "Отразить по горизонтали", V["flip"])
    option(frame_card, "Подгонка", V["ft"], list(FT))
    slider(frame_card, "Угол, °", V["rot"], -180, 180, 1, lambda v: f"{int(v)}°")
    slider(frame_card, "Масштаб", V["zoom"], 0.1, 5.0, 0.05, lambda v: f"{v:.2f}×")
    slider(frame_card, "Сдвиг по X", V["offx"], -100, 100, 0.5, lambda v: f"{v:.1f}%")
    slider(frame_card, "Сдвиг по Y", V["offy"], -100, 100, 0.5, lambda v: f"{v:.1f}%")

    def cmd(mode, **extra):
        c = SELF_CMD + [f"--run={mode}", f"--video={V['video'].get()}", f"--size={V['size'].get()}",
                        f"--charset={CS[V['cs'].get()]}", f"--chars={V['chars'].get()}",
                        f"--codefile={V['codefile'].get()}", f"--color={CO[V['co'].get()]}",
                        f"--fit={FT[V['ft'].get()]}", f"--flicker={V['flicker'].get():.3f}",
                        f"--gain={V['gain'].get():.2f}", f"--dark={V['dark'].get():.2f}",
                        f"--fps={FPS[V['fps'].get()]}", f"--res={RES_PRESETS[V['res'].get()]}",
                        f"--pause-fs={int(V['pause_fs'].get())}",
                        f"--battery={int(V['battery'].get())}",
                        f"--proxy={int(V['proxy'].get())}",
                        f"--rot={V['rot'].get():.1f}", f"--zoom={V['zoom'].get():.2f}",
                        f"--offx={V['offx'].get() / 100:.4f}",
                        f"--offy={V['offy'].get() / 100:.4f}",
                        f"--flip={int(V['flip'].get())}"]
        return c + [f"--{k.replace('_', '-')}={v}" for k, v in extra.items()]

    def save():
        os.makedirs(APPDATA, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump({k: v.get() for k, v in V.items()}, fh, ensure_ascii=False)

    def need_video():
        if not os.path.isfile(V["video"].get()):
            messagebox.showwarning("Нет видео", "Сначала выбери файл с видео.")
            return False
        save()
        return True

    def warm_proxy(then):
        """Готовит 720p-кэш в отдельном процессе, потом запускает then()."""
        if not V["proxy"].get():
            then()
            return
        dst = proxy_path(V["video"].get())
        if not dst or os.path.isfile(dst):
            then()
            return
        _write(PROXY_PROGRESS_FILE, "")
        subprocess.Popen(cmd("proxy", progress=PROXY_PROGRESS_FILE),
                         creationflags=CREATE_NO_WINDOW)

        def poll_proxy():
            try:
                with open(PROXY_PROGRESS_FILE) as fh:
                    s = fh.read().strip()
            except OSError:
                s = ""
            if s == "DONE":
                then()
                return
            if s == "ERR":
                status.set("Кэш не собрался — играю исходник")
                then()
                return
            if "/" in s:
                a, b = s.split("/")
                status.set(f"Готовлю быстрый кэш: {a}/{b} кадров")
            root.after(PROGRESS_POLL_MS, poll_proxy)

        poll_proxy()

    def preview():
        if need_video():
            def go():
                subprocess.Popen(cmd("preview"), creationflags=CREATE_NO_WINDOW)
                status.set("Превью открыто (Esc — закрыть)")
            warm_proxy(go)

    def start_wp():
        if need_video():
            def go():
                stop_wallpaper()
                subprocess.Popen(cmd("wallpaper"), creationflags=CREATE_NO_WINDOW)
                status.set("Обои запущены")
            warm_proxy(go)

    def stop_wp():
        stop_wallpaper()
        status.set("Обои остановлены")

    def export():
        if not need_video():
            return
        out = filedialog.asksaveasfilename(defaultextension=".mp4",
                                           filetypes=[("MP4", "*.mp4")])
        if not out:
            return
        if os.path.exists(EXPORT_PROGRESS_FILE):
            os.remove(EXPORT_PROGRESS_FILE)
        res = "1920x1080" if V["res"].get().startswith("Авто") else RES_PRESETS[V["res"].get()]
        subprocess.Popen(cmd("export", out=out, progress=EXPORT_PROGRESS_FILE, res=res),
                         creationflags=CREATE_NO_WINDOW)
        poll()

    def poll():
        try:
            with open(EXPORT_PROGRESS_FILE) as fh:
                s = fh.read().strip()
        except OSError:
            s = ""
        if s == "DONE":
            bar.set(1.0)
            status.set("Готово! MP4 сохранён.")
            return
        if "/" in s:
            a, b = s.split("/")
            bar.set(int(a) / max(1, int(b)))
            status.set(f"Экспорт: {a}/{b} кадров")
        root.after(PROGRESS_POLL_MS + 100, poll)

    def set_startup(on):
        import winreg
        if on:
            if not os.path.isfile(V["video"].get()):
                startup_var.set(0)
                messagebox.showwarning("Нет видео", "Сначала выбери файл с видео.")
                return
            save()
            line = subprocess.list2cmdline(cmd("wallpaper", delay=5))
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, line)
            status.set("Обои будут включаться при входе в Windows")
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, APP_NAME)
                status.set("Убрано из автозапуска")
            except OSError:
                status.set("В автозапуске ничего не было")

    actions = ctk.CTkFrame(foot, fg_color="transparent")
    actions.pack(fill="x", padx=14, pady=(14, 8))
    button(actions, "▶  Превью", preview).pack(side="left", padx=(0, 8))
    button(actions, "🖼  Поставить обои", start_wp, primary=True, width=180).pack(
        side="left", padx=(0, 8))
    button(actions, "⏹  Снять", stop_wp, width=110).pack(side="left", padx=(0, 8))
    button(actions, "💾  Экспорт MP4", export, width=160).pack(side="left", padx=(0, 8))

    startup_var = tk.IntVar(value=1 if autostart_active() else 0)
    ctk.CTkSwitch(actions, text="Автозапуск", variable=startup_var, font=f_btn,
                  text_color=UI_TEXT, progress_color=UI_ACCENT, fg_color=UI_BORDER,
                  button_color=UI_TEXT, button_hover_color=UI_ACCENT_HOVER,
                  command=lambda: set_startup(bool(startup_var.get()))).pack(side="right")

    bar = ctk.CTkProgressBar(foot, height=6, corner_radius=3, fg_color=UI_CARD_ALT,
                             progress_color=UI_ACCENT)
    bar.set(0)
    bar.pack(fill="x", padx=14)
    ctk.CTkLabel(foot, textvariable=status, font=f_small, text_color=UI_MUTED).pack(
        anchor="w", padx=14, pady=(6, 12))

    def show_banner(info):
        for w in banner.winfo_children():
            w.destroy()
        top = ctk.CTkFrame(banner, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkLabel(top, text=f"Вышло обновление — {APP_NAME} {info['version']}",
                     font=f_label, text_color=UI_ACCENT).pack(side="left")
        ctk.CTkButton(top, text="✕", width=28, height=24, corner_radius=6, font=f_small,
                      fg_color="transparent", hover_color=UI_CARD_ALT, text_color=UI_MUTED,
                      command=banner.pack_forget).pack(side="right")
        ctk.CTkLabel(banner, text=short_notes(info["body"], 300) or "Подробности на GitHub",
                     font=f_small, text_color=UI_TEXT, justify="left",
                     wraplength=760, anchor="w").pack(fill="x", padx=16, pady=(0, 6))
        row = ctk.CTkFrame(banner, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 12))
        button(row, "Что нового", lambda: open_url(info["url"]), width=140).pack(side="left")
        button(row, "Скачать", lambda: open_url(RELEASES_URL), primary=True,
               width=140).pack(side="left", padx=(8, 0))
        banner.pack(fill="x", padx=26, pady=(0, 12), before=body)

    def check_updates(force=False):
        """Сеть в отдельном потоке, чтобы окно не подвисало на холодном старте."""
        def work():
            try:
                info = update_info(force=force)
            except Exception:
                info = None
            root.after(0, lambda: show_banner(info) if info else None)
        threading.Thread(target=work, daemon=True).start()

    def on_close():
        save()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(100, refresh)
    root.after(400, check_updates)
    if os.environ.get("GLYPHWALL_GUI_TEST"):
        root.after(int(os.environ["GLYPHWALL_GUI_TEST"]), root.destroy)
    root.mainloop()



def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run", choices=["gui", "preview", "wallpaper", "export", "proxy"], default="gui")
    p.add_argument("--video", default="")
    p.add_argument("--size", type=int, default=10)
    p.add_argument("--charset", default="classic")
    p.add_argument("--chars", default="")
    p.add_argument("--codefile", default="")
    p.add_argument("--color", default="video")
    p.add_argument("--fit", default="auto")
    p.add_argument("--flicker", type=float, default=0.04)
    p.add_argument("--gain", type=float, default=1.5)
    p.add_argument("--dark", type=float, default=0.15)
    p.add_argument("--fps", type=float, default=30)
    p.add_argument("--rot", type=float, default=0)
    p.add_argument("--zoom", type=float, default=1.0)
    p.add_argument("--offx", type=float, default=0)
    p.add_argument("--offy", type=float, default=0)
    p.add_argument("--flip", type=int, default=0)
    p.add_argument("--res", default="auto")
    p.add_argument("--pause-fs", dest="pause_fs", type=int, default=1)
    p.add_argument("--battery", type=int, default=1)
    p.add_argument("--proxy", type=int, default=1)
    p.add_argument("--no-proxy", dest="no_proxy", type=int, default=0)
    p.add_argument("--delay", type=float, default=0)
    p.add_argument("--out", default="glyphwall_video.mp4")
    p.add_argument("--progress", default="")
    p.add_argument("--max-frames", dest="max_frames", type=int, default=0)
    p.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    p.add_argument("--check-update", dest="check_update", action="store_true",
                   help="проверить обновление и выйти (код 0 — есть новее, 1 — нет)")
    a = p.parse_args()
    if a.check_update:
        info = update_info(force=True)
        if info:
            print(f"{info['version']}\n{info['body']}\n{info['url']}")
            sys.exit(0)
        print("обновлений нет")
        sys.exit(1)
    if a.run == "gui":
        gui()
    elif a.run == "export":
        run_export(a)
    elif a.run == "proxy":
        run_proxy(a)
    else:
        run_player(a, wallpaper=(a.run == "wallpaper"))


if __name__ == "__main__":
    main()
