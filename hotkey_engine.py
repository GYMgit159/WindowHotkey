"""
WindowHotkey 引擎层：配置读写、开机自启、窗口枚举与切换、全局快捷键注册。

这一层完全不依赖界面框架，热键在独立线程的消息循环里生效，
界面只通过 events 队列取通知，因此可以被单元测试直接驱动。
"""

import ctypes
import ctypes.wintypes
import json
import os
import queue
import sys
import threading
import time
import winreg

import win32api
import win32con
import win32gui
import win32process

# ──────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────

APP_NAME = "WindowHotkey"
APP_CN_NAME = "窗口快捷键"
APP_VERSION = "2.0.0"

CONFIG_DIR = os.path.join(os.getenv("APPDATA", "."), APP_NAME)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

VK_NAMES = {}
for _i in range(26):
    VK_NAMES[0x41 + _i] = chr(0x41 + _i)
for _i in range(10):
    VK_NAMES[0x30 + _i] = str(_i)
for _i in range(12):
    VK_NAMES[0x70 + _i] = f"F{_i + 1}"
VK_NAMES.update({
    0x20: "Space", 0x0D: "Enter", 0x09: "Tab",
    0x1B: "Esc", 0x08: "Backspace", 0x2E: "Delete",
    0x2D: "Insert", 0x24: "Home", 0x23: "End",
    0x21: "PageUp", 0x22: "PageDown",
    0x26: "↑", 0x28: "↓", 0x25: "←", 0x27: "→",
    0xC0: "`", 0xBD: "-", 0xBB: "=",
    0xDB: "[", 0xDD: "]", 0xDC: "\\",
    0xBA: ";", 0xDE: "'", 0xBC: ",", 0xBE: ".", 0xBF: "/",
    0x60: "Num0", 0x61: "Num1", 0x62: "Num2", 0x63: "Num3",
    0x64: "Num4", 0x65: "Num5", 0x66: "Num6", 0x67: "Num7",
    0x68: "Num8", 0x69: "Num9",
    0x6A: "Num*", 0x6B: "Num+", 0x6D: "Num-", 0x6E: "Num.", 0x6F: "Num/",
    0x90: "NumLock", 0x91: "ScrollLock", 0x13: "Pause", 0x2C: "PrtSc",
})
VK_TO_NAME = dict(VK_NAMES)

MOD_ORDER = ["ctrl", "alt", "shift", "win"]
MOD_DISPLAY = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}
MOD_FLAG = {
    "ctrl": win32con.MOD_CONTROL,
    "alt": win32con.MOD_ALT,
    "shift": win32con.MOD_SHIFT,
    "win": win32con.MOD_WIN,
}
FLAG_TO_MOD = {
    win32con.MOD_CONTROL: "ctrl", win32con.MOD_ALT: "alt",
    win32con.MOD_SHIFT: "shift", win32con.MOD_WIN: "win",
}

# 鼠标按键当修饰键：RegisterHotKey 不支持鼠标，改用轮询检测
MOUSE_LBUTTON = 1
MOUSE_RBUTTON = 2
MOUSE_NAMES = {MOUSE_LBUTTON: "鼠标左键", MOUSE_RBUTTON: "鼠标右键"}
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02

# 录制时按 [ / ] 代表鼠标左 / 右键
MOUSE_KEYSYMS = {"[": MOUSE_LBUTTON, "]": MOUSE_RBUTTON}

IGNORED_TITLES = ("Program Manager", "MSCTFIME UI", "Default IME",
                  "Windows Input Experience")


# ──────────────────────────────────────────────
# 快捷键字符串
# ──────────────────────────────────────────────

