# -*- coding: utf-8 -*-
"""
鳄鱼派 · 沪深300 基差悬浮窗  v1.0.0
================================================================
基差 = 沪深300 现货指数 − IF 全挂牌合约「持仓量加权」价
     （与文华财经 "300股指加权" 口径一致，自动换月、自动跳过已到期合约）
数据源：新浪财经免费行情（无需账号、免费）

功能
----
· 无边框小浮窗，始终置顶；加了 WS_EX_TOOLWINDOW，按 Win+D（显示桌面）不会被最小化
· 默认只显示基差；点右上角 ▾ 展开详情（现货 / 期货加权 / 各合约持仓权重）
· 左键拖动移动；双击切换展开/收起；右键菜单（刷新间隔 / 置顶 / 开机自启 / 退出）
· 位置、展开状态、刷新间隔自动记忆

运行
----
双击本文件即可（会自动用 pythonw 无控制台启动）
命令行调试： python 鳄鱼派_沪深300基差.py --console
退出：点右上角 × 或右键菜单 → 退出

License: MIT
"""

import ctypes
import datetime
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox

import requests

# ================== 可调参数 ==================
REFRESH_SEC = 3       # 默认刷新间隔（秒），最小 1
EXTRA_QUARTERS = 3    # 探测后续季月合约数量
INTERVAL_CHOICES = (1, 2, 3, 5, 10, 30)
# =============================================

APP_NAME = "鳄鱼派 沪深300基差"
APP_VERSION = "1.2.1"
APP_TITLE = "%s v%s" % (APP_NAME, APP_VERSION)
HEAD_TITLE = "鳄鱼派 · 沪深300基差"   # 窗体内左上角标题
REPO = "wwwwww1239/hs300-basis-widget"
REPO_URL = "https://github.com/" + REPO
UPDATE_API = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_API = "https://api.github.com/repos/%s/releases?per_page=10" % REPO
CFG_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "HS300BasisWidget")
CFG_PATH = os.path.join(CFG_DIR, "settings.json")

UPD_W, UPD_H = 324, 190      # 更新提示浮层尺寸（带更新说明和翻页按钮）
EDGE_PAD = 8                 # 面板离屏幕边缘留的余量

HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}

# ---------- 配色（深色） ----------
COL = {
    "bg":    "#0f131b",
    "bg2":   "#151b26",
    "line":  "#242c3b",
    "fg":    "#e9edf5",
    "sub":   "#8b94a8",
    "dim":   "#5a6377",
    "up":    "#ff5c5c",   # 红 = 基差为正（现货升水）/ 上涨
    "down":  "#2fd07a",   # 绿 = 基差为负（期货升水）/ 下跌
    "warn":  "#f0b429",
    "err":   "#ff5c5c",
    "ok":    "#2fd07a",
}
TRANSPARENT_KEY = "#010203"

FN = "Microsoft YaHei UI"
FNUM = "Segoe UI"

COMPACT_W = 248
COMPACT_H = 124
EXPANDED_W = 306


# ==================================================================
#                        行情数据层（口径同原脚本）
# ==================================================================
def gen_if_codes():
    """当月 + 下月 + 后续几个季月(3/6/9/12)，多探一档做容错。"""
    today = datetime.date.today()
    codes = [today.strftime("IF%y%m")]

    if today.month == 12:
        nm = today.replace(year=today.year + 1, month=1, day=1)
    else:
        nm = today.replace(month=today.month + 1, day=1)
    codes.append(nm.strftime("IF%y%m"))

    qm_list = (3, 6, 9, 12)
    y, m = nm.year, nm.month
    found = 0
    while found < EXTRA_QUARTERS:
        m += 1
        if m > 12:
            m = 1
            y += 1
        if m in qm_list:
            codes.append("IF%02d%02d" % (y % 100, m))
            found += 1

    seen, uniq = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


_CODE_CACHE = {"day": None, "codes": []}


def get_codes():
    today = datetime.date.today()
    if _CODE_CACHE["day"] != today:
        _CODE_CACHE["day"] = today
        _CODE_CACHE["codes"] = gen_if_codes()
    return _CODE_CACHE["codes"]


def fetch_sina(sess, symbols):
    """新浪行情：返回 {key: [field, ...]}"""
    url = "http://hq.sinajs.cn/list=" + ",".join(symbols)
    resp = sess.get(url, headers=HEADERS, timeout=8)
    resp.encoding = "gbk"
    out = {}
    for line in resp.text.strip().split("\n"):
        if '="' not in line:
            continue
        head, body = line.split('="', 1)
        key = head.split("hq_str_")[-1]
        out[key] = body.rstrip('";').split(",")
    return out


def parse_spot(f):
    """
    sh000300 字段： [0]名称 [1]今开 [2]昨收 [3]最新 [4]最高 [5]最低
                   [8]成交量 [9]成交额 [30]日期 [31]时间
    """
    if not f or len(f) < 32:
        return None
    try:
        price = float(f[3])
        if price <= 0:
            return None
        return {
            "price": price,
            "open": float(f[1]),
            "preclose": float(f[2]),
            "high": float(f[4]),
            "low": float(f[5]),
            "amount": float(f[9]),
            "date": f[30],
            "time": f[31],
        }
    except (ValueError, IndexError):
        return None


def parse_if(code, f):
    """
    CFF_RE_IFxxxx 字段： [0]今开 [1]昨结算 [2]最低 [3]最新 [4]成交量
                        [6]持仓量 [14]昨收盘 [36]日期 [37]时间 [49]合约名称
    涨跌以「昨收盘」为基准 —— 与文华终端显示一致（若昨收缺失才退回昨结算）。
    """
    if not f or len(f) < 38:
        return None
    try:
        price = float(f[3])
        volume = float(f[4])
        oi = float(f[6])
        oi_y = float(f[15]) if len(f) > 15 and f[15] else 0.0
        # 盘前合约还没成交：持仓/成交量都是 0，但昨持仓还在，不能直接丢掉
        if price <= 0 or (oi <= 0 and volume <= 0 and oi_y <= 0):
            return None
        pre_settle = float(f[1])
        pre_close = float(f[14]) if len(f) > 14 and f[14] else 0.0
        if pre_close <= 0:
            pre_close = pre_settle
        return {
            "code": code,
            "price": price,
            "pre_settle": pre_settle,
            "pre_close": pre_close,
            "oi_y": oi_y,
            "open": float(f[0]),
            "low": float(f[2]),
            "volume": volume,
            "oi": oi,
            "date": f[36],
            "time": f[37] if len(f) > 37 else "",
        }
    except (ValueError, IndexError):
        return None


