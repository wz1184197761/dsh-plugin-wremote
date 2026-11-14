"""单窗口远程：WGC 采集 + WebSocket 推流 + SendInput 回注。

用法：
  python server.py <hwnd> [--port 8099] [--fps 20] [--quality 70] [--mode mon|auto|wgc|pw]
hwnd 支持 0x 前缀。

约束：采集必须与目标窗口在同一登录会话、且会话处于活动状态。
默认的 mon 通道抓整屏再裁客户区：Qt 自绘窗口（微信）的窗口级通道实测只会重复
投递建源那一刻的冻结画面，只有合成到屏幕上的像素会真实更新。代价是窗口被遮挡
时投出的是遮挡后的画面，所以 pump 会持续 keep_on_screen 并在点击时置前。
"""
import argparse
import asyncio
import ctypes
import json
import logging
import os
import sys
import threading
import time
from ctypes import wintypes

import cv2
import numpy as np
from windows_capture import WindowsCapture

# 隧道健康检查每分钟 GET 一次 /，websockets 会把它记成「握手失败」并打 traceback，
# 看着像故障。降噪到 CRITICAL：真错误仍会由我们自己 print 出来。
logging.getLogger("websockets").setLevel(logging.CRITICAL)

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
PW_RENDERFULLCONTENT = 0x00000002
# 默认 restype 是 c_int，hwnd 高位为 1 时会变成负数，和 find_window 返回的 int 比不相等
try:
    user32.GetForegroundWindow.restype = wintypes.HWND
except Exception:
    pass
DWMWA_EXTENDED_FRAME_BOUNDS = 9

# 与 WGC 帧（物理像素）同一坐标系：不声明 DPI 感知时 GetClientRect/SetCursorPos
# 拿到的是被系统虚拟化的逻辑坐标，点击会整体偏位（10-29 实测 958x1079 vs 帧 1080x960）。
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
SM_CXSCREEN = 0
SM_CYSCREEN = 1

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
# 微信会响应 WM_WINDOWPOSCHANGING 把高度截回屏幕高；跳过该通知才能让客户区超出屏幕
SWP_NOSENDCHANGING = 0x0400
SW_RESTORE = 9
SW_SHOW = 5


class RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_long), ("Top", ctypes.c_long),
                ("Right", ctypes.c_long), ("Bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def client_rect(hwnd):
    r = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(r)):
        raise OSError("GetClientRect failed")
    return r.Right - r.Left, r.Bottom - r.Top


def client_origin(hwnd):
    p = POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(p)):
        raise OSError("ClientToScreen failed")
    return p.x, p.y


user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL

GA_ROOT = 2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
HWND_TOP = 0
# 重排失败（落点压根不在目标窗口里，比如坐标基准错位）时，每次点击都做两次
# SetWindowPos + 两次复验纯属白付延迟：同一个 hwnd 连续失败 1s 内不重试。
BAND_RETRY = 1.0
_band = {"hwnd": 0, "at": -1e9, "rc": 3}


def _root_at(sx, sy):
    """屏幕上 (sx,sy) 那个点最终归哪个顶层窗口。"""
    p = user32.WindowFromPoint(POINT(int(sx), int(sy)))
    if not p:
        return 0
    return user32.GetAncestor(p, GA_ROOT) or p


async def ensure_reachable(hwnd, sx, sy):
    """点击按屏幕坐标注入，被别的窗口盖住就会打到别人身上。

    目标窗口和它的兄弟窗多半全是 WS_EX_TOPMOST，同在一条 topmost 带里；带内相对
    z 序决定 WindowFromPoint 落到谁身上。对本来就 topmost 的窗口做
    SetWindowPos(HWND_TOPMOST) 在多种状态下是 no-op，被兄弟窗压住后提不回来，
    表现就是「点了没反应」。所以这里不再只判断 root != hwnd，而是真的要把它排到
    带内最前：先 HWND_NOTOPMOST 摘出带、再 HWND_TOPMOST 插回，复验；仍被挡就退
    HWND_TOP 带内排首再复验（实测两种都能修，NOTOPMOST->TOPMOST 最稳）。

    返回 0=落点已是目标窗口且前台也是它（什么都没动）
         1=已重排到带内最前，或只重建了前台
         2=重排后复验仍被挡，回退 HWND_TOP 后复验通过
         3=失败（落点仍是别人的窗口）
    调用方拿到非 0 就要等一拍再注入：窗口刚被激活/提顶时，Qt 会把第一次按下当成
    "激活点击"吞掉（实测第一次点按钮没反应、拖动完全不启动）。
    """
    now = time.monotonic()
    rc = 0
    cur = _root_at(sx, sy)
    if not cur:
        # 落点不在任何窗口上（坐标基准错位、窗口刚移走）：没有可判定的落点，直接失败。
        return 3
    if cur == hwnd:
        _band["hwnd"], _band["at"], _band["rc"] = hwnd, now, 0
    elif (_band["hwnd"] == hwnd and _band["rc"] == 3
          and now - _band["at"] < BAND_RETRY):
        # 刚试过且没修好：落点不在目标窗口里，重排多少次都一样。
        return 3
    else:
        ok = bool(user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                                      SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE))
        # 摘出带和插回带之间留一拍，让窗口管理器真的处理完前一条（同步注入点击时
        # 连发两条，实测偶尔只生效一条）。用 await 而不是 sleep：pump 在同一条循环上。
        await asyncio.sleep(0.02)
        ok = bool(user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                      SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)) and ok
        rc = 1 if ok else 3
        if _root_at(sx, sy) != hwnd:
            ok2 = bool(user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                                           SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE))
            rc = 2 if (ok2 and _root_at(sx, sy) == hwnd) else 3
        _band["hwnd"], _band["at"], _band["rc"] = hwnd, time.monotonic(), rc
        print(f"[raise] hwnd={hwnd:#x} rc={rc} at=({sx:.0f},{sy:.0f})", flush=True)
    if user32.GetForegroundWindow() != hwnd:
        user32.SetForegroundWindow(hwnd)
        if rc == 0:
            rc = 1
    return rc


def window_box(hwnd):
    r = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        raise OSError("GetWindowRect failed")
    return r.Right - r.Left, r.Bottom - r.Top, r.Left, r.Top


def frame_bounds(hwnd):
    """DWM 扩展帧边界（去掉阴影与不可见调整边框）。WGC 抓到的正是这块区域。"""
    r = RECT()
    try:
        if dwmapi.DwmGetWindowAttribute(
                hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) == 0:
            w, h = r.Right - r.Left, r.Bottom - r.Top
            if w > 0 and h > 0:
                return r.Left, r.Top, w, h
    except Exception:
        pass
    w, h, x, y = window_box(hwnd)
    return x, y, w, h