def make_display(mod_flags, vk, mouse_btn=0):
    """Ctrl + Alt + W / 鼠标左键 + Q 这样的展示串。
    鼠标键和修饰键互斥（老配置就是这样存的），键名沿用旧格式。"""
    key = VK_NAMES.get(vk, "")
    if mouse_btn:
        parts = [MOUSE_NAMES[mouse_btn]]
    else:
        parts = [MOD_DISPLAY[FLAG_TO_MOD[flag]] for flag in
                 (MOD_FLAG["ctrl"], MOD_FLAG["alt"], MOD_FLAG["shift"],
                  MOD_FLAG["win"])
                 if mod_flags & flag]
    if key:
        parts.append(key)
    return " + ".join(parts)


def mod_keys_of(mod_flags):
    return [FLAG_TO_MOD[f] for f in (MOD_FLAG["ctrl"], MOD_FLAG["alt"],
                                     MOD_FLAG["shift"], MOD_FLAG["win"])
            if mod_flags & f]


# ──────────────────────────────────────────────
# 配置持久化
# ──────────────────────────────────────────────

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(data):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


# ──────────────────────────────────────────────
# 开机自启（写当前用户的 Run 项）
# ──────────────────────────────────────────────

def _get_exe_cmd():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    script = os.path.abspath(sys.argv[0]) if sys.argv else __file__
    exe = sys.executable
    base, name = os.path.split(exe)
    if name.lower() == "python.exe":
        quiet = os.path.join(base, "pythonw.exe")
        if os.path.exists(quiet):
            exe = quiet
    return f'"{exe}" "{script}"'


def is_autostart():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0,
                             winreg.KEY_READ)
        try:
            val, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(val)
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def set_autostart(enable):
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0,
                             winreg.KEY_SET_VALUE)
        try:
            if enable:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _get_exe_cmd())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
        return True, ""
    except Exception as e:
        return False, str(e)


# ──────────────────────────────────────────────
# 窗口工具
# ──────────────────────────────────────────────

def is_taskbar_window(hwnd):
    if not win32gui.IsWindowVisible(hwnd):
        return False
    title = win32gui.GetWindowText(hwnd)
    if not title or title in IGNORED_TITLES:
        return False
    ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    if ex_style & win32con.WS_EX_APPWINDOW:
        return True
    if ex_style & win32con.WS_EX_TOOLWINDOW:
        return False
    return ctypes.windll.user32.GetWindow(hwnd, 4) == 0


def _exe_of(hwnd):
    """返回 (进程文件名, 完整路径)；拿不到路径时留空，界面会退回占位图标。"""
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
            False, pid)
        try:
            exe = win32process.GetModuleFileNameEx(handle, 0)
        finally:
            win32api.CloseHandle(handle)
        return exe.rsplit("\\", 1)[-1], exe
    except Exception:
        return "unknown", ""


def get_taskbar_windows(exclude_pid=None):
    """[(hwnd, 标题, 进程文件名, 可执行文件完整路径)]"""
    results = []

    def callback(hwnd, _):
        if not is_taskbar_window(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        name, path = _exe_of(hwnd)
        if exclude_pid:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == exclude_pid:
                return True
        results.append((hwnd, title, name, path))
        return True

    win32gui.EnumWindows(callback, None)
    results.sort(key=lambda x: (x[2].lower(), x[1].lower()))
    return results


def find_window_by_exe(exe_name, exclude_pid=None):
    """进程名找窗口，用于程序重启后恢复句柄"""
    for hwnd, title, exe, path in get_taskbar_windows(exclude_pid):
        if exe.lower() == exe_name.lower():
            return hwnd, title, path
    return None, None, ""


def foreground_window():
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd or not is_taskbar_window(hwnd):
        return None, "", "unknown", ""
    name, path = _exe_of(hwnd)
    return hwnd, win32gui.GetWindowText(hwnd), name, path


def window_state(hwnd):
    """'min' / 'max' / 'normal' / 'gone'"""
    if not hwnd or not win32gui.IsWindow(hwnd):
        return "gone"
    try:
        show_cmd = win32gui.GetWindowPlacement(hwnd)[1]
    except Exception:
        return "gone"
    if show_cmd == win32con.SW_SHOWMINIMIZED:
        return "min"
    if show_cmd == win32con.SW_SHOWMAXIMIZED:
        return "max"
    return "normal"


def toggle_window(hwnd):
    if not hwnd or not win32gui.IsWindow(hwnd):
        return False
    if window_state(hwnd) == "min":
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    else:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    return True


def toggle_topmost(hwnd):
    """置顶开关：把窗口钉在最上层 / 取消"""
    if not hwnd or not win32gui.IsWindow(hwnd):
        return False
    ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    if ex & win32con.WS_EX_TOPMOST:
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                              | win32con.SWP_NOACTIVATE)
        return False
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                          | win32con.SWP_NOACTIVATE)
    return True