def build_snapshot(sess):
    """抓一次行情，算出基差。返回 dict；取不到数据时抛异常。

    相位 phase：
      pre   —— 盘前 / 非交易日。现货常常还挂在昨天 15:00 的收盘，
               而 IF 合约已经切到今天（09:00 就换了日期），
               此时「今天的期货 − 昨天的现货」全是错的，
               所以统一退回「昨收口径」：现货昨收 − Σ(昨收×昨持仓)/Σ昨持仓
      live  —— 盘中且 90 秒内有新数据
      stale —— 盘中但数据旧了（收盘后 / 断网）
    """
    codes = get_codes()
    syms = ["sh000300"] + ["CFF_RE_%s" % c.upper() for c in codes]
    raw = fetch_sina(sess, syms)

    spot = parse_spot(raw.get("sh000300"))
    if spot is None:
        raise RuntimeError("现货行情为空")

    cands = []
    for c in codes:
        info = parse_if(c, raw.get("CFF_RE_%s" % c.upper()))
        if info:
            cands.append(info)
    if not cands:
        raise RuntimeError("未取到 IF 合约行情")

    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    spot_day = spot["date"]

    try:
        data_dt = datetime.datetime.strptime("%s %s" % (spot["date"], spot["time"]),
                                             "%Y-%m-%d %H:%M:%S")
    except Exception:
        data_dt = now

    # 现货日期是不是「今天」，决定这一刻到底算不算盘中
    same_day = spot_day >= today
    phase = "live" if (same_day and (now - data_dt).total_seconds() <= 90) else (
        "stale" if same_day else "pre")

    # 盘中：以现货的日期为准（不再用期货的 max，免得合约日期参差时权重被砍）
    live = cands
    if same_day:
        live = [c for c in cands if c["date"] == spot_day]
        if not live:                       # 期货全停在昨天 -> 按盘前处理
            phase = "pre"

    drop_n = 0
    if phase == "pre":
        # 昨收口径：只用昨收价 + 昨日持仓，两个数天然同一天同一时刻
        live = [c for c in cands if c["oi_y"] > 0] or cands
        wts = [(c["oi_y"] or c["oi"], c) for c in live]
        tot_oi = sum(w for w, _ in wts)
        if tot_oi <= 0:
            raise RuntimeError("合约持仓为 0")
        wp = sum(c["pre_close"] * w for w, c in wts) / tot_oi
        wp_pre = wp
        spot_px = spot["preclose"]
    else:
        drop_n = len(cands) - len(live)
        wts = [(c["oi"], c) for c in live]
        tot_oi = sum(w for w, _ in wts)
        if tot_oi <= 0:
            raise RuntimeError("合约持仓为 0")
        wp = sum(c["price"] * w for w, c in wts) / tot_oi
        # 加权基准价：文华用「昨日持仓量 × 昨日收盘」加权（换手导致的权重变化不影响当日涨跌）
        tot_oi_y = sum(c["oi_y"] for c in live)
        if tot_oi_y > 0:
            wp_pre = sum(c["pre_close"] * c["oi_y"] for c in live) / tot_oi_y
        else:
            wp_pre = sum(c["pre_close"] * c["oi"] for c in live) / tot_oi
        spot_px = spot["price"]

    rows = []
    for w, c in wts:
        px = c["pre_close"] if phase == "pre" else c["price"]
        rows.append({
            "code": c["code"],
            "price": px,
            "chg": px - c["pre_close"],
            "oi": c["oi_y"] if (phase == "pre" and c["oi_y"]) else c["oi"],
            "vol": c["volume"],
            "w": w / tot_oi,
        })
    rows.sort(key=lambda r: -r["oi"])

    basis = spot_px - wp
    basis_pct = basis / spot_px * 100.0 if spot_px else 0.0

    # 盘中：现货与各合约时间戳的最大偏差（秒），>120 秒基本就是有一侧没跟上
    skew = 0
    if phase != "pre":
        def _sec(t):
            try:
                return int(t[0:2]) * 3600 + int(t[3:5]) * 60 + int(t[6:8])
            except (ValueError, IndexError, TypeError):
                return None
        s0 = _sec(spot["time"])
        if s0 is not None:
            for c in live:
                s1 = _sec(c["time"])
                if s1 is not None and s1 > 0:
                    skew = max(skew, abs(s1 - s0))

    if phase == "pre":
        spot = dict(spot)
        spot["price"] = spot_px

    return {
        "spot": spot,
        "rows": rows,
        "wp": wp,
        "wp_pre": wp_pre,
        "wp_chg": wp - wp_pre,
        "wp_chg_pct": (wp - wp_pre) / wp_pre * 100.0 if wp_pre else 0.0,
        "basis": basis,
        "basis_pct": basis_pct,
        "tot_oi": tot_oi,
        "data_dt": data_dt,
        "phase": phase,
        "live": phase == "live",
        "skew": skew,
        "drop_n": drop_n,
        "ts": now,
    }


def _in_trading_hours(now):
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (datetime.time(9, 30) <= t <= datetime.time(11, 30)) or \
           (datetime.time(13, 0) <= t <= datetime.time(15, 0))


class Feed(threading.Thread):
    """后台抓行情线程，结果丢进 queue。"""

    def __init__(self, q, get_interval):
        super().__init__(daemon=True)
        self.q = q
        self.get_interval = get_interval
        self._stop = threading.Event()
        self._wake = threading.Event()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def kick(self):
        self._wake.set()

    def run(self):
        sess = requests.Session()
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.q.put(("data", build_snapshot(sess)))
            except Exception as e:
                self.q.put(("error", "%s: %s" % (type(e).__name__, e)))
            wait = max(0.4, float(self.get_interval()) - (time.time() - t0))
            self._wake.wait(wait)
            self._wake.clear()


# ==================================================================
#                        配置 / 系统小工具
# ==================================================================
def load_cfg():
    try:
        with open(CFG_PATH, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_cfg(d):
    try:
        os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
        tmp = CFG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG_PATH)
    except Exception:
        pass