def crop_to_client(hwnd, buf):
    """WGC/PrintWindow 抓到的是整窗，裁掉客户区之外的非客户区（左右/底部边框）。"""
    cw, ch = client_rect(hwnd)
    cx, cy = client_origin(hwnd)
    fx, fy, fw, fh = frame_bounds(hwnd)
    ww, wh, wx, wy = window_box(hwnd)
    h, w = buf.shape[:2]
    if fw <= 0 or fh <= 0 or w <= 0 or h <= 0:
        return buf
    # 参照系必须跟着缓冲区来源走：PrintWindow 抓的是 GetWindowRect 整窗，WGC 抓的是
    # DWM 扩展帧。固定用扩展帧时，遇到「扩展帧 ≈ 客户区」的无边框窗口比例会算成 1:1，
    # 整帧原样返回（978x1098），客户端再把 958 拉到 960，全局压缩约 1.7%——画面发糊。
    if abs(w - ww) + abs(h - wh) <= abs(w - fw) + abs(h - fh):
        rx, ry, rw, rh = wx, wy, ww, wh
    else:
        rx, ry, rw, rh = fx, fy, fw, fh
    if rw <= 0 or rh <= 0:
        return buf
    sx, sy = w / rw, h / rh
    x0 = max(0, int(round((cx - rx) * sx)))
    y0 = max(0, int(round((cy - ry) * sy)))
    x1 = min(w, x0 + int(round(cw * sx)))
    y1 = min(h, y0 + int(round(ch * sy)))
    if x1 - x0 < 32 or y1 - y0 < 32:
        return buf  # 读数异常时宁可发整窗，也不要发一张废帧
    return buf[y0:y1, x0:x1]


def ensure_normal(hwnd):
    """把最小化/最大化的窗口拉回普通态，返回是否动过。

    这两种状态都会让帧跟客户区对不上：最大化时 SetWindowPos 被忽略、WGC 仍推
    1920x1079 整屏帧（页面只能缩放）；最小化后只剩 144x20 的图标窗（WGC 无帧、
    PrintWindow 抓到的是图标缩略图），点击也就没有落点。
    """
    changed = False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        changed = True
    if user32.IsZoomed(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        changed = True
    # 点关闭/最小化到托盘后窗口是 SW_HIDE 状态，既不是 iconic 也不是 zoomed，
    # 但 WGC 采不到、输入也打不进（用户看到的是"画面不动、打不了字"）。
    if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_SHOW)
        changed = True
    return changed


def keep_on_screen(hwnd):
    """窗口只改大小不改位置时会被推出屏幕（实测底边超出 359px），
    屏幕外的部分按坐标注入点不到，表现为「点按钮没反应」。"""
    w, h, wx, wy = window_box(hwnd)
    sw = user32.GetSystemMetrics(SM_CXSCREEN) or 1920
    sh = user32.GetSystemMetrics(SM_CYSCREEN) or 1080
    nx = max(0, min(wx, sw - min(w, sw)))
    ny = max(0, min(wy, sh - min(h, sh)))
    if (nx, ny) == (wx, wy):
        return False
    user32.SetWindowPos(hwnd, 0, nx, ny, 0, 0,
                        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
    print(f"[win] 窗口超出屏幕，移回 ({wx},{wy})->({nx},{ny})", flush=True)
    return True


def resize_client(hwnd, want_w, want_h, tol=1):
    """把客户区调整到 want_w x want_h，左上角不动。

    客户端按自己屏幕的可见区域下发尺寸，服务端据此改变源窗口大小，
    这样推过去的帧与客户端显示区同尺寸，页面 1:1 绘制、零缩放。
    """
    if ensure_normal(hwnd):
        print("[win] resize 前还原窗口状态（最小化/最大化）", flush=True)
    cw, ch = client_rect(hwnd)
    if abs(cw - want_w) <= tol and abs(ch - want_h) <= tol:
        keep_on_screen(hwnd)
        return False
    # 微信掉线/重登时会缩成「重新登录」小窗（实测 296x388）。这时按页面尺寸撑大
    # 会把登录界面拉变形，而且窗口会被挤到屏幕外；等它恢复成主窗口再对齐。
    if cw < 420 or ch < 320:
        print(f"[geom] 源窗口过小（{cw}x{ch}），跳过自动对齐", flush=True)
        return False
    ww, wh, wx, wy = window_box(hwnd)
    nw = ww + (want_w - cw)
    nh = wh + (want_h - ch)
    # 一次到位只能到屏幕高：微信/Chromium 按 max track size 把超出部分截回去
    # （want 1082 实测只得到 1080）。先设一个明显更大的高度再落回目标才能拿到 1082。
    if not user32.SetWindowPos(hwnd, 0, wx, wy, nw, nh + 400,
                               SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOSENDCHANGING):
        return False
    if not user32.SetWindowPos(hwnd, 0, wx, wy, nw, nh,
                               SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOSENDCHANGING):
        return False
    print(f"[geom] resize client {cw}x{ch} -> {client_rect(hwnd)[0]}x{client_rect(hwnd)[1]}"
          f" (want {want_w}x{want_h}, window {nw}x{nh})", flush=True)
    keep_on_screen(hwnd)
    return True


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


def print_window_bgr(hwnd):
    """GDI PrintWindow 抓整窗（含标题栏）为 BGR 数组。"""
    w, h, _, _ = window_box(hwnd)
    if w <= 0 or h <= 0:
        raise OSError("bad window rect %dx%d" % (w, h))
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT)
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0):
            raise OSError("GetDIBits failed")
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)
    return np.frombuffer(buf.raw, dtype=np.uint8).reshape(h, w, 4)[:, :, :3]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def find_window(title, min_w=200, min_h=120):
    """按标题精确匹配顶层窗口，取面积最大的候选（排除托盘/小窗）。

    下限不能太高：微信掉线时会缩成「重新登录」小窗（实测 296x388），
    卡 400x300 会让服务直接报找不到窗口，用户看到的是投屏永久冻结。
    """
    hits = []

    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value != title:
            return True
        r = RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(r)):
            w, h = r.Right - r.Left, r.Bottom - r.Top
            if w >= min_w and h >= min_h:
                # 微信会残留不可见的同名窗口（RDP 接管会话后必现），抓到它只能是黑帧。
                vis = 1 if user32.IsWindowVisible(hwnd) else 0
                hits.append((vis, w * h, hwnd, w, h))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    if not hits:
        return None
    hits.sort(reverse=True)
    return hits[0][2]


FOLLOW_SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                       "Windows.UI.Core.CoreWindow"}
FOLLOW_WEIXIN = {"weixin.exe", "wechat.exe", "wechatappex.exe"}


def win_class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def win_process_name(hwnd):
    """窗口所属进程的 exe 名（小写）；取不到返回空串。"""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(h)


def follow_target(state):
    """跟随前台窗口：返回该接管的新 hwnd，0 表示不切。

    微信点开图片/视频/小程序是独立顶层窗口（主窗口仍在），只盯原 hwnd 永远看不到；
    默认只跟微信系进程，切到浏览器/IDE 时画面不会乱跳（--follow all 才全跟）。
    """
    mode = state.get("follow")
    if not mode or mode == "off":
        return 0
    fg = user32.GetForegroundWindow() or 0
    if not fg or fg == state["hwnd"] or not user32.IsWindowVisible(fg):
        return 0
    if win_class_name(fg) in FOLLOW_SKIP_CLASSES:
        return 0
    r = RECT()
    if not user32.GetWindowRect(fg, ctypes.byref(r)):
        return 0
    if r.Right - r.Left < 200 or r.Bottom - r.Top < 120:
        return 0
    if mode == "weixin" and win_process_name(fg) not in FOLLOW_WEIXIN:
        return 0
    return fg