# ──────────────────────────────────────────────
# 全局快捷键管理器（独立线程 + 自己的消息循环）
# ──────────────────────────────────────────────

class HotkeyManager:

    def __init__(self):
        self._bindings = {}          # hid -> (hwnd, display)
        self._mouse_bindings = {}    # hid -> (hwnd, display, mouse_btn, vk)
        self._callbacks = {}         # hid -> callable（特殊快捷键，不动窗口）
        self._next_id = 1
        self._thread = None
        self._running = False
        self._thread_id = None
        self._lock = threading.Lock()
        self._pending_register = []
        self._pending_unregister = []
        self._active_keys = set()
        self.on_event = lambda kind, payload: None

    # ── 生命周期 ──
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        for _ in range(50):
            if self._thread_id:
                return True
            time.sleep(0.02)
        return bool(self._thread_id)

    def stop(self):
        self._running = False
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(
                self._thread_id, win32con.WM_QUIT, 0, 0)

    # ── 注册 ──
    def _wake(self):
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(
                self._thread_id, win32con.WM_USER, 0, 0)

    def register(self, hwnd, mod_flags, vk_code, display):
        hid = self._next_id
        self._next_id += 1
        with self._lock:
            self._pending_register.append((hid, mod_flags, vk_code, hwnd, display))
        self._wake()
        for _ in range(50):
            time.sleep(0.02)
            if hid in self._bindings:
                return True, hid
            with self._lock:
                if not any(r[0] == hid for r in self._pending_register):
                    return False, "快捷键注册失败，可能已被其他程序占用"
        return False, "快捷键注册超时"

    def unregister(self, hid):
        with self._lock:
            self._pending_unregister.append(hid)
        self._wake()

    def register_callback(self, mod_flags, vk_code, callback, display):
        hid = self._next_id
        self._next_id += 1
        self._callbacks[hid] = callback
        with self._lock:
            self._pending_register.append((hid, mod_flags, vk_code, 0, display))
        self._wake()
        for _ in range(50):
            time.sleep(0.02)
            if hid in self._bindings:
                return True, hid
            with self._lock:
                if not any(r[0] == hid for r in self._pending_register):
                    self._callbacks.pop(hid, None)
                    return False, "快捷键注册失败，可能已被其他程序占用"
        self._callbacks.pop(hid, None)
        return False, "快捷键注册超时"

    def unregister_callback(self, hid):
        self._callbacks.pop(hid, None)
        self.unregister(hid)

    def register_mouse_key(self, hwnd, mouse_btn, vk_code, display):
        """鼠标键 + 键盘键：无钩子，靠轮询，注册一定成功"""
        hid = self._next_id
        self._next_id += 1
        with self._lock:
            self._mouse_bindings[hid] = (hwnd, display, mouse_btn, vk_code)
        return True, hid

    def unregister_mouse_key(self, hid):
        with self._lock:
            self._mouse_bindings.pop(hid, None)

    def drop(self, hid):
        if hid in self._mouse_bindings:
            self.unregister_mouse_key(hid)
        elif hid in self._callbacks:
            self.unregister_callback(hid)
        else:
            self.unregister(hid)

    def update_hwnd(self, hid, new_hwnd):
        with self._lock:
            if hid in self._bindings:
                _, disp = self._bindings[hid]
                self._bindings[hid] = (new_hwnd, disp)
            if hid in self._mouse_bindings:
                _, disp, mb, vk = self._mouse_bindings[hid]
                self._mouse_bindings[hid] = (new_hwnd, disp, mb, vk)

    def display_of(self, hid):
        with self._lock:
            if hid in self._bindings:
                return self._bindings[hid][1]
            if hid in self._mouse_bindings:
                return self._mouse_bindings[hid][1]
        return ""

    # ── 内部 ──
    def _process(self):
        with self._lock:
            regs = list(self._pending_register)
            self._pending_register.clear()
            unregs = list(self._pending_unregister)
            self._pending_unregister.clear()
        for hid, mod, vk, hwnd, disp in regs:
            ok = ctypes.windll.user32.RegisterHotKey(None, hid, mod, vk)
            if ok:
                self._bindings[hid] = (hwnd, disp)
        for hid in unregs:
            ctypes.windll.user32.UnregisterHotKey(None, hid)
            self._bindings.pop(hid, None)

    def _poll_mouse_keys(self):
        if not self._mouse_bindings:
            return
        get_key = ctypes.windll.user32.GetAsyncKeyState
        lbtn = get_key(VK_LBUTTON) & 0x8000
        rbtn = get_key(VK_RBUTTON) & 0x8000
        mouse = MOUSE_LBUTTON if lbtn else (MOUSE_RBUTTON if rbtn else 0)
        if not mouse:
            self._active_keys.clear()
            return
        fired = []
        with self._lock:
            items = list(self._mouse_bindings.items())
        for hid, (hwnd, disp, mb, vk) in items:
            if mb == mouse and (get_key(vk) & 0x8000):
                if vk not in self._active_keys:
                    self._active_keys.add(vk)
                    fired.append((hid, hwnd, disp))
            elif not (get_key(vk) & 0x8000) and vk in self._active_keys:
                self._active_keys.discard(vk)
        for hid, hwnd, disp in fired:
            self._fire(hid, hwnd, disp)

    def _fire(self, hid, hwnd, disp):
        self.on_event("fired", {"hid": hid, "display": disp})
        if hwnd:
            threading.Thread(target=toggle_window, args=(hwnd,),
                             daemon=True).start()

    def _loop(self):
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = ctypes.wintypes.MSG()
        while self._running:
            self._process()
            self._poll_mouse_keys()
            ret = ctypes.windll.user32.PeekMessageW(
                ctypes.byref(msg), None, 0, 0, win32con.PM_REMOVE)
            if ret:
                if msg.message == win32con.WM_QUIT:
                    break
                if msg.message == win32con.WM_HOTKEY:
                    hid = msg.wParam
                    with self._lock:
                        info = self._bindings.get(hid)
                        cb = self._callbacks.get(hid)
                    if not info:
                        continue
                    hwnd, disp = info
                    if cb:
                        threading.Thread(target=cb, daemon=True).start()
                    else:
                        self._fire(hid, hwnd, disp)
            else:
                wait_ms = 30 if self._mouse_bindings else 200
                ctypes.windll.user32.MsgWaitForMultipleObjects(
                    0, None, False, wait_ms, 0x0100)
        with self._lock:
            for hid in list(self._bindings):
                ctypes.windll.user32.UnregisterHotKey(None, hid)
            self._bindings.clear()
            self._mouse_bindings.clear()
            self._callbacks.clear()