def _startup_lnk():
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup", APP_NAME + ".lnk")


def autostart_enabled():
    return os.path.exists(_startup_lnk())


def set_autostart(on):
    """在「启动」文件夹里建立/删除快捷方式。返回 (ok, msg)。"""
    lnk = _startup_lnk()
    try:
        if not on:
            if os.path.exists(lnk):
                os.remove(lnk)
            return True, "已关闭开机自启"
        if getattr(sys, "frozen", False):     # PyInstaller 打包后的 exe
            exe = sys.executable or ""
            script = ""
            wd = os.path.dirname(exe)
        else:
            exe = sys.executable or ""
            if exe.lower().endswith("python.exe"):
                pyw = exe[:-len("python.exe")] + "pythonw.exe"
                if os.path.exists(pyw):
                    exe = pyw
            script = os.path.abspath(__file__)
            wd = os.path.dirname(script)
        q = lambda s: s.replace("'", "''")
        args = ('"%s"' % q(script)) if script else ""
        ps = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
            "$s.TargetPath='%s';$s.Arguments='%s';"
            "$s.WorkingDirectory='%s';$s.IconLocation='%s,0';$s.Save()"
        ) % (q(lnk), q(exe), args, q(wd), q(exe))
        os.makedirs(os.path.dirname(lnk), exist_ok=True)
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=40, creationflags=0x08000000)
        if os.path.exists(lnk):
            return True, "已加入开机自启"
        return False, (r.stderr.decode("mbcs", "ignore") or "创建快捷方式失败")[:120]
    except Exception as e:
        return False, str(e)[:120]


# ---------------- Win32：置顶 + 防 Win+D ----------------
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOPMOST = 0x00000008
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0001, 0x0002, 0x0010, 0x0020


_U32 = ctypes.windll.user32
try:
    from ctypes import wintypes
    _U32.GetParent.argtypes = [wintypes.HWND]
    _U32.GetParent.restype = wintypes.HWND
    _U32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _U32.GetWindowLongW.restype = ctypes.c_long
    _U32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    _U32.SetWindowLongW.restype = ctypes.c_long
    _U32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    _U32.SetWindowPos.restype = wintypes.BOOL
    _U32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _U32.ShowWindow.restype = wintypes.BOOL
    _U32.IsIconic.argtypes = [wintypes.HWND]
    _U32.IsIconic.restype = wintypes.BOOL
    _U32.IsWindowVisible.argtypes = [wintypes.HWND]
    _U32.IsWindowVisible.restype = wintypes.BOOL
except Exception:
    pass


def get_hwnd(root):
    try:
        h = _U32.GetParent(root.winfo_id())
        return h or root.winfo_id()
    except Exception:
        return 0


def apply_tool_window(hwnd):
    """WS_EX_TOOLWINDOW：不出现在任务栏 / Alt+Tab，且 Win+D(显示桌面) 不会被最小化。"""
    if not hwnd:
        return
    try:
        style = _U32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        new = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        if new != style:
            _U32.SetWindowLongW(hwnd, GWL_EXSTYLE, ctypes.c_long(new))
        _U32.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0,
                          SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED)
    except Exception:
        pass