class FrameSource:
    """后台线程持续采最新一帧，主线程只取快照。"""

    space = "client"
    name = "wgc"

    def __init__(self, hwnd, fps, quality):
        self.hwnd = hwnd
        self.interval_ms = int(1000 / max(1, fps))
        self.quality = quality
        self._jpeg = None
        self._seq = 0
        self._lock = threading.Lock()
        self._stop = False
        self._cap = None
        self._ctl = None
        self.error = None
        # 内容去重：微信主窗口上实测 WGC 会反复投递同一张图（seq 照涨、画面不变），
        # 只按 seq 判变化会让页面收到一堆重复帧，而画面真变了却永远等不到新帧。
        self._same_since = None
        self._same_count = 0
        self._bgr_size = None

    def start(self):
        cap = WindowsCapture(
            window_hwnd=self.hwnd,
            # 不把源端光标画进帧：鼠标直接用客户端本地系统光标，跟手零延迟，
            # 也不会出现"画面里一个、页面外一个"的两个鼠标。
            cursor_capture=False,
            draw_border=False,
            minimum_update_interval=self.interval_ms,
        )

        @cap.event
        def on_frame_arrived(frame, control):
            try:
                buf = crop_to_client(self.hwnd, frame.convert_to_bgr().frame_buffer)
                ok, enc = cv2.imencode(
                    ".jpg", buf, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
                )
                if ok:
                    b = enc.tobytes()
                    self.error = None  # 成功即清错误，否则 swap_source 的 keep 分支永久失效
                    self._bgr_size = (buf.shape[1], buf.shape[0])
                    with self._lock:
                        first = False
                        if b != self._jpeg:
                            self._jpeg = b
                            self._seq += 1
                            self._same_since = None
                            self._same_count = 0
                            first = self._seq == 1
                        else:
                            # 同一张画面又送来一次：不递增 seq，只累计重复时长，
                            # 供上层判断「这条采集通道已经不再反映真实画面」。
                            self._same_count += 1
                            if self._same_since is None:
                                self._same_since = time.time()
                    if first:
                        print(f"[cap] first frame {buf.shape} jpeg={len(self._jpeg)}", flush=True)
                else:
                    self.error = "imencode failed"
            except Exception as e:  # 单帧失败不应终止采集
                self.error = repr(e)

        @cap.event
        def on_closed():
            self._stop = True

        self._cap = cap
        # start_free_threaded 返回 CaptureControl，停采集必须调它的 stop()；
        # WindowsCapture 本身没有 stop（实测 AttributeError）
        self._ctl = cap.start_free_threaded()

    def stop(self):
        self._stop = True
        ctl, self._ctl = self._ctl, None
        self._cap = None
        if ctl is None:
            return
        # 不 stop 会让 capture 会话一直持有 frame pool 与采集线程；
        # swap_source 每次重建都新建一个，旧的全泄漏（实测进程涨到 7.9GB）
        try:
            ctl.stop()
        except Exception as e:
            print(f"[cap] 停止旧采集源失败: {e!r}", flush=True)

    def snapshot(self):
        with self._lock:
            return self._jpeg, self._seq

    def frame_size(self):
        """最近一帧的 (宽, 高)；还没出过帧返回 None。"""
        return self._bgr_size

    def stale_for(self):
        """连续投递同一张画面已经多少秒；内容一直在变则返回 0。"""
        with self._lock:
            if self._same_since is None:
                return 0.0
            return time.time() - self._same_since


class PwFrameSource:
    """PrintWindow 轮询采集：登录会话断开时 WGC 不产帧，用 GDI 抓整窗位图兜底（同样裁到客户区）。"""

    space = "client"
    name = "pw"

    def __init__(self, hwnd, fps, quality):
        self.hwnd = hwnd
        self.interval = 1.0 / max(1, fps)
        self.quality = quality
        self._jpeg = None
        self._seq = 0
        self._lock = threading.Lock()
        self._stop = False
        self.error = None
        self._bgr_size = None

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop = True

    def _loop(self):
        while not self._stop:
            t0 = time.time()
            try:
                img = crop_to_client(self.hwnd, print_window_bgr(self.hwnd))
                ok, enc = cv2.imencode(
                    ".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
                )
                if ok:
                    b = enc.tobytes()
                    self.error = None  # 成功即清错误，否则 swap_source 的 keep 分支永久失效
                    self._bgr_size = (img.shape[1], img.shape[0])
                    with self._lock:
                        if b != self._jpeg:
                            self._jpeg = b
                            self._seq += 1
                            if self._seq == 1:
                                print(f"[cap:pw] first frame {img.shape} jpeg={len(b)}", flush=True)
                else:
                    self.error = "imencode failed"
            except Exception as e:  # 单帧失败不应终止采集
                self.error = repr(e)
            dt = time.time() - t0
            if dt < self.interval:
                time.sleep(self.interval - dt)

    def snapshot(self):
        with self._lock:
            return self._jpeg, self._seq

    def frame_size(self):
        """最近一帧的 (宽, 高)；还没出过帧返回 None。"""
        return self._bgr_size

    def stale_for(self):
        # 拉模式：每轮都重新抓真实像素，不存在「重复投递旧画面」这种失效形态
        return 0.0


class MonFrameSource:
    """显示器整体捕获 + 裁客户区。

    微信（Qt51514）主窗口上的实测：窗口级通道（WGC window_hwnd / PrintWindow）
    拿到的都是冻结画面——建源那一帧之后永远重复投递同一张图（WGC 的 seq 还在涨，
    PrintWindow 的像素和恒定），点击、滚动、新消息一概不反映。只有合成到屏幕上的
    像素是真实的（整屏捕获点击 diff_px=38951、28fps、distinct 正常）。
    所以主力通道用「WGC 抓整屏 + 按客户区矩形裁片」。

    代价：窗口被别的窗口遮住时，投出去的就是遮挡后的画面。pump 里已有
    keep_on_screen + ensure_normal，点击时 ensure_reachable 还会把源窗口置前。
    """

    space = "client"
    name = "mon"

    def __init__(self, hwnd, fps, quality, monitor_index=1):
        self.hwnd = hwnd
        self.interval_ms = int(1000 / max(1, fps))
        self.quality = quality
        self.monitor_index = monitor_index
        self._jpeg = None
        self._seq = 0
        self._lock = threading.Lock()
        self._stop = False
        self._cap = None
        self._ctl = None
        self.error = None
        # 上一帧裁剪区原始字节：整屏任何角落变化（任务栏时钟、别的窗口动画）都会
        # 触发回调，先比这一层再决定是否付 cvtColor+imencode 的代价（实测 9ms/帧）。
        self._last_crop = None
        self._bgr_size = None

    def _crop(self, fb):
        # 窗口会被 resize（geom）和 keep_on_screen 移动，每帧现取客户区矩形；
        # 越界夹到帧内，避免负索引与白边。
        ox, oy = client_origin(self.hwnd)
        cw, ch = client_rect(self.hwnd)
        fh, fw = fb.shape[0], fb.shape[1]
        x0 = max(0, min(ox, fw - 1))
        y0 = max(0, min(oy, fh - 1))
        x1 = max(x0 + 1, min(ox + cw, fw))
        y1 = max(y0 + 1, min(oy + ch, fh))
        return fb[y0:y1, x0:x1]

    def start(self):
        cap = WindowsCapture(
            monitor_index=self.monitor_index,
            cursor_capture=False,
            draw_border=False,
            minimum_update_interval=self.interval_ms,
        )

        @cap.event
        def on_frame_arrived(frame, control):
            try:
                crop = self._crop(frame.frame_buffer)
                if crop.size == 0:
                    self.error = "empty crop"
                    return
                # 客户区像素没变就不必编码：整屏捕获对屏幕任何变化都会回调，
                # 但投屏只关心窗口这一块。frame_buffer 每帧复用，必须拷贝留存。
                snap = np.array(crop, dtype=np.uint8, copy=True)
                prev = self._last_crop
                if prev is not None and prev.shape == snap.shape and np.array_equal(prev, snap):
                    return
                self._last_crop = snap
                bgr = cv2.cvtColor(snap, cv2.COLOR_BGRA2BGR)
                ok, enc = cv2.imencode(
                    ".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
                )
                if ok:
                    b = enc.tobytes()
                    self.error = None  # 成功即清错误，否则 swap_source 的 keep 分支永久失效
                    self._bgr_size = (bgr.shape[1], bgr.shape[0])
                    first = False
                    with self._lock:
                        if b != self._jpeg:
                            self._jpeg = b
                            self._seq += 1
                            first = self._seq == 1
                    if first:
                        print(f"[cap:mon] first frame {bgr.shape} jpeg={len(b)}", flush=True)
                else:
                    self.error = "imencode failed"
            except Exception as e:  # 单帧失败不应终止采集
                self.error = repr(e)

        @cap.event
        def on_closed():
            self._stop = True

        self._cap = cap
        self._ctl = cap.start_free_threaded()

    def stop(self):
        self._stop = True
        ctl, self._ctl = self._ctl, None
        self._cap = None
        if ctl is None:
            return
        try:
            ctl.stop()
        except Exception as e:
            print(f"[cap] 停止旧采集源失败: {e!r}", flush=True)

    def snapshot(self):
        with self._lock:
            return self._jpeg, self._seq

    def frame_size(self):
        """最近一帧的 (宽, 高)；还没出过帧返回 None。"""
        return self._bgr_size

    def stale_for(self):
        # 显示器级捕获拿的就是屏幕真像素，不存在「重复投递旧画面」这种失效形态。
        # 存活判据交给 pump 的 src.error 检查，不靠这一层。
        return 0.0


