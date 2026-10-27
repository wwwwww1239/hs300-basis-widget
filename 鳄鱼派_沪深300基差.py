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
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

import requests

# ================== 可调参数 ==================
REFRESH_SEC = 3       # 默认刷新间隔（秒），最小 1
EXTRA_QUARTERS = 3    # 探测后续季月合约数量
INTERVAL_CHOICES = (1, 2, 3, 5, 10, 30)
# =============================================

APP_NAME = "鳄鱼派 沪深300基差"
APP_VERSION = "1.0.0"
APP_TITLE = "%s v%s" % (APP_NAME, APP_VERSION)
HEAD_TITLE = "鳄鱼派 · 沪深300基差"   # 窗体内左上角标题
CFG_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "HS300BasisWidget")
CFG_PATH = os.path.join(CFG_DIR, "settings.json")

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
        if price <= 0 or (oi <= 0 and volume <= 0):
            return None
        pre_settle = float(f[1])
        pre_close = float(f[14]) if len(f) > 14 and f[14] else 0.0
        if pre_close <= 0:
            pre_close = pre_settle
        oi_y = float(f[15]) if len(f) > 15 and f[15] else 0.0
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
    """抓一次行情，算出基差。返回 dict；取不到数据时抛异常。"""
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

    # 只保留「最新行情日」的合约（自动剔除已到期/停牌合约）
    latest_day = max(c["date"] for c in cands)
    live = [c for c in cands if c["date"] == latest_day]

    tot_oi = sum(c["oi"] for c in live)
    if tot_oi <= 0:
        raise RuntimeError("合约持仓为 0")

    wp = sum(c["price"] * c["oi"] for c in live) / tot_oi
    # 加权基准价：文华用「昨日持仓量 × 昨日收盘」加权（换手导致的权重变化不影响当日涨跌）
    tot_oi_y = sum(c["oi_y"] for c in live)
    if tot_oi_y > 0:
        wp_pre = sum(c["pre_close"] * c["oi_y"] for c in live) / tot_oi_y
    else:
        wp_pre = sum(c["pre_close"] * c["oi"] for c in live) / tot_oi

    rows = []
    for c in live:
        rows.append({
            "code": c["code"],
            "price": c["price"],
            "chg": c["price"] - c["pre_close"],
            "oi": c["oi"],
            "vol": c["volume"],
            "w": c["oi"] / tot_oi,
        })
    rows.sort(key=lambda r: -r["oi"])

    basis = spot["price"] - wp
    basis_pct = basis / spot["price"] * 100.0

    try:
        data_dt = datetime.datetime.strptime("%s %s" % (spot["date"], spot["time"]), "%Y-%m-%d %H:%M:%S")
    except Exception:
        data_dt = datetime.datetime.now()

    now = datetime.datetime.now()
    live_flag = (now - data_dt).total_seconds() <= 90 and _in_trading_hours(now)

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
        "live": live_flag,
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
        self.autostart_var = None

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

    # ---------------- 布局 ----------------
    def _size(self):
        n = max(1, len(self.snap["rows"]) if self.snap else 3)
        if self.expanded:
            return EXPANDED_W, 286 + 20 * n
        return COMPACT_W, COMPACT_H

    def _apply_geometry(self):
        w, h = self._size()
        w = int(round(w * self.S))
        h = int(round(h * self.S))
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.pos[0] = max(-10, min(self.pos[0], sw - w + 10))
        self.pos[1] = max(-10, min(self.pos[1], sh - h + 10))
        self.root.geometry("%dx%d+%d+%d" % (w, h, self.pos[0], self.pos[1]))
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
        dot_col = {"live": COL["ok"], "stale": COL["warn"],
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
            tcol = COL["dim"] if status == "stale" else COL["sub"]
            tail = s["data_dt"].strftime("%H:%M:%S") + ("" if status == "live" else "·收盘")
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
            cv.create_text(W - 14, 110, text=tail, fill=tcol, anchor="e",
                           font=self._font(7, family=FNUM))

        if self.expanded:
            self._render_detail(W)

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
                       text="持仓量加权(文华口径)%s · %ds 刷新" % (amt, self.interval),
                       fill=COL["dim"], anchor="center", font=self._font(7))

    # ---------------- 状态 / 数据轮询 ----------------
    def _status(self):
        if self.err and (self.snap is None or self.err_count >= 3):
            return "error"
        if self.snap is None:
            return "init"
        return "live" if self.snap.get("live") else "stale"

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
        """返回命中的按钮名：'close' / 'toggle' / None"""
        s = self.S
        W = self.W * s
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
        self.drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _on_drag(self, e):
        if self.drag is None:
            return
        x = e.x_root - self.drag[0]
        y = e.y_root - self.drag[1]
        self.pos = [x, y]
        self.root.geometry("+%d+%d" % (x, y))

    def _on_release(self, e):
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
        self._apply_geometry()
        self.render()

    def _build_menu(self):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label=APP_TITLE, state="disabled")
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