# ──────────────────────────────────────────────
# 绑定条目 + 面向界面的编排
# ──────────────────────────────────────────────

class Entry:
    __slots__ = ("hid", "hwnd", "title", "exe", "path", "mod_flags", "vk",
                 "mouse_btn", "display")

    def __init__(self, hid, hwnd, title, exe, mod_flags, vk, mouse_btn, display,
                 path=""):
        self.hid = hid
        self.hwnd = hwnd
        self.title = title
        self.exe = exe
        self.path = path
        self.mod_flags = mod_flags
        self.vk = vk
        self.mouse_btn = mouse_btn
        self.display = display

    @property
    def alive(self):
        return bool(self.hwnd) and win32gui.IsWindow(self.hwnd)

    def to_dict(self):
        return {"exe": self.exe, "title": self.title, "mod_flags": self.mod_flags,
                "vk": self.vk, "mouse_btn": self.mouse_btn,
                "display": self.display, "path": self.path}

    @staticmethod
    def from_dict(d):
        return Entry(0, 0, d.get("title", ""), d.get("exe", "unknown"),
                     int(d.get("mod_flags", 0)), int(d.get("vk", 0)),
                     int(d.get("mouse_btn", 0)), d.get("display", ""),
                     d.get("path", ""))


DEFAULT_QUICK_BIND = {"mod_flags": MOD_FLAG["ctrl"] | MOD_FLAG["alt"],
                      "vk": 0x42, "display": "Ctrl + Alt + B"}