def send_mouse(x, y, action, extra=0):
    """统一走 SendInput：mouse_event 在部分 Qt/高完整性窗口上被静默丢弃（10-29 实测）。
    move 必须发 MOUSEEVENTF_MOVE 事件：SetCursorPos 只挪光标、不产生移动消息，
    窗口拖动收不到 move 就完全不动（10-30 实测）。"""
    table = {
        "down": MOUSEEVENTF_LEFTDOWN,
        "up": MOUSEEVENTF_LEFTUP,
        "rdown": MOUSEEVENTF_RIGHTDOWN,
        "rup": MOUSEEVENTF_RIGHTUP,
        "md": MOUSEEVENTF_MIDDLEDOWN,
        "mu": MOUSEEVENTF_MIDDLEUP,
    }
    if action == "move":
        sw = user32.GetSystemMetrics(SM_CXSCREEN) or 1
        sh = user32.GetSystemMetrics(SM_CYSCREEN) or 1
        ax = int(round(x * 65535 / max(sw - 1, 1)))
        ay = int(round(y * 65535 / max(sh - 1, 1)))
        inp = _INPUT(type=INPUT_MOUSE)
        inp.mi = _MOUSEINPUT(ax, ay, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, None)
        return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
    user32.SetCursorPos(int(x), int(y))
    flag = None
    data = 0
    if action == "wheel":
        flag = MOUSEEVENTF_WHEEL
        data = int(extra)
    elif action in table:
        flag = table[action]
    if flag is None:
        return 0
    inp = _INPUT(type=INPUT_MOUSE)
    inp.mi = _MOUSEINPUT(0, 0, data, flag, 0, None)
    return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                               wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT

_PM_MOUSE = {
    "down": (WM_LBUTTONDOWN, MK_LBUTTON),
    "up": (WM_LBUTTONUP, 0),
    "rdown": (WM_RBUTTONDOWN, MK_RBUTTON),
    "rup": (WM_RBUTTONUP, 0),
    "md": (WM_MBUTTONDOWN, MK_MBUTTON),
    "mu": (WM_MBUTTONUP, 0),
}
_EXT_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
             0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x6F, 0x90, 0xA3, 0xA5}
_MOD_VKS = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}


def _pm_lp(x, y):
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def post_mouse(hwnd, cx, cy, action, extra=0, sx=0, sy=0, down=None):
    """SendInput 被拒（会话无活动输入桌面）时的回退：直接向窗口投递消息。"""
    if action == "wheel":
        wp = (int(extra) & 0xFFFF) << 16
        return int(user32.PostMessageW(hwnd, WM_MOUSEWHEEL, wp, _pm_lp(sx, sy)))
    if action == "move":
        mk = {"down": MK_LBUTTON, "rdown": MK_RBUTTON, "md": MK_MBUTTON}.get(down, 0)
        return int(user32.PostMessageW(hwnd, WM_MOUSEMOVE, mk, _pm_lp(cx, cy)))
    hit = _PM_MOUSE.get(action)
    if not hit:
        return 0
    wm, mk = hit
    if mk:
        user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, _pm_lp(cx, cy))
    return int(user32.PostMessageW(hwnd, wm, mk, _pm_lp(cx, cy)))


def post_key(hwnd, vk, up=False):
    vk = int(vk)
    scan = int(user32.MapVirtualKeyW(vk, 0)) & 0xFF
    lp = 1 | (scan << 16)
    if vk in _EXT_KEYS:
        lp |= 1 << 24
    if up:
        lp |= (1 << 30) | (1 << 31)
    return int(user32.PostMessageW(hwnd, WM_KEYUP if up else WM_KEYDOWN, vk, lp))


def send_key(vk, up=False):
    scan = int(user32.MapVirtualKeyW(int(vk), 0)) & 0xFF
    inp = _INPUT(type=INPUT_KEYBOARD)
    inp.ki = _KEYBDINPUT(int(vk), scan, KEYEVENTF_KEYUP if up else 0, 0, None)
    return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _clip_get():
    if not user32.OpenClipboard(0):
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = _kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            _kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def _clip_set(text):
    data = (text + "\0").encode("utf-16-le")
    if not user32.OpenClipboard(0):
        return False
    try:
        user32.EmptyClipboard()
        h = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = _kernel32.GlobalLock(h)
        if not p:
            return False
        ctypes.memmove(p, data, len(data))
        _kernel32.GlobalUnlock(h)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, h))
    finally:
        user32.CloseClipboard()


def send_unicode(text):
    sent = 0
    for ch in text:
        cp = ord(ch)
        units = [cp] if cp < 0x10000 else [0xD800 + ((cp - 0x10000) >> 10),
                                           0xDC00 + ((cp - 0x10000) & 0x3FF)]
        for unit in units:
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                inp = _INPUT(type=INPUT_KEYBOARD)
                inp.ki = _KEYBDINPUT(0, unit, flags, 0, None)
                sent += user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        time.sleep(0.012)
    return sent