# ==================================================================
#                              界面
# ==================================================================
class BasisWidget:
    def __init__(self, root):
        self.root = root
        self.cfg = load_cfg()
        self.interval = int(self.cfg.get("interval", REFRESH_SEC) or REFRESH_SEC)
        self.expanded = bool(self.cfg.get("expanded", False))
        self.topmost = bool(self.cfg.get("topmost", True))
        self.alive = True
        self.snap = None
        self.err = None
        self.err_count = 0
        self.W, self.H = COMPACT_W, COMPACT_H
        self.drag = None
        self.resize = None
        self.autostart_var = None
        self.update = None          # 发现的可用新版本
        self.releases = []          # 最近几个版本（含更新说明），弹窗左右翻页用
        self.upd_idx = 0            # 当前翻到第几个
        self.upd_win = None         # 更新提示小窗
        self._upd_busy = False
        self._updating = False      # 正在下载/安装更新

        try:
            self.S = min(2.5, max(0.75, float(self.cfg.get("scale", 1.0) or 1.0)))
        except (TypeError, ValueError):
            self.S = 1.0
        if "scale" not in self.cfg:
            # 首次运行：按系统 DPI 放大，125% 屏上默认 1.25 倍，避免字太小
            try:
                self.S = min(2.0, max(1.0, round(root.winfo_fpixels("1i") / 96.0, 2)))
            except Exception:
                self.S = 1.0
        root.overrideredirect(True)
        root.configure(bg=TRANSPARENT_KEY)
        try:
            root.attributes("-transparentcolor", TRANSPARENT_KEY)
        except Exception:
            root.configure(bg=COL["bg"])
        root.attributes("-topmost", bool(self.topmost))
        try:
            root.attributes("-alpha", 1.0)
        except Exception:
            pass

        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        x = self.cfg.get("x")
        y = self.cfg.get("y")
        if not isinstance(x, int) or not isinstance(y, int) or x < -50 or y < -50 \
                or x > sw - 40 or y > sh - 40:
            x, y = sw - COMPACT_W - 60, 90
        self.pos = [x, y]
        self._last_size = None            # 上一次实际尺寸，用来判断展开时该往哪边长

        self.cv = tk.Canvas(root, bg=TRANSPARENT_KEY, highlightthickness=0, bd=0,
                            width=COMPACT_W, height=COMPACT_H)
        self.cv.place(x=0, y=0, relwidth=1, relheight=1)

        self.cv.bind("<Button-1>", self._on_press)
        self.cv.bind("<B1-Motion>", self._on_drag)
        self.cv.bind("<ButtonRelease-1>", self._on_release)
        self.cv.bind("<Double-Button-1>", self._on_double)
        self.cv.bind("<Button-3>", self._on_menu)

        self.q = queue.Queue()
        self.feed = Feed(self.q, lambda: self.interval)
        self.feed.start()

        self.hwnd = get_hwnd(root)
        apply_tool_window(self.hwnd)

        self._apply_geometry()
        self.render()
        self.root.after(120, self._poll)
        self.root.after(1000, self._watchdog)
        self.root.after(8000, lambda: self.check_update_async(False))

    # ---------------- 布局 ----------------
    def _size(self):
        n = max(1, len(self.snap["rows"]) if self.snap else 3)
        if self.expanded:
            return EXPANDED_W, 286 + 20 * n
        return COMPACT_W, COMPACT_H

    def _apply_geometry(self, anchor=True):
        """anchor=True：贴边时保持「靠边的那条边」不动，朝屏幕内部长，
        这样点开详情不会把整个面板挤到旁边，收起后也会回到原位。"""
        w, h = self._size()
        w = int(round(w * self.S))
        h = int(round(h * self.S))
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        pad = EDGE_PAD
        pw, ph = self._last_size if self._last_size else (w, h)
        x, y = self.pos[0], self.pos[1]
        if anchor:
            if x + w > sw - pad:                 # 右边放不下 -> 右边缘对齐，向左长
                x = max(pad, min(x + (pw - w), sw - w - pad))
            if y + h > sh - pad:                 # 下边放不下 -> 下边缘对齐，向上长
                y = max(pad, min(y + (ph - h), sh - h - pad))
        if x + w > sw - pad:                     # 屏幕都装不下，只能贴左/上
            x = pad
        if y + h > sh - pad:
            y = pad
        if x < -10:
            x = -10
        if y < -10:
            y = -10
        self._last_size = (w, h)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        try:
            self.root.update_idletasks()
        except Exception:
            pass

    def _font(self, size, weight="normal", family=None):
        return (family or FN, max(1, int(round(size * self.S))), weight)

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
               x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
               x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.cv.create_polygon(pts, smooth=True, splinesteps=20, **kw)

    def _btn(self, x, y, glyph, tag, size=9, fam=None):
        t = self.cv.create_text(x, y, text=glyph, fill=COL["dim"],
                                font=self._font(size, "bold", fam), tags=(tag, "nodrag"))
        self.cv.tag_bind(tag, "<Enter>", lambda e: self.cv.itemconfigure(t, fill=COL["fg"]))
        self.cv.tag_bind(tag, "<Leave>", lambda e: self.cv.itemconfigure(t, fill=COL["dim"]))

    # ---------------- 绘制 ----------------
    def render(self):
        cv = self.cv
        cv.delete("all")
        W, H = self._size()
        self.W, self.H = W, H
        s = self.snap
        status = self._status()

        self._round_rect(0.5, 0.5, W - 0.5, H - 0.5, 12,
                         fill=COL["bg"], outline=COL["line"], width=1)

        # --- 头部 ---
        dot_col = {"live": COL["ok"], "stale": COL["warn"], "pre": COL["sub"],
                   "error": COL["err"], "init": COL["dim"]}.get(status, COL["dim"])
        cv.create_oval(13, 16, 20, 23, fill=dot_col, outline="")
        cv.create_text(26, 20, text=HEAD_TITLE, anchor="w", fill=COL["sub"],
                       font=self._font(8.5, "bold"))
        self._btn(W - 22, 19, "×", "btn_close", 12)
        self._btn(W - 50, 19, "▴" if self.expanded else "▾", "btn_toggle", 10)

        # --- 基差大数字 ---
        if s:
            basis = s["basis"]
            col = COL["up"] if basis > 0 else (COL["down"] if basis < 0 else COL["fg"])
            if status == "error":
                col = COL["dim"]
            txt = "%+.2f" % basis
            sub = "%+.2f%%" % s["basis_pct"]
            sub_col = col
            foot = "%.2f → %.2f" % (s["spot"]["price"], s["wp"])
            tcol = COL["dim"] if status in ("stale", "pre") else COL["sub"]
            tail = s["data_dt"].strftime("%H:%M")
            if status == "pre":
                tail += "·昨收"
            elif status == "stale":
                tail += "·收盘" if not _in_trading_hours(s["data_dt"]) else "·延迟"
            elif s.get("skew", 0) > 120 or s.get("drop_n", 0) > 0:
                tail += "·不同步"
        else:
            col, txt = COL["dim"], "--.--"
            sub = "行情获取失败，重试中…" if self.err else "正在获取行情…"
            foot, tail, sub_col, tcol = "", "", COL["dim"], COL["dim"]

        cv.create_text(W / 2.0, 60, text=txt, fill=col, anchor="center",
                       font=self._font(30, "bold", FNUM))
        cv.create_text(W / 2.0, 93, text=sub, fill=sub_col, anchor="center",
                       font=self._font(9))
        cv.create_text(14, 110, text=foot, fill=tcol, anchor="w",
                       font=self._font(7.5, family=FNUM))
        if tail:
            cv.create_text(W - 34, 110, text=tail, fill=tcol, anchor="e",
                           font=self._font(7, family=FNUM))

        if self.expanded:
            self._render_detail(W)

        # 右下角缩放抓手（三道斜线，拖它就能整体放大/缩小）
        for k in range(3):
            x2, y2 = W - 9 - k * 4, H - 9 - k * 4
            cv.create_line(x2 - 6, y2, x2, y2 - 6,
                           fill="#43506b", width=1, capstyle="round")

        if self.S != 1.0:
            cv.scale("all", 0, 0, self.S, self.S)

    def _render_detail(self, W):
        cv = self.cv
        s = self.snap
        y = COMPACT_H

        cv.create_line(14, y + 8, W - 14, y + 8, fill=COL["line"])

        def row_label(x, yy, text, col=COL["sub"], size=9, weight="normal", anchor="w", fam=None):
            cv.create_text(x, yy, text=text, fill=col, anchor=anchor,
                           font=self._font(size, weight, fam))

        if not s:
            row_label(W / 2.0, y + 40, "等待行情数据…", COL["dim"], 9, anchor="center")
            return

        sp = s["spot"]
        # 现货
        cv.create_text(16, y + 30, text="现货", anchor="w", fill=COL["dim"],
                       font=self._font(8))
        cv.create_text(46, y + 30, text="%.2f" % sp["price"], anchor="w", fill=COL["fg"],
                       font=self._font(11, "bold", FNUM))
        schg = sp["price"] - sp["preclose"]
        spct = schg / sp["preclose"] * 100 if sp["preclose"] else 0.0
        scol = COL["up"] if schg > 0 else (COL["down"] if schg < 0 else COL["sub"])
        cv.create_text(W - 16, y + 30, text="%+.2f  %+.2f%%" % (schg, spct), anchor="e",
                       fill=scol, font=self._font(9, family=FNUM))
        row_label(16, y + 50,
                  "开 %.2f   高 %.2f   低 %.2f" % (sp["open"], sp["high"], sp["low"]),
                  COL["dim"], 7.5)

        # 期货加权
        cv.create_text(16, y + 76, text="加权", anchor="w", fill=COL["dim"],
                       font=self._font(8))
        cv.create_text(46, y + 76, text="%.2f" % s["wp"], anchor="w", fill=COL["fg"],
                       font=self._font(11, "bold", FNUM))
        wcol = COL["up"] if s["wp_chg"] > 0 else (COL["down"] if s["wp_chg"] < 0 else COL["sub"])
        cv.create_text(W - 16, y + 76, text="%+.2f  %+.2f%%" % (s["wp_chg"], s["wp_chg_pct"]),
                       anchor="e", fill=wcol, font=self._font(9, family=FNUM))

        cv.create_line(14, y + 92, W - 14, y + 92, fill=COL["line"])

        # 合约表
        c_px, c_price, c_chg, c_oi = 16, 120, 172, 232
        hy = y + 106
        row_label(c_px, hy, "合约", COL["dim"], 7.5)
        row_label(c_price, hy, "最新", COL["dim"], 7.5, anchor="e")
        row_label(c_chg, hy, "涨跌", COL["dim"], 7.5, anchor="e")
        row_label(c_oi, hy, "持仓", COL["dim"], 7.5, anchor="e")
        row_label(W - 16, hy, "权重", COL["dim"], 7.5, anchor="e")

        ry = y + 124
        for r in s["rows"]:
            rcol = COL["up"] if r["chg"] > 0 else (COL["down"] if r["chg"] < 0 else COL["sub"])
            row_label(c_px, ry, r["code"], COL["fg"], 8.5, "bold", fam=FNUM)
            row_label(c_price, ry, "%.2f" % r["price"], COL["fg"], 8.5, anchor="e", fam=FNUM)
            row_label(c_chg, ry, "%+.2f" % r["chg"], rcol, 8.5, anchor="e", fam=FNUM)
            row_label(c_oi, ry, "{:,}".format(int(r["oi"])), COL["sub"], 8.5, anchor="e", fam=FNUM)
            row_label(W - 16, ry, "%.1f%%" % (r["w"] * 100), COL["sub"], 8.5, anchor="e", fam=FNUM)
            ry += 20

        dy = ry + 2
        cv.create_line(14, dy, W - 14, dy, fill=COL["line"])
        amt = (" · 成交 %.0f亿" % (sp["amount"] / 1e8)) if sp.get("amount") else ""
        cv.create_text(W / 2.0, dy + 16,
                       text="持仓量加权(文华口径)%s · %ds" % (amt, self.interval),
                       fill=COL["dim"], anchor="center", font=self._font(7))

    # ---------------- 状态 / 数据轮询 ----------------
    def _status(self):
        if self.err and (self.snap is None or self.err_count >= 3):
            return "error"
        if self.snap is None:
            return "init"
        return self.snap.get("phase") or ("live" if self.snap.get("live") else "stale")

    def _poll(self):
        if not self.alive:
            return
        dirty = False
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "data":
                    self.snap = payload
                    self.err, self.err_count = None, 0
                    dirty = True
                elif kind == "update":
                    self._on_update_info(payload[0], payload[1])
                elif kind == "upderr":
                    self._on_upd_err(payload)
                elif kind == "quit":
                    self.close()
                    return
                else:
                    self.err_count += 1
                    self.err = payload
                    dirty = True
        except queue.Empty:
            pass
        if dirty:
            if self._size() != (self.W, self.H):
                self._apply_geometry()
            self.render()
        self.root.after(180, self._poll)

    def _watchdog(self):
        """Win+D / 被其它窗口压下去时，自动把浮窗重新显示并置顶。"""
        if not self.alive:
            return
        try:
            if self.topmost and self.hwnd:
                if _U32.IsIconic(self.hwnd):
                    _U32.ShowWindow(self.hwnd, 9)          # SW_RESTORE
                elif not _U32.IsWindowVisible(self.hwnd):
                    _U32.ShowWindow(self.hwnd, 4)          # SW_SHOWNOACTIVATE
                if not (_U32.GetWindowLongW(self.hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST):
                    _U32.SetWindowPos(self.hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0,
                                      SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception:
            pass
        self.root.after(900, self._watchdog)

    # ---------------- 交互 ----------------
    def _hit(self, x, y):
        """返回命中的按钮名：'close' / 'toggle' / 'grip' / None"""
        s = self.S
        W = self.W * s
        H = self.H * s
        if x >= W - 22 * s and y >= H - 22 * s:
            return "grip"
        if abs(x - (W - 22 * s)) <= 12 * s and abs(y - 19 * s) <= 12 * s:
            return "close"
        if abs(x - (W - 50 * s)) <= 12 * s and abs(y - 19 * s) <= 12 * s:
            return "toggle"
        return None

    def _on_press(self, e):
        hit = self._hit(e.x, e.y)
        if hit == "close":
            self.close()
            return
        if hit == "toggle":
            self.toggle()
            return
        if hit == "grip":
            self.resize = (e.x_root, e.y_root, self.S)
            return
        self.drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _on_drag(self, e):
        if self.resize is not None:
            x0, y0, s0 = self.resize
            bw, bh = self._size()
            dx, dy = e.x_root - x0, e.y_root - y0
            if abs(dx) >= abs(dy):
                ns = (bw * s0 + dx) / float(bw)
            else:
                ns = (bh * s0 + dy) / float(bh)
            # 拖到什么程度就长多大，但不许把窗口顶出屏幕（左上角固定不动）
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            ns = min(ns, (sw - EDGE_PAD - self.root.winfo_x()) / float(bw),
                     (sh - EDGE_PAD - self.root.winfo_y()) / float(bh))
            ns = min(2.5, max(0.75, ns))
            if abs(ns - self.S) >= 0.005:
                self.S = round(ns, 3)
                self._apply_geometry(anchor=False)
                self.render()
            return
        if self.drag is None:
            return
        x = e.x_root - self.drag[0]
        y = e.y_root - self.drag[1]
        self.pos = [x, y]
        self.root.geometry("+%d+%d" % (x, y))

    def _on_release(self, e):
        if self.resize is not None:
            self.resize = None
            self.cfg["scale"] = self.S
            save_cfg(self.cfg)
        if self.drag is not None:
            self.cfg["x"], self.cfg["y"] = self.pos[0], self.pos[1]
            save_cfg(self.cfg)
        self.drag = None

    def _on_double(self, e):
        if self._hit(e.x, e.y):
            return
        self.toggle()

    def toggle(self):
        self.expanded = not self.expanded
        self.cfg["expanded"] = self.expanded
        save_cfg(self.cfg)
        self._apply_geometry(anchor=True)     # 贴边展开时朝屏幕里长，不把面板挤走
        self.render()

    def _build_menu(self):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label=APP_TITLE, state="disabled")
        if self.update:
            m.add_command(label="⬆ 新版本 v%s · 点此更新" % self.update["version"],
                          command=self.do_update)
        else:
            m.add_command(label="检查更新（当前 v%s）" % APP_VERSION,
                          command=lambda: self.check_update_async(True))
        m.add_separator()
        m.add_command(label="收起详情" if self.expanded else "展开详情", command=self.toggle)
        m.add_separator()
        sub = tk.Menu(m, tearoff=0)
        for iv in INTERVAL_CHOICES:
            sub.add_radiobutton(label="%d 秒" % iv, value=iv,
                                command=lambda v=iv: self.set_interval(v))
        m.add_cascade(label="刷新间隔（当前 %ds）" % self.interval, menu=sub)
        m.add_command(label="立即刷新", command=self.refresh_now)
        m.add_separator()

        topvar = tk.BooleanVar(value=self.topmost)
        m.add_checkbutton(label="始终置顶", variable=topvar, command=lambda: self.set_topmost(topvar.get()))
        autov = tk.BooleanVar(value=autostart_enabled())
        m.add_checkbutton(label="开机自启动", variable=autov,
                          command=lambda: self.set_autostart(autov.get()))
        m.add_command(label="回到屏幕右上角", command=self.reset_pos)
        m.add_separator()
        m.add_command(label="退出", command=self.close)
        return m

    def _on_menu(self, e):
        m = self._build_menu()
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def set_interval(self, v):
        self.interval = int(v)
        self.cfg["interval"] = self.interval
        save_cfg(self.cfg)
        self.feed.kick()
        self.render()

    def refresh_now(self):
        self.feed.kick()

    # ---------------- 在线更新 ----------------
    def check_update_async(self, manual=False):
        if self._upd_busy:
            return
        self._upd_busy = True

        def job():
            try:
                rels = fetch_releases()
            except Exception as e:
                rels = ("err", str(e))
            if isinstance(rels, tuple):
                self.q.put(("upderr", rels[1]))
            else:
                self.q.put(("update", (rels, manual)))

        threading.Thread(target=job, daemon=True).start()

    def _on_update_info(self, rels, manual):
        self._upd_busy = False
        if not rels:
            if manual:
                messagebox.showinfo(APP_NAME, "没查到 Release：可能没发布，或网络连不上 GitHub。")
            return
        latest = rels[0]
        if _ver_tuple(latest["version"]) <= _ver_tuple(APP_VERSION):
            if manual:
                messagebox.showinfo(APP_NAME, "当前已是最新版本 v%s。" % APP_VERSION)
            return
        if self.cfg.get("skip_version") == latest["version"]:
            return
        self.releases = rels
        self.upd_idx = 0
        self.update = latest
        self._show_update_popup()

    def _show_update_popup(self):
        if not self.releases:
            return
        if self.upd_win is not None:
            try:
                self.upd_win.destroy()
            except Exception:
                pass
            self.upd_win = None
        w, h = int(UPD_W * self.S), int(UPD_H * self.S)
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=TRANSPARENT_KEY)
        try:
            win.attributes("-transparentcolor", TRANSPARENT_KEY)
        except Exception:
            win.configure(bg=COL["bg"])
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        # 用窗口的实际位置（贴边展开时 self.pos 可能和真实坐标不一样）
        try:
            wx, wy = self.root.winfo_x(), self.root.winfo_y()
            ww, wh = self.root.winfo_width(), self.root.winfo_height()
        except Exception:
            wx, wy, ww = self.pos[0], self.pos[1], int(self.W * self.S)
            wh = int(self.H * self.S)
        x = max(0, min(wx, sw - w))
        y = wy + wh + 8
        if y + h > sh - 40:
            y = max(0, wy - h - 8)
        win.geometry("%dx%d+%d+%d" % (w, h, x, y))

        cv = tk.Canvas(win, bg=TRANSPARENT_KEY, highlightthickness=0, bd=0)
        cv.pack(fill="both", expand=True)
        cv.bind("<Button-1>", self._on_upd_click)
        self._upd_cv = cv
        self.upd_win = win
        self._render_upd_popup()
        # 先画完再挂图标，保证首帧就有内容
        try:
            _apply_icon(win)
        except Exception:
            pass

    def _render_upd_popup(self):
        cv = self._upd_cv
        if cv is None or not self.releases:
            return
        cv.delete("all")
        w, h = UPD_W, UPD_H
        it = self.releases[self.upd_idx]
        newest = (self.upd_idx == 0)
        self._round_rect_cv(cv, 0.5, 0.5, w - 0.5, h - 0.5, 10,
                            fill=COL["bg2"], outline=COL["line"], width=1)
        title = ("发现新版本 v%s" % it["version"]) if newest else ("v%s · 更新说明" % it["version"])
        cv.create_text(14, 20, text=title, anchor="w", fill=COL["fg"],
                       font=(FN, 10, "bold"))
        cv.create_text(w - 16, 19, text="×", fill=COL["dim"],
                       font=(FN, 11, "bold"), tags=("close",))
        meta = [x for x in (it.get("date"), "最新" if newest else
                            ("当前安装版" if it["version"] == APP_VERSION else "")) if x]
        cv.create_text(14, 38, text=" · ".join(meta), anchor="w", fill=COL["dim"],
                       font=(FN, 7))

        by = h - 40
        notes = it.get("notes") or "（这个版本没填更新说明）"
        tid = cv.create_text(14, 54, text=notes, anchor="nw", width=w - 28,
                             justify="left", fill=COL["sub"], font=(FN, 8))
        for _ in range(14):                      # 说明太长就截断，别压到按钮上
            try:
                bb = cv.bbox(tid)
            except Exception:
                break
            if not bb or bb[3] <= by - 10:
                break
            notes = notes[:max(14, int(len(notes) * 0.86))].rstrip() + "…"
            cv.itemconfigure(tid, text=notes)
        self._popup_msg = tid

        self._popup_btn(cv, 14, by, 128, by + 26,
                        "立即更新" if newest else "打开网页",
                        COL["ok"] if newest else COL["sub"])
        self._popup_btn(cv, 134, by, 206, by + 26, "以后再说", COL["dim"])
        cv.create_text(232, by + 13, text="‹", tags=("prev",),
                       fill=COL["fg"] if self.upd_idx > 0 else COL["dim"],
                       font=(FN, 13, "bold"))
        cv.create_text(258, by + 13, text="%d / %d" % (self.upd_idx + 1, len(self.releases)),
                       fill=COL["dim"], font=(FN, 7))
        cv.create_text(300, by + 13, text="›", tags=("next",),
                       fill=COL["fg"] if self.upd_idx < len(self.releases) - 1 else COL["dim"],
                       font=(FN, 13, "bold"))
        if self.S != 1.0:
            cv.scale("all", 0, 0, self.S, self.S)

    def _on_upd_click(self, e):
        s = self.S or 1.0
        ex, ey = e.x / s, e.y / s          # 画布按 S 整体放大过，判断前先缩回来
        w, h = UPD_W, UPD_H
        by = h - 40
        if by <= ey <= by + 26:
            if 14 <= e.x <= 128:
                if self.upd_idx == 0:
                    self.update = self.releases[0]
                    self.do_update()
                else:
                    webbrowser.open(self.releases[self.upd_idx]["url"])
                return
            if 134 <= e.x <= 206:
                self.cfg["skip_version"] = self.releases[0]["version"]
                save_cfg(self.cfg)
                self._close_upd_popup()
                return
            if 215 <= e.x <= 245:
                if self.upd_idx > 0:
                    self.upd_idx -= 1
                    self._render_upd_popup()
                return
            if e.x >= 285:
                if self.upd_idx < len(self.releases) - 1:
                    self.upd_idx += 1
                    self._render_upd_popup()
                return
        if e.x >= w - 30 and e.y <= 34:
            self._close_upd_popup()


    def _round_rect_cv(self, cv, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
               x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
               x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return cv.create_polygon(pts, smooth=True, splinesteps=20, **kw)

    def _popup_btn(self, cv, x1, y1, x2, y2, text, col):
        self._round_rect_cv(cv, x1, y1, x2, y2, 7, fill=COL["line"], outline="")
        return cv.create_text((x1 + x2) / 2.0, (y1 + y2) / 2.0, text=text,
                              fill=col, font=(FN, 8, "bold"))

    def _close_upd_popup(self):
        if self.upd_win is not None:
            try:
                self.upd_win.destroy()
            except Exception:
                pass
            self.upd_win = None

    def do_update(self):
        info = self.update
        if not info:
            return
        if self._updating:          # 防止重复点，两个线程同时写同一个临时文件会把它写坏
            return
        if not getattr(sys, "frozen", False):
            webbrowser.open(info["url"])
            self._close_upd_popup()
            messagebox.showinfo(APP_NAME, "当前是源码模式，已在浏览器打开下载页。\n"
                                          "（用 exe 运行时可以自动更新）")
            return
        if not info.get("exe"):
            webbrowser.open(info["url"])
            self._close_upd_popup()
            return
        self._updating = True
        if getattr(self, "_upd_cv", None) is not None:
            try:
                self._upd_cv.itemconfigure(self._popup_msg,
                                           text="正在下载 v%s，请稍候…" % info["version"])
            except Exception:
                pass
        threading.Thread(target=self._do_self_update, args=(info,), daemon=True).start()

    def _do_self_update(self, info):
        try:
            tmp = os.path.join(tempfile.gettempdir(),
                               "hs300_update_%s_%d.exe" % (info["version"], os.getpid()))
            download_release_asset(info, tmp)
            # 装到旧 exe 旁边、用带版本号的新文件名：
            # 绝不覆盖正在运行的自己（覆盖会让新进程读到半截文件，报 python313.dll 解压失败）
            src_exe = os.path.abspath(sys.executable)
            target_dir = os.path.dirname(src_exe)
            new_exe = os.path.join(target_dir,
                                   "eyupai-hs300-basis-v%s.exe" % info["version"])
            if os.path.abspath(new_exe).lower() == src_exe.lower():
                new_exe = os.path.join(target_dir,
                                       "eyupai-hs300-basis-v%s-new.exe" % info["version"])
            shutil.copyfile(tmp, new_exe)
            if os.path.getsize(new_exe) != os.path.getsize(tmp):
                raise RuntimeError("复制不完整")
            subprocess.Popen([new_exe], cwd=target_dir, close_fds=True)
            self.q.put(("quit", None))
        except Exception as e:
            self.q.put(("upderr", "更新失败：%s" % e))

    def _on_upd_err(self, msg):
        self._upd_busy = False
        self._updating = False      # 正在下载/安装更新
        if getattr(self, "_upd_cv", None) is not None:
            try:
                self._upd_cv.itemconfigure(self._popup_msg, text=str(msg)[:34])
                return
            except Exception:
                pass
        try:
            messagebox.showwarning(APP_NAME, str(msg))
        except Exception:
            pass

    def set_topmost(self, on):
        self.topmost = bool(on)
        self.cfg["topmost"] = self.topmost
        save_cfg(self.cfg)
        try:
            self.root.attributes("-topmost", self.topmost)
        except Exception:
            pass
        if self.topmost:
            apply_tool_window(self.hwnd)

    def set_autostart(self, on):
        ok, msg = set_autostart(on)
        if not ok:
            print(msg)
            try:
                messagebox.showwarning(APP_NAME, msg)
            except Exception:
                pass

    def reset_pos(self):
        sw = self.root.winfo_screenwidth()
        self.pos = [sw - int(COMPACT_W * self.S) - 60, 90]
        self._apply_geometry()
        self.cfg["x"], self.cfg["y"] = self.pos
        save_cfg(self.cfg)

    def close(self):
        self.alive = False
        try:
            self.cfg["x"], self.cfg["y"] = self.pos[0], self.pos[1]
            self.cfg["expanded"] = self.expanded
            self.cfg["interval"] = self.interval
            self.cfg["topmost"] = self.topmost
            save_cfg(self.cfg)
        except Exception:
            pass
        try:
            self.feed.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


# ==================================================================
#                        在线更新
# ==================================================================
def _ver_tuple(v):
    """'v1.2.3' -> (1, 2, 3)"""
    out = []
    for p in str(v).strip().lstrip("vV").split("."):
        n = ""
        for ch in p:
            if ch.isdigit():
                n += ch
            else:
                break
        out.append(int(n) if n else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def _clean_notes(text, limit=400):
    """把 Release 的 markdown 正文压成几行纯文本。"""
    if not text:
        return ""
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    t = t.replace("**", "").replace("__", "").replace("`", "")
    out = []
    for ln in t.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("<!--") or ln.startswith("```"):
            continue
        ln = ln.lstrip("#").strip()
        ln = re.sub(r"^[-*+]\s+", "· ", ln)
        if ln:
            out.append(ln)
        if len("\n".join(out)) > limit:
            break
    s = "\n".join(out).strip()
    return (s[:limit] + "…") if len(s) > limit else s


def fetch_releases(timeout=8):
    """最近 10 个 Release（含更新说明）-> [{version,notes,url,exe,date}]，新到旧。

    结果缓存 1 小时，避免每次启动都打 GitHub API（未登录限 60 次/小时）。
    """
    cache_path = os.path.join(CFG_DIR, "releases.json")
    try:
        if os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if time.time() - float(cache.get("ts") or 0) < 3600:
                if cache.get("list"):
                    return cache["list"]
    except (OSError, ValueError, TypeError):
        pass
    try:
        r = requests.get(RELEASES_API, timeout=timeout,
                         headers={"User-Agent": "hs300-basis-widget/%s" % APP_VERSION,
                                  "Accept": "application/vnd.github+json"})
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    out = []
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict) or it.get("draft"):
            continue
        best = None
        for a in (it.get("assets") or []):
            if not str(a.get("name", "")).lower().endswith(".exe"):
                continue
            if (a.get("size") or 0) > 3 * 1024 * 1024:
                best = a
                break
        out.append({
            "version": str(it.get("tag_name", "")).strip().lstrip("vV"),
            "name": it.get("name") or "",
            "notes": _clean_notes(it.get("body") or ""),
            "url": it.get("html_url") or REPO_URL + "/releases",
            "exe": (best or {}).get("browser_download_url") or "",
            "api_exe": (best or {}).get("url") or "",
            "size": (best or {}).get("size") or 0,
            "digest": (best or {}).get("digest") or "",
            "date": str(it.get("published_at") or "")[:10],
        })
    if not out:
        return []
    out.sort(key=lambda x: _ver_tuple(x["version"]), reverse=True)
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "list": out}, f)
    except OSError:
        pass
    return out