DEFAULT_PIN = {"mod_flags": MOD_FLAG["ctrl"] | MOD_FLAG["alt"],
               "vk": 0x50, "display": "Ctrl + Alt + P"}


class Engine:
    """界面唯一需要打交道的对象：绑定表 + 快捷键 + 配置文件 + 事件队列"""

    def __init__(self):
        self.mgr = HotkeyManager()
        self.mgr.on_event = self._push
        self.entries = []
        self.quick_bind = dict(DEFAULT_QUICK_BIND)
        self.pin = dict(DEFAULT_PIN)
        self.autostart = False
        self.motion = True
        self.appearance = "light"
        self.events = queue.Queue()
        self._pid = os.getpid()
        self._qb_hid = None
        self._pin_hid = None
        self._pinned = set()

    # ── 事件桥：热键线程 → 界面线程 ──
    def _push(self, kind, payload):
        self.events.put((kind, payload))

    def drain(self):
        """界面用定时器轮询；返回这批事件后清空"""
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                break
        return out

    # ── 启动 / 收尾 ──
    def start(self, register=True):
        """register=False：只读配置，不占系统热键。截图自检用。"""
        if register:
            self.mgr.start()
        cfg = load_config()
        self.autostart = is_autostart()
        self.motion = bool(cfg.get("motion", True))
        self.appearance = cfg.get("appearance", "light")
        qb = cfg.get("quick_bind_hotkey") or DEFAULT_QUICK_BIND
        pn = cfg.get("pin_hotkey") or DEFAULT_PIN
        self.quick_bind = {"mod_flags": int(qb.get("mod_flags", 0)),
                           "vk": int(qb.get("vk", 0)),
                           "display": qb.get("display", "")}
        self.pin = {"mod_flags": int(pn.get("mod_flags", 0)),
                    "vk": int(pn.get("vk", 0)),
                    "display": pn.get("display", "")}
        for raw in cfg.get("bindings", []):
            try:
                self._restore(Entry.from_dict(raw), register)
            except Exception:
                continue
        if register:
            self._register_specials()

    def shutdown(self):
        self.mgr.stop()

    def _restore(self, e, register=True):
        hwnd, new_title, path = find_window_by_exe(e.exe, self._pid)
        if hwnd:
            e.hwnd, e.title = hwnd, new_title
            if path:
                e.path = path
        if not register:
            e.hid = len(self.entries) + 1
            self.entries.append(e)
            return True
        if e.mouse_btn:
            ok, hid = self.mgr.register_mouse_key(e.hwnd, e.mouse_btn, e.vk, e.display)
        else:
            ok, hid = self.mgr.register(e.hwnd, e.mod_flags, e.vk, e.display)
        if not ok:
            self._push("conflict", {"display": e.display, "title": e.title})
            return False
        e.hid = hid
        self.entries.append(e)
        return True

    # ── 绑定操作 ──
    def find_by_exe(self, exe):
        for e in self.entries:
            if e.exe.lower() == exe.lower():
                return e
        return None

    def add(self, hwnd, title, exe, mod_flags, vk, mouse_btn, path=""):
        if self.find_by_exe(exe):
            return False, "这个程序已经绑过了"
        display = make_display(mod_flags, vk, mouse_btn)
        if mouse_btn:
            ok, hid = self.mgr.register_mouse_key(hwnd, mouse_btn, vk, display)
        else:
            ok, hid = self.mgr.register(hwnd, mod_flags, vk, display)
        if not ok:
            return False, str(hid)
        self.entries.append(Entry(hid, hwnd, title, exe, mod_flags, vk,
                                  mouse_btn, display, path))
        self.save()
        return True, display

    def drop(self, hid):
        for i, e in enumerate(self.entries):
            if e.hid == hid:
                self.mgr.drop(hid)
                self.entries.pop(i)
                self.save()
                return e
        return None

    def drop_all(self):
        n = len(self.entries)
        for e in self.entries:
            self.mgr.drop(e.hid)
        self.entries.clear()
        self.save()
        return n

    def recheck(self):
        """每几秒跑一次：目标窗口关了要变灰，重开了要自动接回句柄"""
        changed = []
        for e in self.entries:
            if e.alive:
                continue
            hwnd, title, path = find_window_by_exe(e.exe, self._pid)
            if hwnd:
                e.hwnd, e.title = hwnd, title
                if path:
                    e.path = path
                self.mgr.update_hwnd(e.hid, hwnd)
                changed.append(("recovered", e))
            else:
                changed.append(("lost", e))
        if changed:
            self.save()
        return changed

    def save(self):
        data = load_config()
        data["bindings"] = [e.to_dict() for e in self.entries]
        data["quick_bind_hotkey"] = self.quick_bind
        data["pin_hotkey"] = self.pin
        data["autostart"] = self.autostart
        data["motion"] = self.motion
        save_config(data)

    def windows(self):
        return get_taskbar_windows(self._pid)

    # ── 特殊快捷键 ──
    def _register_specials(self):
        if self._qb_hid is None:
            ok, hid = self.mgr.register_callback(
                self.quick_bind["mod_flags"], self.quick_bind["vk"],
                lambda: self._push("quick_bind", {}), self.quick_bind["display"])
            self._qb_hid = hid if ok else None
        if self._pin_hid is None:
            ok, hid = self.mgr.register_callback(
                self.pin["mod_flags"], self.pin["vk"],
                lambda: self._push("pin", {}), self.pin["display"])
            self._pin_hid = hid if ok else None

    def change_quick_bind(self, mod_flags, vk):
        return self._rebind("quick_bind", mod_flags, vk)

    def change_pin(self, mod_flags, vk):
        return self._rebind("pin", mod_flags, vk)

    def _rebind(self, which, mod_flags, vk):
        key = getattr(self, which)
        display = make_display(mod_flags, vk)
        hid_attr = "_qb_hid" if which == "quick_bind" else "_pin_hid"
        old = getattr(self, hid_attr)
        if old is not None:
            self.mgr.unregister_callback(old)
            setattr(self, hid_attr, None)
        cb = (lambda: self._push("quick_bind", {})) if which == "quick_bind" \
            else (lambda: self._push("pin", {}))
        ok, result = self.mgr.register_callback(mod_flags, vk, cb, display)
        if not ok:
            # 新的被占用，退回原来的
            back = self.mgr.register_callback(key["mod_flags"], key["vk"], cb,
                                              key["display"])
            if back[0]:
                setattr(self, hid_attr, back[1])
            return False, str(result)
        setattr(self, hid_attr, result)
        key.update({"mod_flags": mod_flags, "vk": vk, "display": display})
        self.save()
        return True, display

    # ── 置顶 ──
    def toggle_pin(self, hwnd=None):
        hwnd = hwnd or foreground_window()[0]
        if not hwnd:
            return False, None
        on = toggle_topmost(hwnd)
        if on:
            self._pinned.add(hwnd)
        else:
            self._pinned.discard(hwnd)
        return on, hwnd

    @property
    def pinned(self):
        return set(self._pinned)