def send_text(text):
    """整段文本用剪贴板 + Ctrl+V 落到远端输入框。

    不能直接注入 Unicode：远端输入法（简体中文）处于组字待输入状态时，WM_CHAR
    会被 IME 截获转成组字，候选框是独立顶层窗口、投屏采集不到，表现为整句中文
    一个字符都不上屏（10-30 两次实测）。粘贴不经过 IME，中文/emoji 都一样可靠。
    剪贴板原内容在粘贴完成后恢复（仅当原内容是文本时）。"""
    if not text:
        return 0
    prev = _clip_get()
    if not _clip_set(text):
        return send_unicode(text)
    # Ctrl+V：按下与抬起之间留间隔，Qt 对零间隔的组合键会漏掉
    send_key(0x11, False); time.sleep(0.02)
    send_key(0x56, False); time.sleep(0.04)
    send_key(0x56, True); time.sleep(0.02)
    send_key(0x11, True)
    if prev is not None:
        threading.Timer(0.6, lambda: _clip_set(prev)).start()
    return 1


def release_stuck_modifiers(held):
    """系统里被别处按住的修饰键会把注入变成快捷键：Win 卡住时单按 A 就是 Win+A，
    弹快速设置、微信一个字符都收不到（10-30 实测）。只清不属于本条连接的键。"""
    freed = []
    for vk in sorted(_MOD_VKS):
        if vk in held:
            continue
        if user32.GetAsyncKeyState(vk) & 0x8000:
            send_key(vk, True)
            freed.append(vk)
    return freed


async def handle(ws, state):
    held = set()
    state["held"] = held
    # 新连接不带任何按键状态：系统里此刻按住的修饰键都属于别处，先放掉再接收输入
    freed = release_stuck_modifiers(held)
    if freed:
        print(f"[in] released stuck modifiers on connect: {freed}", flush=True)
    send_task = asyncio.create_task(pump_frames(ws, state))

    async def recv_loop():
        async for raw in ws:
            hwnd = state["hwnd"]  # 窗口重建后由 pump 更新，这里每次取最新的
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            kind = msg.get("t")
            try:
                if kind == "ping":
                    # 页面靠 pong 判断上行是否还通：隧道/服务端重启后 TCP 半开时，
                    # readyState 仍是 OPEN、帧也照收，只有上行悄悄失效（10-30 实测丢字根因）
                    await ws.send('{"t":"pong"}')
                    continue
                if kind == "geom":
                    gw = int(msg.get("w") or 1)
                    gh = int(msg.get("h") or 1)
                    take = bool(msg.get("take"))
                    # state 是全连接共享的：多条连接各发各的 geom 会让源窗口在两组
                    # 尺寸之间来回切，每次顺带一次「停源 + 新建 + wait_frame 2s」。
                    # 只认首个拿到 owner 的连接，它断开后才释放给后来者。
                    # take=True = 面板主动接管：全屏时面板的尺寸才是用户想看的。
                    # 只让首个连接当 owner 会被残留的浏览器页面永久占住主人位。
                    if take or state.get("geom_owner") is None:
                        state["geom_owner"] = ws
                    print(f"[geom] recv {gw}x{gh} take={take}"
                          f" owner={'self' if state.get('geom_owner') is ws else 'other'}",
                          flush=True)
                    # 只有 owner 的尺寸能进 state["geom"]。这个值还被 --follow 换源
                    # (1218) 与「最小化自动还原」后补校 (1267) 两处无条件消费 ——
                    # 非 owner 写进来就会把别人的尺寸带到那两条路径上。
                    if state.get("geom_owner") is ws and 200 <= gw <= 4000 and 200 <= gh <= 4000:
                        state["geom"] = (gw, gh)
                    if (state.get("geom_owner") is ws
                            and state.get("autosize") and state.get("space") == "client"
                            and not state.get("down")
                            and 200 <= gw <= 4000 and 200 <= gh <= 4000):
                        try:
                            cur = state["hwnd"]
                            # 句柄可能刚被销毁（实测微信临时窗），对失效句柄 resize
                            # 会抛 GetClientRect failed 并触发新一轮重建
                            if (user32.IsWindow(cur) and resize_client(cur, gw, gh)
                                    and not state.get("swapping")):
                                # WGC 的帧尺寸固定在建源那一刻，窗口改大小后必须重建才跟得上
                                state["swapping"] = True
                                asyncio.create_task(rebuild_after_resize(state))
                        except Exception as e:
                            print(f"[geom] resize failed: {e!r}", flush=True)
                elif kind == "m":
                    a = msg.get("a")
                    cw, ch = client_rect(hwnd)
                    ox, oy = client_origin(hwnd)
                    # 客户端发来的是帧像素（页面已按帧尺寸归一化），帧=客户区裁 TRIM=1，
                    # 直接 1:1 落点。按客户端 geom 缩放只在窗口被自动对齐成页面尺寸时凑巧正确；
                    # 源窗口过小跳过对齐时整体偏向左上（280 宽窗口配 914 页面，偏移只剩 0.31 倍）。
                    # 页面发来的 fx,fy 是「它当前显示那一帧」的像素，换算基准必须是那一帧
                    # 的真实尺寸；用点击瞬间的实时客户区，会在 resize 过渡、帧被下沿裁短、
                    # 冻屏期三者不一致时整体按比例错位（1876 帧配 914 客户区 -> 点不到）。
                    src_now = state.get("src")
                    fs = src_now.frame_size() if src_now is not None else None
                    gw, gh = fs if fs else (cw, ch)
                    fx, fy = msg["x"], msg["y"]
                    sx = ox + (fx / gw) * cw
                    sy = oy + (fy / gh) * ch
                    raised = 0
                    pm = 0
                    if a in ("down", "rdown", "md"):
                        raised = await ensure_reachable(hwnd, sx, sy)
                        if raised and raised != 3:
                            # 激活/提顶和按下挤在同一毫秒时，Qt 会把这次按下当成"激活点击"
                            # 吞掉：表现为第一次点按钮没反应、拖动完全不启动（实测 d=0）。
                            # raise=3 是重排失败（落点根本不在目标窗口里），等也没用。
                            await asyncio.sleep(0.09)
                        # 锁定按下瞬间的光标-窗口关系；拖动中窗口自己在动，再按"当前位置+偏移"
                        # 换算会每帧多挪一步、累积漂移把窗口甩出屏幕（10-30 实测）。
                        _, _, wx, wy = window_box(hwnd)
                        state["drag"] = {"ox": sx, "oy": sy, "fx": fx, "fy": fy,
                                         "win": (wx, wy), "moves": 0}
                    drag = state.get("drag")
                    if drag is not None:
                        # 相对位移会被系统鼠标加速放大（实测 2.3 倍），绝对坐标不受影响：
                        # 目标 = 按下时光标位置 + 页面位移，窗口随之 1:1 平移。
                        sx = drag["ox"] + (fx - drag["fx"])
                        sy = drag["oy"] + (fy - drag["fy"])
                        if a == "move":
                            drag["moves"] = drag.get("moves", 0) + 1
                    n = send_mouse(sx, sy, a, msg.get("d", 0))
                    if not n:
                        # 会话输入桌面不活动（RDP 断开/锁屏）时 SendInput 恒返回 0，
                        # 退化为窗口消息投递，不依赖输入桌面。
                        pm = post_mouse(hwnd, fx / gw * cw, fy / gh * ch,
                                        a, msg.get("d", 0), sx, sy, state.get("down"))
                    if a in ("down", "rdown", "md"):
                        state["down"] = a
                    elif a in ("up", "rup", "mu"):
                        # 「拖不动」要能自证：按下到抬起之间 move 到了几条、窗口实际挪了多少。
                        if drag is not None:
                            _, _, wx, wy = window_box(hwnd)
                            print(f"[drag] {a} moves={drag.get('moves', 0)}"
                                  f" win={drag['win']}->({wx},{wy})"
                                  f" d=({wx - drag['win'][0]},{wy - drag['win'][1]})", flush=True)
                        state["down"] = None
                        state["drag"] = None
                    if a != "move":
                        print(f"[in] {a} ({fx:.0f},{fy:.0f})"
                              f" -> ({sx:.0f},{sy:.0f}) sent={n} pm={pm} space={state.get('space')}"
                              f" area={cw}x{ch} geom={gw}x{gh} raise={raised}", flush=True)
                elif kind == "k":
                    vk = int(msg["vk"])
                    up = bool(msg.get("up"))
                    if vk not in _MOD_VKS:
                        stuck = release_stuck_modifiers(held)
                        if stuck:
                            print(f"[in] key released stuck modifiers {stuck}", flush=True)
                    n = send_key(vk, up)
                    pm = 0 if n else post_key(hwnd, vk, up)
                    if vk in _MOD_VKS:
                        if up:
                            held.discard(vk)
                        else:
                            held.add(vk)
                    print(f"[in] key vk={vk} up={up} sent={n} pm={pm}", flush=True)
                elif kind == "text":
                    s = msg.get("s") or ""
                    stuck = release_stuck_modifiers(held)
                    if stuck:
                        print(f"[in] text released stuck modifiers {stuck}", flush=True)
                    n = send_text(s)
                    print(f"[in] text {s!r} sent={n}", flush=True)
            except Exception as e:
                # 单条消息处理失败不能打断连接：句柄随时可能失效（实测
                # ClientToScreen failed / GetClientRect failed），抛出去会取消帧泵，
                # 页面重连后又只收到首帧，表现为「点了没反应」。
                print(f"[in] msg error {kind!r}: {e!r}", flush=True)

    try:
        await recv_loop()
    except Exception:
        pass
    finally:
        if state.get("geom_owner") is ws:
            state["geom_owner"] = None
        send_task.cancel()
        # 页面断开时代码里可能还按着修饰键，不放掉会卡在系统里影响后续输入
        for vk in sorted(held):
            send_key(vk, True)
        held.clear()
        release_stuck_modifiers(held)