def fetch_latest_release(timeout=8):
    """兼容旧调用：只要最新一个。"""
    rels = fetch_releases(timeout)
    return rels[0] if rels else None


def _verify_download(path, info):
    """核对下载结果：大小必须一致；Release 提供了 sha256 摘要就再比一次。"""
    size = os.path.getsize(path)
    want = int(info.get("size") or 0)
    if want and size != want:
        raise RuntimeError("文件大小不对（%d / %d 字节）" % (size, want))
    with open(path, "rb") as f:
        if f.read(2) != b"MZ":
            raise RuntimeError("下载到的不是 exe")
    digest = str(info.get("digest") or "")
    if digest.startswith("sha256:"):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        if h.hexdigest().lower() != digest[7:].strip().lower():
            raise RuntimeError("文件校验失败（sha256 对不上，可能下载被中断或被代理改过）")
    return True


def download_release_asset(info, dst, attempts=3):
    """下载 Release 附件到 dst，带校验和重试。返回 True/抛异常。"""
    urls = [u for u in (info.get("exe"), info.get("api_exe")) if u]
    last = None
    for url in urls:
        api = "api.github.com" in url
        for _ in range(attempts):
            try:
                headers = {"User-Agent": "hs300-basis-widget/%s" % APP_VERSION,
                           "Accept-Encoding": "identity"}   # 别让代理压缩二进制
                if api:
                    headers["Accept"] = "application/octet-stream"
                with requests.get(url, stream=True, timeout=180, headers=headers) as r:
                    r.raise_for_status()
                    with open(dst, "wb") as f:
                        for chunk in r.iter_content(65536):
                            if chunk:
                                f.write(chunk)
                _verify_download(dst, info)
                return True
            except Exception as e:
                last = e
                try:
                    if os.path.exists(dst):
                        os.remove(dst)
                except OSError:
                    pass
                time.sleep(1.5)
    raise RuntimeError(str(last) if last else "下载失败")