def _stop_src(src):
    """停一个采集源；失败只记日志，不影响新建流程。"""
    if src is None:
        return
    try:
        src.stop()
    except Exception as e:
        print(f"[cap] 停止旧源异常: {e!r}", flush=True)


async def swap_source(state):
    """重建采集源：按 --mode 选通道。锁屏自愈、窗口改尺寸后重配都靠它。

    通道优先级 mon > wgc > pw：微信这类 Qt 自绘窗口的窗口级捕获实测只会重复
    投递同一张旧图，显示器级捕获才是真实画面。

    mon 和 pw 都是「按当前客户区矩形现抓」，窗口改尺寸不需要重建；只要 hwnd
    没变、通道也没报错/关闭，就原样用现在的采集源，否则每次 resize 都重启采集
    线程、丢一段画面（实测一晚上重建 469 次就是这么来的）。
    """
    hwnd, fps, quality = state["hwnd"], state["fps"], state["quality"]
    mode = state.get("mode") or "auto"
    bad = state.get("wgc_bad") == hwnd
    old = state.get("src")
    if old is not None and old.hwnd == hwnd and old.error is None and not old._stop:
        keep = ((mode == "mon" and old.name in ("mon", "pw"))
                or (mode == "auto" and old.name in ("mon", "pw"))
                or (mode == "wgc" and old.name == "wgc")
                or (mode == "auto" and old.name == "wgc" and not bad))
        if keep:
            return True
    # 旧源先不动：新源真出帧了再停。反过来会让新源失败时留下一个已停的采集源
    # 挂在 state["src"] 上，泵永远拿到同一对 (jpeg, seq)，画面永久冻结。
    if mode in ("mon", "auto"):
        mon = MonFrameSource(hwnd, fps, quality)
        mon.start()
        if await wait_frame(mon, 40):
            _stop_src(old)
            state["src"], state["space"] = mon, mon.space
            print("[cap] 重建：显示器捕获就绪", flush=True)
            return True
        mon.stop()
        if mode == "mon":
            print("[cap] 重建失败：显示器捕获无帧", flush=True)
            return False
    if mode in ("wgc", "auto") and not bad:
        wgc = FrameSource(hwnd, fps, quality)
        wgc.start()
        if await wait_frame(wgc, 40):
            _stop_src(old)
            state["src"], state["space"] = wgc, wgc.space
            print("[cap] 重建：WGC 就绪", flush=True)
            return True
        wgc.stop()
    pw = PwFrameSource(hwnd, fps, quality)
    pw.start()
    if await wait_frame(pw, 40):
        _stop_src(old)
        state["src"], state["space"] = pw, pw.space
        print("[cap] 重建：降级 PrintWindow", flush=True)
        return True
    print("[cap] 重建失败：显示器捕获/WGC/PrintWindow 均无帧", flush=True)
    return False


async def rebuild_after_resize(state):
    try:
        await swap_source(state)
    finally:
        state["swapping"] = False


async def restore_hint(ws, state, delay=6):
    """自愈后把页面状态从提示改回「已连接」；锁屏降级期间则不覆盖（那时提示本身是准的）。"""
    await asyncio.sleep(delay)
    if state["src"].name not in ("wgc", "mon"):
        return
    try:
        await ws.send(json.dumps({"t": "state", "src": "wgc", "hint": "已连接"},
                                 ensure_ascii=False))
    except Exception:
        pass


async def pump_frames(ws, state):
    last = -1
    sent = 0
    idle_since = time.time()
    wgc_retry = time.time()
    win_check = time.time()
    announced = None
    fails = 0
    send_fails = 0
    err_since = None     # 采集源首次报错的时刻；非空即说明这路通道已经死了
    probe_t = 0.0        # 上次用 PrintWindow 实测真实画面的时刻
    probe_prev = None    # 上一次实测画面，用来判断"真实画面到底有没有变"
    while True:
        try:
            src = state["src"]
            # 用户在源端（或页面误点顶部按钮）把窗口最小化/最大化时，采集与缩放都会失真，
            # 这里发现即还原并重建采集源，免去「画面变成一张图标」「整屏被缩小」再排查一轮。
            now_win = time.time()
            if now_win - win_check > 0.5:
                win_check = now_win
                # 跟随前台窗口（--follow）：微信的图片/视频/小程序是独立顶层窗口，
                # 主窗口还在，不切目标就一直看不到。按住拖动时不切，免得拖到一半换画面。
                ft = 0 if state.get("down") else follow_target(state)
                if ft and (not user32.IsWindow(ft) or not user32.IsWindowVisible(ft)):
                    ft = 0
                if ft:
                    state["hwnd"] = ft
                    print(f"[win] 跟随前台 hwnd=0x{ft:X} ({win_process_name(ft)})", flush=True)
                    try:
                        await ws.send(json.dumps(
                            {"t": "state", "src": "recover",
                             "hint": "已切换到前台窗口"}, ensure_ascii=False))
                    except Exception:
                        pass
                    state["swapping"] = True
                    try:
                        gw, gh = state["geom"]
                        if state.get("autosize") and state.get("space") == "client":
                            try:
                                resize_client(ft, gw, gh)
                            except Exception as e:
                                print(f"[win] 跟随后 resize 失败: {e!r}", flush=True)
                        await swap_source(state)
                    finally:
                        state["swapping"] = False
                    last = -1
                    idle_since = time.time()
                    continue
                # 微信重启/重登会销毁并重建主窗口（hwnd 变）。旧实现继续对着失效句柄
                # 采集和注入：画面冻在最后一帧、点击/拖动全部"没反应"，只能靠用户报障。
                if not user32.IsWindow(state["hwnd"]):
                    new = find_window(state.get("title") or "微信")
                    if new:
                        state["hwnd"] = new
                        print(f"[win] 源窗口已销毁，重新接管 hwnd=0x{new:X}", flush=True)
                        try:
                            await ws.send(json.dumps(
                                {"t": "state", "src": "recover",
                                 "hint": "源端窗口已重建，已重新接管"}, ensure_ascii=False))
                        except Exception:
                            pass
                        state["swapping"] = True
                        try:
                            await swap_source(state)
                        finally:
                            state["swapping"] = False
                        last = -1
                        idle_since = time.time()
                        continue
                if not state.get("down"):
                    keep_on_screen(state["hwnd"])
                if ensure_normal(state["hwnd"]) and not state.get("swapping"):
                    print("[win] 源端窗口被最小化/最大化，已还原并重建采集", flush=True)
                    try:
                        await ws.send(json.dumps(
                            {"t": "state", "src": src.name,
                             "hint": "源端窗口被最小化/最大化，已自动还原"}, ensure_ascii=False))
                    except Exception:
                        pass
                    asyncio.create_task(restore_hint(ws, state))
                    announced = src.name
                    state["swapping"] = True
                    try:
                        # 还原后窗口尺寸是它自己的记忆值（实测差 2px），这里按客户端
                        # 下发的 geom 再校一次，保证重建出来的帧仍是页面需要的精确尺寸
                        gw, gh = state["geom"]
                        if state.get("autosize") and state.get("space") == "client":
                            try:
                                resize_client(state["hwnd"], gw, gh)
                            except Exception as e:
                                print(f"[win] 还原后 resize 失败: {e!r}", flush=True)
                        await swap_source(state)
                    finally:
                        state["swapping"] = False
                    last = -1
                    idle_since = time.time()
                    continue
            # 采集源降级到 PrintWindow 几乎只发生在会话锁屏/桌面不活动：这时画面还能
            # 轮询出来，但 SendInput 已被安全桌面丢弃，表现为「能看不能点」。把状态
            # 显式推给页面，避免用户把它当成点击失灵再排查一遍。
            if src.name != announced:
                announced = src.name
                if src.name in ("wgc", "mon"):
                    hint = "已连接"
                elif state.get("wgc_bad") == state["hwnd"]:
                    # PrintWindow 现在是该窗口的正式通道，不是"锁屏降级"
                    hint = "已连接（该窗口改用 PrintWindow 采集）"
                else:
                    hint = "源端锁屏或桌面不活动：画面可看，点击/键盘可能无效"
                try:
                    await ws.send(json.dumps({"t": "state", "src": src.name, "hint": hint},
                                             ensure_ascii=False))
                except Exception:
                    pass
            # WGC 对微信窗口的实测失效形态：seq 在涨但内容恒为同一张图。
            # 判据不看 WGC 自己，而是隔 0.4s 用 PrintWindow 抓两次真实像素：
            # 真实画面在变、WGC 却没给出新帧 -> 这条通道不可信，永久降级 PrintWindow。
            if src.name == "wgc" and src.stale_for() > 1.0:
                now_p = time.time()
                if now_p - probe_t > 0.4:
                    probe_t = now_p
                    real_changed = False
                    try:
                        real = crop_to_client(state["hwnd"], print_window_bgr(state["hwnd"]))
                        if probe_prev is not None and probe_prev.shape == real.shape:
                            d = np.abs(real.astype(np.int16) - probe_prev.astype(np.int16))
                            real_changed = float(d.mean()) > 1.0
                        probe_prev = real
                    except Exception:
                        probe_prev = None
                    if real_changed and not state.get("swapping"):
                        print("[cap] WGC 画面已不更新（重复投递同一帧），改用 PrintWindow", flush=True)
                        state["wgc_bad"] = state["hwnd"]
                        state["swapping"] = True
                        try:
                            await swap_source(state)
                        finally:
                            state["swapping"] = False
                        last = -1
                        idle_since = time.time()
                        try:
                            await ws.send(json.dumps(
                                {"t": "state", "src": "pw",
                                 "hint": "已连接（该窗口改用 PrintWindow 采集）"},
                                ensure_ascii=False))
                        except Exception:
                            pass
                        announced = state["src"].name
                        continue
            jpeg, seq = src.snapshot()
            if jpeg is not None and seq != last:
                last = seq
                idle_since = time.time()
                try:
                    await asyncio.wait_for(ws.send(jpeg), 5)
                    send_fails = 0
                except asyncio.TimeoutError:
                    # 对端慢读（标签页切走 / 隧道抖动）时不要立刻断连：
                    # 断连会让页面重连、重连空窗期间的点击全部丢失（实测最像
                    # 「点了没反应」）。这里先丢帧继续，连续 10 次才认死。
                    send_fails += 1
                    if send_fails == 1 or send_fails % 5 == 0:
                        print(f"[pump] 发送超时 5s（第 {send_fails} 次），丢弃该帧继续", flush=True)
                    if send_fails >= 10:
                        print("[pump] 连续发送超时 10 次，断开等待页面重连", flush=True)
                        return
                    await asyncio.sleep(0.02)
                    continue
                sent += 1
                if sent == 1 or sent % 100 == 0:
                    print(f"[pump] sent={sent} seq={seq} bytes={len(jpeg)} err={src.error}", flush=True)
            else:
                # WGC 是变化驱动的，长时间无帧可能是会话锁屏导致捕获失效；
                # PrintWindow 则要在解锁后主动试回 WGC，否则会一直停在 10fps 轮询。
                now = time.time()
                # pw 是拉模式：没新帧就是真没变化，不需要"定期换源"；
                # 已被判定 WGC 不可信的窗口同样不再回头重试。
                due = False
                if src.error is not None:
                    # 采集回调一直在报错（句柄失效 / ClientToScreen 失败等）时 seq 不再
                    # 增长，光靠 60s 的兜底计时器会让用户盯着冻结画面等一分钟。
                    if err_since is None:
                        err_since = now
                        print(f"[pump] 采集源报错 {src.name}: {src.error}", flush=True)
                    due = now - err_since > 1.5
                else:
                    err_since = None
                    if state.get("mode") != "pw" and state.get("wgc_bad") != state["hwnd"]:
                        due = ((now - idle_since > 60) if src.name == "wgc"
                               else (now - wgc_retry > 60))
                if due and not state.get("swapping"):
                    if src.name != "wgc":
                        wgc_retry = now
                    state["swapping"] = True
                    try:
                        await swap_source(state)
                    finally:
                        state["swapping"] = False
                    last = -1
                    err_since = None
                    idle_since = time.time()
            await asyncio.sleep(0.01)
            fails = 0
        except Exception as e:
            # 采集源会随窗口重建失效（实测抛 Failed to convert item to
            # `GraphicsCaptureItem`）。旧实现一跳出循环画面就永久冻结，但输入仍在注入，
            # 用户看到的是"点了/拖了没反应"。这里换源续推；只有 ws 断开或连续失败
            # 过多才真正退出（页面会自己重连，新连接另起一个泵）。
            fails += 1
            print(f"[pump] 帧泵异常（第 {fails} 次）：{type(e).__name__}: {e}", flush=True)
            if fails > 20:
                print("[pump] 连续失败过多，停止推流", flush=True)
                return
            try:
                await ws.send(json.dumps(
                    {"t": "state", "src": "recover",
                     "hint": "画面采集中断，正在自动恢复"}, ensure_ascii=False))
            except Exception:
                return
            state["swapping"] = True
            try:
                await swap_source(state)
            finally:
                state["swapping"] = False
            last = -1
            idle_since = time.time()
            await asyncio.sleep(0.5)