# ==================================================================
def _relaunch_hidden():
    """双击 .py 时用 pythonw 重启自己，避免留下黑色控制台窗口。"""
    if os.name != "nt" or "--console" in sys.argv:
        return
    exe = sys.executable or ""
    if not exe.lower().endswith("python.exe"):
        return
    pyw = exe[:-len("python.exe")] + "pythonw.exe"
    if not os.path.exists(pyw):
        return
    try:
        subprocess.Popen([pyw, os.path.abspath(__file__)] + [a for a in sys.argv[1:] if a != "--console"],
                         cwd=os.path.dirname(os.path.abspath(__file__)), close_fds=True)
        sys.exit(0)
    except Exception:
        pass


def _single_instance():
    try:
        ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\HS300BasisWidget_mutex")
        return ctypes.windll.kernel32.GetLastError() != 183   # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _res_path(name):
    """兼容 PyInstaller：打包后资源在 sys._MEIPASS 里。"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def _apply_icon(root):
    for name in ("鳄鱼派.ico", "icon.ico"):
        p = _res_path(name)
        if os.path.exists(p):
            try:
                root.iconbitmap(default=p)
                return
            except Exception:
                pass


def main():
    _relaunch_hidden()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    if not _single_instance():
        return

    root = tk.Tk()
    root.title(APP_TITLE)
    _apply_icon(root)
    BasisWidget(root)
    root.mainloop()


if __name__ == "__main__":
    main()