ROOT = os.path.dirname(os.path.abspath(__file__))


async def serve_page(path, headers):
    """非 WS 升级的请求直接回 client.html，浏览器打开 http://host:port/ 即可用。

    websockets 13.x 的 legacy 服务端签名是 (path, headers)；新签名是
    (connection, request)，两种都接。
    """
    if hasattr(headers, "headers"):  # 新签名：request 对象
        headers = headers.headers
    up = headers.get("Upgrade") if headers is not None else None
    if (up or "").lower() == "websocket":
        return None
    print("[http] serve client.html", flush=True)
    try:
        with open(os.path.join(ROOT, "client.html"), "rb") as f:
            body = f.read()
    except OSError:
        body = b"client.html missing"
    from websockets.datastructures import Headers
    from websockets.http11 import Response
    return Response(200, "OK", Headers([
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ]), body)


async def wait_frame(src, tries=40):
    for _ in range(tries):
        if src.snapshot()[0] is not None:
            return True
        await asyncio.sleep(0.05)
    return False


async def main_async(hwnd, port, fps, quality, mode="auto", autosize=True, title="微信",
                     follow="weixin"):
    src = None
    if mode in ("mon", "auto"):
        # 微信主窗口的窗口级捕获全是冻结画面（见 MonFrameSource 注释），
        # 默认直接走显示器级捕获。
        try:
            mon = MonFrameSource(hwnd, fps, quality)
            mon.start()
            if await wait_frame(mon):
                src = mon
                print("[cap] 采集方式=显示器捕获（裁客户区）", flush=True)
            else:
                print("[cap] 显示器捕获 2s 内无帧：", mon.error or "未知", flush=True)
                mon.stop()
                if mode == "mon":
                    return 1
        except Exception as e:
            print(f"[cap] 显示器捕获启动异常：{e!r}", flush=True)
            if mode == "mon":
                return 1
    wgc_err = ""
    if src is None and mode in ("auto", "wgc"):
        # RDP 会话下 WGC 可能在 start() 直接抛异常，auto 必须降级 PrintWindow 而不是整个退出。
        try:
            wgc = FrameSource(hwnd, fps, quality)
            wgc.start()
            if await wait_frame(wgc):
                src = wgc
                print("[cap] 采集方式=WGC", flush=True)
            else:
                wgc_err = wgc.error or "2s 内无帧"
                if mode == "wgc":
                    print("ERROR: WGC 采集未出帧（会话断开或窗口不可见）：", wgc_err, flush=True)
                    return 1
        except Exception as e:
            wgc_err = repr(e)
            print(f"[cap] WGC 启动异常：{wgc_err}", flush=True)
            if mode == "wgc":
                return 1
    if src is None:
        pw = PwFrameSource(hwnd, fps, quality)
        pw.start()
        if not await wait_frame(pw):
            print(f"ERROR: 采集未出帧（WGC: {wgc_err}；PrintWindow: {pw.error}）", flush=True)
            return 1
        src = pw
        if mode == "pw":
            print("[cap] 采集方式=PrintWindow 轮询", flush=True)
        else:
            print(f"[cap] WGC 不可用（{wgc_err}），降级为 PrintWindow 轮询", flush=True)

    import websockets
    from websockets.asyncio.server import serve as ws_serve

    state = {"geom": (client_rect(hwnd)[0], client_rect(hwnd)[1]),
             "space": src.space, "src": src, "hwnd": hwnd, "title": title,
             "fps": fps, "quality": quality, "autosize": autosize, "follow": follow,
             "mode": mode, "wgc_bad": 0}
    async def handler(ws, *args):
        await handle(ws, state)

    async with ws_serve(handler, "0.0.0.0", port, max_size=None,
                        process_request=serve_page):
        cw, ch = client_rect(hwnd)
        print(f"listening ws://0.0.0.0:{port}  client_area={cw}x{ch}", flush=True)
        await asyncio.Future()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hwnd", nargs="?", type=lambda s: int(s, 0))
    ap.add_argument("--title", default="微信", help="按窗口标题自动定位（微信重启后 hwnd 会变）")
    ap.add_argument("--show", action="store_true", help="窗口隐藏时先 ShowWindow（WGC 采不了隐藏窗口）")
    ap.add_argument("--port", type=int, default=8098)
    ap.add_argument("--fps", type=int, default=20)
    ap.add_argument("--quality", type=int, default=70)
    ap.add_argument("--mode", choices=("mon", "auto", "wgc", "pw"), default="mon",
                    help="mon=显示器捕获后裁客户区（默认；微信窗口的窗口级捕获实测"
                         "只重复投递冻结画面，只有屏幕像素是真实的）；"
                         "wgc=窗口级 Windows Graphics Capture；auto=mon 失败再试 wgc/pw；"
                         "pw=PrintWindow 轮询")
    ap.add_argument("--no-autosize", dest="autosize", action="store_false",
                    help="不按客户端可见区调整源窗口尺寸（默认会调整，以便 1:1 传输）")
    ap.add_argument("--follow", choices=("off", "weixin", "all"), default="weixin",
                    help="跟随前台窗口：weixin=只跟微信系进程（默认），all=跟任意程序，off=只投初始窗口")
    a = ap.parse_args()
    hwnd = a.hwnd
    if hwnd is None:
        # 微信重启/重登的那几秒里窗口不存在，直接退出会让投屏彻底断掉；
        # 这里等最多 120s，微信一回来就能接上。
        deadline = time.time() + 120
        while True:
            hwnd = find_window(a.title)
            if hwnd is not None:
                break
            if time.time() > deadline:
                print(f"ERROR: 120s 内找不到标题为「{a.title}」的可见顶层窗口", flush=True)
                sys.exit(2)
            time.sleep(1)
        print(f"[locate] title={a.title} hwnd=0x{hwnd:X}", flush=True)
    if a.show and not user32.IsWindowVisible(hwnd):
        user32.ShowWindow(hwnd, 5)
        print("[show] 窗口原为隐藏，已 ShowWindow(5)", flush=True)
    sys.exit(asyncio.run(main_async(hwnd, a.port, a.fps, a.quality, a.mode, a.autosize,
                                    a.title, a.follow)))


if __name__ == "__main__":
    main()
