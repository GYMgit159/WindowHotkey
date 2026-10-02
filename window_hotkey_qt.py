"""
WindowHotkey · 给窗口绑定快捷键，一键最小化 / 恢复

· 左侧深色栏：品牌 + 键帽舞台 + 全局开关（快速绑定 / 窗口置顶 / 开机自启 / 界面动效）
· 右侧浅色工作区：选窗口 → 录键 → 绑定列表
· 热键在 hotkey_engine 的独立线程里生效，界面只从事件队列取通知
· 没有任务栏按钮、没有窗口缩略图：Qt.WindowType.Tool + 托盘常驻
"""

import math
import os
import sys
import time
import traceback

from PyQt6.QtCore import (
    QFileInfo,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QPropertyAnimation,
    QVariantAnimation,
    pyqtProperty,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFontMetrics,
    QIcon,
    QLinearGradient,
    QPainter,
    QPen,
    QRadialGradient,
)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication,
    QFileIconProvider,
    QGraphicsOpacityEffect,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QScrollArea,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

import hotkey_engine as E
import whk_design as V

APP_TITLE = "窗口快捷键"
WIN_W, WIN_H = 964, 660
RAIL_W = 320
HEAD_H = 62


def C(name, alpha=1.0):
    """颜色在绘制时从令牌取，切主题只要重绘，不用重建界面。"""
    return V.T.c(name, alpha) if isinstance(name, str) else QColor(name)


# ──────────────────────────────────────────────
# Qt 按键 → Windows VK
# ──────────────────────────────────────────────

_K = Qt.Key
_QT_TO_VK = {}
for _i in range(26):
    _QT_TO_VK[getattr(_K, "Key_%s" % chr(65 + _i))] = 0x41 + _i
for _i in range(10):
    _QT_TO_VK[getattr(_K, "Key_%d" % _i)] = 0x30 + _i
for _i in range(12):
    _QT_TO_VK[getattr(_K, "Key_F%d" % (_i + 1))] = 0x70 + _i
_QT_TO_VK.update({
    _K.Key_Space: 0x20, _K.Key_Return: 0x0D, _K.Key_Enter: 0x0D,
    _K.Key_Tab: 0x09, _K.Key_Escape: 0x1B, _K.Key_Backspace: 0x08,
    _K.Key_Delete: 0x2E, _K.Key_Insert: 0x2D, _K.Key_Home: 0x24,
    _K.Key_End: 0x23, _K.Key_PageUp: 0x21, _K.Key_PageDown: 0x22,
    _K.Key_Up: 0x26, _K.Key_Down: 0x28, _K.Key_Left: 0x25, _K.Key_Right: 0x27,
    _K.Key_QuoteLeft: 0xC0, _K.Key_Minus: 0xBD, _K.Key_Equal: 0xBB,
    _K.Key_BracketLeft: 0xDB, _K.Key_BracketRight: 0xDD, _K.Key_Backslash: 0xDC,
    _K.Key_Semicolon: 0xBA, _K.Key_Apostrophe: 0xDE, _K.Key_Comma: 0xBC,
    _K.Key_Period: 0xBE, _K.Key_Slash: 0xBF,
    _K.Key_NumLock: 0x90, _K.Key_ScrollLock: 0x91, _K.Key_Pause: 0x13,
    _K.Key_Print: 0x2C,
})

MOD_QT = {
    Qt.KeyboardModifier.ControlModifier: "ctrl",
    Qt.KeyboardModifier.AltModifier: "alt",
    Qt.KeyboardModifier.ShiftModifier: "shift",
    Qt.KeyboardModifier.MetaModifier: "win",
}
_VK_MODS = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0x14, 0x15, 0x16}

_ICON_CACHE = {}
_provider = None


def exe_pixmap(path):
    """程序自己的图标；取不到返回 None，由调用方画线描占位。"""
    global _provider
    if not path:
        return None
    if path in _ICON_CACHE:
        return _ICON_CACHE[path]
    pm = None
    try:
        _provider = _provider or QFileIconProvider()
        got = _provider.icon(QFileInfo(path)).pixmap(QSize(40, 40))
        pm = None if got.isNull() else got
    except Exception:
        pm = None
    _ICON_CACHE[path] = pm
    return pm


def paint_app_icon(p, path, box, glyph="monitor"):
    pm = exe_pixmap(path)
    if pm is None:
        V.rounded(p, box, 8, C("inset"))
        V.hair(p, box, 8, C("line_soft"))
        V.paint_glyph(p, glyph, C("ink_faint"), box.center().x(),
                      box.center().y(), box.width() * 0.52)
        return
    p.drawPixmap(QRect(int(box.left()), int(box.top()),
                       int(box.width()), int(box.height())), pm)


def qss_lineedit():
    t = V.T
    return ("QLineEdit{border:none;background:transparent;padding-left:34px;"
            "color:%s;font:%dpx \"%s\";selection-background-color:%s;"
            "selection-color:%s;outline:none}") % (
        t.s("ink"), V.BODY, t and V.faces()["cjk"], t.s("brand"), t.s("on_brand"))


def qss_scrollbar():
    t = V.T
    return ("QScrollArea{border:none;background:transparent;}"
            "QScrollBar:vertical{width:8px;background:transparent;margin:2px;}"
            "QScrollBar::handle:vertical{background:%s;border-radius:4px;"
            "min-height:26px;}"
            "QScrollBar::handle:vertical:hover{background:%s;}"
            "QScrollBar::add-line,QScrollBar::sub-line{height:0;}"
            "QScrollBar::add-page,QScrollBar::sub-page{background:transparent;}") % (
        t.s("line"), t.s("ink_faint"))


# ──────────────────────────────────────────────
# 深色栏
# ──────────────────────────────────────────────

class LogoMark(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(36, 36)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.5, 0.5, 35, 35)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0.0, C("brand_hi"))
        g.setColorAt(1.0, C("brand_deep"))
        V.rounded(p, r, 11, QBrush(g))
        V.hair(p, r, 11, C("rail_line", 0.22))
        V.paint_glyph(p, "keyboard", C("on_brand"), 18, 18, 20, 1.4)


class Hero(QWidget):
    """舞台：一颗主键帽加几颗修饰键小帽；快捷键真的按下时整组键帽会沉下去。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(RAIL_W - 52, 132)
        self._mods = []
        self._main = ""
        self._caption = ""
        self._press = 0.0
        self._ring = 0.0

    def set_hotkey(self, display, caption=None):
        toks = V.caps_of(display)
        self._main = toks[-1] if toks else ""
        self._mods = toks[:-1]
        if caption is not None:
            self._caption = caption
        self.update()

    def caption(self):
        return self._caption

    def fire(self, display, caption):
        self.set_hotkey(display, caption)
        v = QVariantAnimation(self)
        v.setDuration(V.dur(260))
        v.setEasingCurve(V.OUT)
        v.setStartValue(0.0)
        v.setEndValue(1.0)

        def frame(t):
            self._press = math.sin(math.pi * t)
            self._ring = 1.0 - t
            self.update()

        v.valueChanged.connect(frame)
        v.finished.connect(self._settled)
        V.keep(self, v)
        v.start()

    def _settled(self):
        self._press = 0.0
        self._ring = 0.0
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        w = self.width()
        if not self._main:
            r = QRectF(0.5, 6.5, w - 1, 83)
            V.rounded(p, r, 16, C("rail_line", 0.045))
            V.hair(p, r, 16, C("rail_line", 0.12))
            V.paint_glyph(p, "keyboard", C("rail_faint"), w / 2, 44, 26)
        else:
            f = V.f_disp(16, 600)
            widths = [min(78, V.advance(m, f) + 26) for m in self._mods]
            side = 84 - 9 * self._press
            x = 0.0
            for m, mw in zip(self._mods, widths):
                V.keycap(p, QRectF(x, 28, mw, 34), m, C("rail_2"),
                         C("rail_line", 0.20), C("rail_0"), C("rail_ink"),
                         press=self._press * 0.6, font=f, radius=9)
                x += mw + 8
            x += 10
            main = QRectF(x, (90 - side) / 2, side, side)
            V.keycap(p, main, self._main, C("cap_face"), C("cap_edge"),
                     C("cap_side"), C("cap_ink"), press=self._press * 0.9,
                     font=V.f_disp(40, 700), radius=14)
            if self._ring > 0.01:
                g = 12 * (1 - self._ring)
                p.setPen(QPen(C("brand_hi", 0.55 * self._ring), 1.6))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(main.adjusted(-g, -g, g, g), 14 + g, 14 + g)
        fm = QFontMetrics(V.f_cjk(V.AUX, 400))
        p.setFont(V.f_cjk(V.AUX, 400))
        p.setPen(C("rail_dim"))
        p.drawText(QRectF(0, 96, w, 20),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(self._caption, Qt.TextElideMode.ElideRight, w))


RAIL_TEXT = {
    "quick": ("快速绑定当前窗口", "抓当前窗口，补一组键"),
    "pin": ("窗口置顶开关", "钉住最前窗口，再按放开"),
}


class RailRow(QWidget):
    def __init__(self, parent, win, icon, which, y):
        super().__init__(parent)
        self.win = win
        self.which = which
        self._icon = icon
        self._hot = 0.0
        self._recording = False
        self.setFixedSize(RAIL_W - 52, 52)
        self.move(26, y)
        self.setMouseTracking(True)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.caps = V.Caps("", 22, parent=self)
        V.attach_tip(self, "点这里改这组快捷键")

    def refresh(self):
        cfg = self.win.engine.quick_bind if self.which == "quick" \
            else self.win.engine.pin
        self.caps.set_display(cfg["display"])
        self.caps.move(self.width() - max(1, self.caps.sizeHint().width()) - 2, 6)
        self.update()

    def set_recording(self, on):
        self._recording = bool(on)
        self.update()

    def enterEvent(self, e):
        self._hot = 1.0
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hot = 0.0
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.win.start_record(self.which)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        r = QRectF(0, 0, self.width(), self.height())
        if self._hot or self._recording:
            V.rounded(p, r, 12, C("rail_line", 0.09 if self._recording else 0.05))
        V.paint_glyph(p, self._icon, C("warn") if self._recording else C("rail_dim"),
                      12, 20, 15)
        title, sub = RAIL_TEXT[self.which]
        tw = self.width() - 30 - self.caps.width() - 14
        p.setFont(V.f_cjk(V.BODY, 400))
        p.setPen(C("rail_ink") if (self._hot or self._recording) else C("rail_dim"))
        p.drawText(QRectF(30, 5, tw, 24),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "按一组新键…" if self._recording else title)
        p.setFont(V.f_cjk(V.TINY, 400))
        p.setPen(C("rail_faint"))
        p.drawText(QRectF(30, 29, tw, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sub)


class SwitchRow(QWidget):
    def __init__(self, parent, win, name, sub, icon, y, which):
        super().__init__(parent)
        self.win = win
        self.which = which
        self._icon = icon
        self.setFixedSize(RAIL_W - 52, 44)
        self.move(26, y)
        text_w = self.width() - 30 - 52
        self.lbl = V.Label(name, V.f_cjk(V.BODY, 400), "rail_dim",
                           elide=True, parent=self)
        self.lbl.setFixedWidth(text_w)
        self.lbl.move(30, 3)
        self.sub = V.Label(sub, V.f_cjk(V.TINY, 400), "rail_faint",
                           elide=True, parent=self)
        self.sub.setFixedWidth(text_w)
        self.sub.move(30, 23)
        self.sw = V.Switch(on=False, on_change=self._flip, parent=self)
        self.sw.move(self.width() - 46, 10)

    def _flip(self, on):
        self.win.set_flag(self.which, on)

    def set_on(self, v):
        self.sw.set_on(bool(v), animate=False)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        V.paint_glyph(p, self._icon, C("rail_faint"), 12, 22, 15)


class Rail(QWidget):
    """深色锚点：竖向渐变 + 两颗缓慢漂移的光斑（环境层）+ 细线键位插图。"""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setFixedSize(RAIL_W, WIN_H)
        self._t = 0.0
        self._wipe = 1.0
        V.breathe(self, 9000, 0.0, 1.0, "_t")
        self.logo = LogoMark(self)
        self.logo.move(26, 26)
        self.title = V.Label(APP_TITLE, V.f_head(19, 700), "rail_ink", parent=self)
        self.title.move(72, 22)
        self.ver = V.Label("WindowHotkey v" + E.APP_VERSION, V.f_num(V.TINY, 400),
                           "rail_faint", parent=self)
        self.ver.move(73, 45)
        self.hero = Hero(self)
        self.hero.move(26, 76)
        self.num = V.Label("0", V.f_disp(V.NUM + 6, 700), "rail_ink", parent=self)
        self.num.move(24, 224)
        self.num_sub = V.Label("组绑定在运行", V.f_cjk(V.AUX, 400), "rail_faint",
                               parent=self)
        self.row_qb = RailRow(self, win, "keyboard", "quick", 296)
        self.row_pin = RailRow(self, win, "pin", "pin", 348)
        self.row_auto = SwitchRow(self, win, "开机自启", "登录 Windows 后自动在托盘待命",
                                  "power", 418, "autostart")
        self.row_motion = SwitchRow(self, win, "界面动效", "关掉只留淡入淡出，位移全部去掉",
                                    "bolt", 462, "motion")
        self.note = V.Paragraph("关掉窗口不会退出，程序收在右下角托盘里。",
                                V.f_cjk(V.AUX, 400), "rail_faint",
                                width=RAIL_W - 52, parent=self)
        self.note.move(26, WIN_H - 98)
        self.b_tray = V.Button("收进托盘", "tray", height=34, kind="soft",
                               on_click=win.to_tray, parent=self)
        self.b_tray.setFixedWidth(122)
        self.b_tray.move(24, WIN_H - 54)
        self.b_quit = V.Button("退出", "power", height=34, kind="soft",
                               on_click=win.quit, parent=self)
        self.b_quit.setFixedWidth(96)
        self.b_quit.move(24 + 130, WIN_H - 54)

    def set_count(self, n):
        text = str(n)
        changed = self.num.text() != text
        self.num.setText(text)
        w = V.advance(text, V.f_disp(V.NUM + 6, 700))
        self.num_sub.move(24 + w + 12, 245)
        if changed:
            V.pop(self.num, 240)

    def run_wipe(self, ms=V.D_ENTER):
        self._wipe = 0.0
        a = QPropertyAnimation(self, b"wipe", self)
        a.setDuration(V.dur(ms))
        a.setEasingCurve(V.OUT_SOFT)
        a.setStartValue(0.0)
        a.setEndValue(1.0)
        V.keep(self, a)
        a.start()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._wipe < 1.0:
            p.setClipRect(QRectF(0, 0, self.width() * max(0.02, self._wipe),
                                 self.height()))
        g = QLinearGradient(0, 0, self.width() * 0.65, self.height())
        g.setColorAt(0.0, C("rail_0"))
        g.setColorAt(0.5, C("rail_1"))
        g.setColorAt(1.0, C("rail_2"))
        p.fillRect(self.rect(), QBrush(g))
        ph = 0.5 - 0.5 * math.cos(2 * math.pi * self._t)
        for cx, cy, rad, a, drift in ((266, 150, 214, 0.22, 22),
                                      (56, 520, 186, 0.12, -16)):
            dx = drift * ph
            rg = QRadialGradient(QPointF(cx + dx, cy), rad)
            rg.setColorAt(0.0, C("rail_glow", a))
            rg.setColorAt(1.0, C("rail_glow", 0.0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(rg))
            p.drawEllipse(QRectF(cx - rad + dx, cy - rad, rad * 2, rad * 2))
        p.setPen(QPen(C("rail_line", 0.055), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(8):
            p.drawRoundedRect(QRectF(RAIL_W - 158 + i * 16, 220 + (i % 3) * 7,
                                     14, 14), 4, 4)
        for y, a in ((286, 0.10), (408, 0.07), (522, 0.10)):
            p.setPen(QPen(C("rail_line", a), 1.0))
            p.drawLine(QPointF(26, y), QPointF(RAIL_W - 26, y))


Rail.wipe = pyqtProperty(float, fget=lambda s: s._wipe,
                         fset=lambda s, v: (setattr(s, "_wipe", v), s.update()))


# ──────────────────────────────────────────────
# 录快捷键
# ──────────────────────────────────────────────

class Recorder(QWidget):
    def __init__(self, win, parent=None, width=250):
        super().__init__(parent)
        self.win = win
        self.setFixedSize(width, 52)
        self._value = None
        self._armed = False
        self._pending_mouse = 0
        self._hint = "点这里，按一组键"
        self._tint = 0.0
        self._dash = 0.0
        self._anim = None
        self.caps = V.Caps("", 24, parent=self)
        self.caps.move(14, 13)
        self.caps.hide()
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

    def value(self):
        return self._value

    def set_value(self, display):
        if display:
            self.caps.set_display(display)
            self.caps.move(14, (self.height() - self.caps.height()) // 2)
            self.caps.show()
        else:
            self.caps.hide()
        self.update()

    def reset(self):
        self._value = None
        self._pending_mouse = 0
        self.set_value("")
        self.set_hint("点这里，按一组键")

    def set_hint(self, t):
        self._hint = t
        self.update()

    def arm(self):
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def enterEvent(self, e):
        self._tint = 1.0
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._tint = 0.0
        self.update()
        super().leaveEvent(e)

    def focusInEvent(self, e):
        self._armed = True
        self._spin()
        self.update()

    def focusOutEvent(self, e):
        self._armed = False
        self._pending_mouse = 0
        if self._anim:
            self._anim.stop()
        self.update()

    def _spin(self):
        """录制中让虚线跑起来，「正在听你按键」这件事要看得见。"""
        if V.ANIM_SCALE < 0.5 or (self._anim and self._anim.state()
                                  == QVariantAnimation.State.Running):
            return
        a = QVariantAnimation(self)
        a.setDuration(1400)
        a.setLoopCount(-1)
        a.setStartValue(0.0)
        a.setEndValue(6.0)
        a.valueChanged.connect(self._dash_to)
        V.keep(self, a)
        a.start()
        self._anim = a

    def _dash_to(self, v):
        self._dash = v
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and not self._armed:
            self.arm()
            return
        if self._armed and e.button() in (Qt.MouseButton.LeftButton,
                                          Qt.MouseButton.RightButton):
            self._pending_mouse = (E.MOUSE_LBUTTON
                                   if e.button() == Qt.MouseButton.LeftButton
                                   else E.MOUSE_RBUTTON)
            self.set_hint("保持按住鼠标，再按一个字母键")

    def keyPressEvent(self, e):
        if not self._armed:
            e.ignore()
            return
        key = e.key() & 0x0FFFFFFF
        if key == _K.Key_Escape:
            self.reset()
            self.clearFocus()
            self.win.record_cancelled()
            return
        flags = 0
        for m, name in MOD_QT.items():
            if e.modifiers() & m:
                flags |= E.MOD_FLAG[name]
        vk = e.nativeVirtualKey() or _QT_TO_VK.get(key)
        if vk is None or vk == 0:
            e.ignore()
            return
        if vk in _VK_MODS:
            self.set_hint("接着按一个字母或功能键")
            return
        mouse = 0
        if not flags:
            if vk in (0xDB, 0xDD):
                # [ / ] 是鼠标左/右键的替身，按下它只是开始，还要等一个字母键
                self._pending_mouse = E.MOUSE_KEYSYMS["[" if vk == 0xDB else "]"]
                self.set_hint("已记下鼠标%s，再按一个字母键" %
                              ("左键" if vk == 0xDB else "右键"))
                self.update()
                return
            if self._pending_mouse:
                mouse = self._pending_mouse
        self._pending_mouse = 0
        if mouse:
            flags = 0
        elif not flags:
            self.set_hint("至少要带一个修饰键")
            V.shake(self, 7, 2, 260)
            return
        display = E.make_display(flags, vk, mouse)
        self._value = (flags, vk, mouse, display)
        self.set_value(display)
        self.clearFocus()
        self.win.on_recorded(self._value)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        V.rounded(p, r, 13, C("surface"))
        if self._armed:
            pen = QPen(C("warn", 0.9), 1.4)
            pen.setDashPattern([3, 3])
            pen.setDashOffset(self._dash)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r, 13, 13)
        else:
            V.hair(p, r, 13, C("brand", 0.42) if self._tint else C("line"))
        if self._value is None:
            p.setFont(V.f_cjk(V.BODY, 400))
            p.setPen(C("warn") if self._armed else C("ink_faint"))
            p.drawText(QRectF(14, 0, self.width() - 28, self.height()),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       self._hint)


# ──────────────────────────────────────────────
# 选窗口
# ──────────────────────────────────────────────

class PickField(QWidget):
    def __init__(self, win, parent=None, width=286):
        super().__init__(parent)
        self.win = win
        self.setFixedSize(width, 52)
        self._hwnd = 0
        self._title = self._exe = self._path = ""
        self._tint = 0.0
        self._flash = 0.0
        self.setMouseTracking(True)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

    def value(self):
        return self._hwnd, self._title, self._exe, self._path

    def set_pick(self, hwnd, title, exe, path):
        self._hwnd, self._title, self._exe, self._path = hwnd, title, exe, path
        self.update()

    def enterEvent(self, e):
        self._tint = 1.0
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._tint = 0.0
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.win.open_picker(self)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        V.rounded(p, r, 13, C("surface"))
        if self._flash > 0.001:
            V.rounded(p, r, 13, C("brand", self._flash))
        V.hair(p, r, 13, C("brand", 0.42) if self._tint else C("line"))
        if not self._title:
            V.paint_glyph(p, "search", C("ink_faint"), 28, self.height() / 2, 17)
            p.setFont(V.f_cjk(V.BODY, 400))
            p.setPen(C("ink_faint"))
            p.drawText(QRectF(48, 0, self.width() - 60, self.height()),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       "选择窗口")
        else:
            paint_app_icon(p, self._path,
                           QRectF(13, (self.height() - 30) / 2, 30, 30))
            avail = self.width() - 60 - 24
            p.setFont(V.f_cjk(14, 500))
            p.setPen(C("ink"))
            p.drawText(QRectF(54, 9, avail, 19),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       QFontMetrics(V.f_cjk(14, 500)).elidedText(
                           self._title, Qt.TextElideMode.ElideRight, int(avail)))
            p.setFont(V.f_num(V.TINY, 400))
            p.setPen(C("ink_dim"))
            p.drawText(QRectF(54, 27, avail, 15),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       self._exe)
        V.paint_glyph(p, "chevron", C("ink_faint"), self.width() - 20,
                      self.height() / 2, 13)


class PickRow(QWidget):
    def __init__(self, item, win, popup):
        super().__init__(popup)
        self.hwnd, self.title, self.exe, self.path = item
        self.win = win
        self.popup = popup
        self._hot = False
        self.setFixedHeight(38)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

    def set_hot(self, on):
        self._hot = bool(on)
        self.update()

    def enterEvent(self, e):
        self.set_hot(True)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.set_hot(False)
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.pos()):
            self.choose()

    def choose(self):
        self.popup.close()
        self.win.confirm_pick(self.hwnd, self.title, self.exe, self.path)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        r = QRectF(0, 0, self.width(), self.height())
        if self._hot:
            V.rounded(p, r, 9, C("brand_ice"))
        paint_app_icon(p, self.path, QRectF(6, (self.height() - 22) / 2, 22, 22))
        f = V.f_cjk(V.BODY, 400)
        avail = self.width() - 44 - 118
        p.setFont(f)
        p.setPen(C("ink"))
        p.drawText(QRectF(36, 0, avail, self.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(f).elidedText(self.title, Qt.TextElideMode.ElideRight,
                                              int(avail)))
        p.setFont(V.f_num(V.TINY, 400))
        p.setPen(C("ink_faint"))
        p.drawText(QRectF(self.width() - 118, 0, 108, self.height()),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   self.exe)


class PickerPopup(QWidget):
    """Popup 类型自带「点外面就关闭」。"""

    def __init__(self, win):
        super().__init__(None, Qt.WindowType.Popup |
                         Qt.WindowType.FramelessWindowHint)
        self.win = win
        self.setFixedWidth(434)
        self._rows = []
        self._items = []
        self._idx = -1
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)
        box = QWidget(self)
        box.setFixedHeight(38)
        self.search = QLineEdit(box)
        self.search.setPlaceholderText("搜索窗口标题或程序名")
        self.search.setGeometry(0, 2, 410, 34)
        self.search.setStyleSheet(qss_lineedit())
        self.search.textChanged.connect(self._fill)
        self.search_box = box
        self.host = BgHost("surface")
        self.rows_lay = QVBoxLayout(self.host)
        self.rows_lay.setContentsMargins(0, 0, 0, 0)
        self.rows_lay.setSpacing(2)
        self.rows_lay.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.host)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFixedHeight(252)
        lay.addWidget(box)
        lay.addWidget(self.scroll)

    def load(self, windows):
        self._items = windows
        self._fill("")
        self.search.setFocus(Qt.FocusReason.OtherFocusReason)

    def _fill(self, text):
        for r in self._rows:
            r.setParent(None)
            r.deleteLater()
        self._rows = []
        self._idx = -1
        q = (text or "").lower()
        shown = [w for w in self._items
                 if not q or q in w[1].lower() or q in w[2].lower()][:60]
        for i, w in enumerate(shown):
            row = PickRow(w, self.win, self)
            self.rows_lay.insertWidget(self.rows_lay.count() - 1, row)
            self._rows.append(row)
            if V.ANIM_SCALE > 0.5 and i < 10:
                V.fade_in(row, 180, 0.0, 1.0)
        if not shown:
            lbl = V.Label("没有匹配的窗口", V.f_cjk(V.BODY, 400), "ink_faint")
            self.rows_lay.insertWidget(self.rows_lay.count() - 1, lbl)
            self._rows.append(lbl)

    def keyPressEvent(self, e):
        rows = [r for r in self._rows if isinstance(r, PickRow)]
        if e.key() in (_K.Key_Down, _K.Key_Up) and rows:
            self._idx = (self._idx + (1 if e.key() == _K.Key_Down else -1)) % len(rows)
            for i, r in enumerate(rows):
                r.set_hot(i == self._idx)
        elif e.key() in (_K.Key_Return, _K.Key_Enter) and rows:
            if 0 <= self._idx < len(rows):
                rows[self._idx].choose()
            elif rows:
                rows[0].choose()
        elif e.key() == _K.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        V.rounded(p, r, 16, C("surface"))
        V.hair(p, r, 16, C("line"))
        box = QRectF(12.5, 14.5, 409, 34)
        V.rounded(p, box, 10, C("inset"))
        V.hair(p, box, 10, C("line_soft"))
        V.paint_glyph(p, "search", C("ink_faint"), 32, 31, 15)


# ──────────────────────────────────────────────
# 绑定行 · 空状态
# ──────────────────────────────────────────────

class BgHost(QWidget):
    """滚动区视口默认刷一层白，深色主题下就是条白带；自己铺底色。"""

    def __init__(self, token, parent=None):
        super().__init__(parent)
        self._token = token

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), C(self._token))


class BindingRow(QWidget):
    def __init__(self, entry, win):
        super().__init__(win.list_host)
        self.e = entry
        self.win = win
        self.setFixedHeight(56)
        self._base = None
        self._lift = 0.0
        self._flash = 0.0
        self.setMouseTracking(True)
        self.caps = V.Caps(entry.display, 26, parent=self)
        self.title = V.Label(entry.title, V.f_cjk(14, 500), "ink",
                             elide=True, parent=self)
        self.sub = V.Label(entry.exe, V.f_num(V.TINY, 400), "ink_dim",
                           elide=True, parent=self)
        self.state = V.Label("", V.f_cjk(V.TINY, 400), "ink_faint", parent=self)
        self.dot = V.StatusDot("ok", 8, parent=self)
        self.acts = QWidget(self)
        lay = QHBoxLayout(self.acts)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(V.IconButton("bolt", "立刻切换一次", size=30, tone="brand",
                                   on_click=self.fire))
        lay.addWidget(V.IconButton("unlink", "解除绑定", size=30, tone="bad",
                                   on_click=self.remove))
        self.acts.setFixedSize(68, 30)
        self._eff = QGraphicsOpacityEffect(self.acts)
        self.acts.setGraphicsEffect(self._eff)
        self._eff.setOpacity(0.0)

    def relayout(self):
        cw = max(1, self.caps.sizeHint().width())
        self.caps.move(56, (self.height() - self.caps.height()) // 2)
        x = 56 + cw + 16
        avail = max(30, self.width() - x - 156)
        self.title.setGeometry(x, 8, avail, 20)
        self.sub.setGeometry(x, 30, avail, 16)
        self.acts.move(self.width() - 74, 13)
        sw = V.advance(self.state.text(), V.f_cjk(V.TINY, 400))
        self.state.setGeometry(self.width() - 96 - sw, 20, sw + 6, 16)
        self.dot.move(self.width() - 106 - sw, 22)

    def resizeEvent(self, _e):
        self.relayout()

    def refresh(self):
        alive = self.e.alive
        self.dot.set_state("ok" if alive else "lost")
        self.state.setText("已就绪" if alive else "窗口没开")
        self.state.setColor("ok" if alive else "ink_faint")
        self.title.setText(self.e.title)
        self.sub.setText(self.e.exe)
        self.caps.set_display(self.e.display)
        self.relayout()

    def fire(self):
        if self.e.alive:
            E.toggle_window(self.e.hwnd)
            self.caps.press_pulse()
            self.win.rail.hero.fire(self.e.display, "已切换 " + self.e.title)
            self.win.toast.post("ok", "已切换 " + self.e.display, self.e.title)
        else:
            self.win.toast.post("warn", "这个窗口现在没开", self.e.exe)

    def remove(self):
        self.win.unbind(self.e.hid)

    def enterEvent(self, e):
        if self._base is None:
            self._base = self.pos()
        V.move_to(self, b"pos", self._base + QPoint(0, -2), V.D_QUICK, V.OUT_SOFT)
        self._fade(1.0, V.D_QUICK, V.OUT)
        self._lift = 1.0
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        if self._base is not None:
            V.move_to(self, b"pos", self._base, V.D_STD, V.OUT_SOFT)
        self._fade(0.0, V.D_STD // 2, V.IN)
        self._lift = 0.0
        self.update()
        super().leaveEvent(e)

    def _fade(self, target, ms, ease):
        a = QPropertyAnimation(self._eff, b"opacity", self)
        a.setDuration(V.dur(ms))
        a.setEasingCurve(ease)
        a.setStartValue(self._eff.opacity())
        a.setEndValue(target)
        V.keep(self, a)
        a.start()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        V.rounded(p, r, 15, C("surface"))
        if self._flash > 0.001:
            V.rounded(p, r, 15, C("brand", self._flash))
        V.hair(p, r, 15, C("brand", 0.30) if self._lift else C("line_soft"))
        paint_app_icon(p, self.e.path,
                       QRectF(14, (self.height() - 30) / 2, 30, 30))


class EmptyCard(QWidget):
    def __init__(self, win):
        super().__init__(win.list_host)
        self.win = win
        self.setFixedHeight(178)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        r = QRectF(1, 1, self.width() - 2, self.height() - 2)
        pen = QPen(C("line"), 1.2)
        pen.setDashPattern([5, 5])
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, 18, 18)
        p.setPen(QPen(C("ink_faint", 0.6), 1.2))
        base = self.width() / 2 - 60
        for dx, dy, w, h in ((0, 10, 44, 30), (26, 2, 44, 30), (52, 12, 44, 30)):
            rect = QRectF(base + dx, 26 + dy, w, h)
            p.drawRoundedRect(rect, 5, 5)
            p.drawLine(rect.topLeft() + QPointF(0, 8),
                       rect.topRight() + QPointF(0, 8))
            for d in range(3):
                p.drawPoint(QPointF(rect.left() + 6 + d * 5, rect.top() + 4))
        p.setFont(V.f_head(V.SUB, 500))
        p.setPen(C("ink"))
        p.drawText(QRectF(0, 80, self.width(), 22),
                   Qt.AlignmentFlag.AlignCenter, "还没有绑定")
        p.setFont(V.f_cjk(V.BODY, 400))
        p.setPen(C("ink_dim"))
        p.drawText(QRectF(50, 104, self.width() - 100, 48),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                   "在上方选一个正在开的窗口，录一组键。\n绑好之后按这组键就能把它最小化，再按一次把它叫回来。")


# ──────────────────────────────────────────────
# 主窗口
# ──────────────────────────────────────────────

class DragBar(QWidget):
    def __init__(self, win, w):
        super().__init__(win.work)
        self.win = win
        self.setGeometry(0, 0, w, HEAD_H)
        self._grab = None

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        wh = self.window().windowHandle()
        if wh and wh.startSystemMove():
            return
        self._grab = (e.globalPosition().toPoint()
                      - self.window().frameGeometry().topLeft())

    def mouseMoveEvent(self, e):
        if self._grab and (e.buttons() & Qt.MouseButton.LeftButton):
            self.window().move(e.globalPosition().toPoint() - self._grab)

    def mouseReleaseEvent(self, e):
        self._grab = None


class Win(QWidget):
    def __init__(self, engine, shown=True):
        super().__init__()
        self.engine = engine
        self.rows = {}
        self._picker = None
        self._empty_card = None
        self._tray = None
        self.first_run = True
        self.setWindowTitle(APP_TITLE)
        self.setWindowFlags(Qt.WindowType.Tool |
                            Qt.WindowType.FramelessWindowHint)
        self.setFixedSize(WIN_W, WIN_H)
        self._build()
        self.sync_all()
        if shown:
            self.play_intro()

    # ── 装配 ──
    def _build(self):
        self.rail = Rail(self)
        self.work = QWidget(self)
        self.work.setGeometry(RAIL_W, 0, WIN_W - RAIL_W, WIN_H)

        self.bar = DragBar(self, self.work.width())
        self.h_title = V.Label("快捷键绑定", V.f_head(V.TITLE, 700), "ink",
                               parent=self.bar)
        self.h_title.move(26, 14)
        self.h_chip = V.Chip("0", "brand", "brand_ice", parent=self.bar,
                             font=V.f_disp(V.AUX, 700))
        self.h_sub = V.Label("按一次键收起来，再按一次回到桌面", V.f_cjk(V.AUX, 400),
                             "ink_dim", parent=self.bar)
        self.h_sub.move(27, 40)
        self.btn_theme = V.IconButton("moon", "切换深浅配色", size=32, parent=self.bar,
                                      tone="ink_dim", on_click=self.toggle_theme)
        self.btn_tray = V.IconButton("tray", "收进托盘", size=32, parent=self.bar,
                                     tone="ink_dim", on_click=self.to_tray)
        self.btn_quit = V.IconButton("power", "退出程序", size=32, parent=self.bar,
                                     tone="ink_dim", on_click=self.quit)

        self.task = V.Card(radius=20, parent=self.work, flat=True)
        self.task.setGeometry(26, 74, self.work.width() - 52, 116)
        self.f_step1 = V.Label("第 1 步 · 选窗口", V.f_cjk(V.AUX, 500), "ink_dim",
                               parent=self.task)
        self.f_step1.move(20, 12)
        self.f_step2 = V.Label("第 2 步 · 录快捷键", V.f_cjk(V.AUX, 500), "ink_dim",
                               parent=self.task)
        self.f_step2.move(272, 12)
        self.field = PickField(self, parent=self.task, width=240)
        self.field.move(20, 40)
        self.rec = Recorder(self, parent=self.task, width=190)
        self.rec.move(272, 40)
        V.attach_tip(self.rec, "想绑鼠标键：先按 [ 代表左键、] 代表右键，"
                               "再按一个字母键")
        self.btn_bind = V.Button("绑定", "plus", height=40, kind="primary",
                                 on_click=self.bind, parent=self.task)

        self.scroll = QScrollArea(self.work)
        self.scroll.setGeometry(26, 204, self.work.width() - 52, WIN_H - 204 - 60)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.viewport().setAutoFillBackground(False)
        self.scroll.horizontalScrollBar().setEnabled(False)
        self.list_host = BgHost("paper")
        self.rows_lay = QVBoxLayout(self.list_host)
        self.rows_lay.setContentsMargins(0, 2, 8, 2)
        self.rows_lay.setSpacing(8)
        self.rows_lay.addStretch(1)
        self.scroll.setWidget(self.list_host)
        self.scroll.setWidgetResizable(True)

        self.foot = QWidget(self.work)
        self.foot.setGeometry(26, WIN_H - 52, self.work.width() - 52, 40)
        self.btn_clear = V.Button("全部解除", "unlink", height=32, kind="soft",
                                  on_click=self.unbind_all, parent=self.foot)
        self.foot_note = V.Label("鼠标左/右键用 [ 和 ] 代替；组合键被占用时换一组再来",
                                 V.f_cjk(V.TINY, 400), "ink_faint", parent=self.foot)

        self.toast = V.Toast(self.work)
        self.apply_qss()
        self._place()

    def apply_qss(self):
        self.scroll.setStyleSheet(qss_scrollbar())
        if self._picker:
            self._picker.search.setStyleSheet(qss_lineedit())
            self._picker.scroll.setStyleSheet(qss_scrollbar())

    def _place(self):
        self.h_chip.move(26 + V.advance("快捷键绑定", V.f_head(V.TITLE, 700)) + 12, 18)
        right = self.bar.width() - 20
        for b in (self.btn_quit, self.btn_tray, self.btn_theme):
            right -= 32
            b.move(right, 16)
            right -= 8
        self.btn_bind.move(self.task.width() - self.btn_bind.width() - 20, 46)
        self.foot_note.move(self.btn_clear.width() + 18, 8)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), C("paper"))
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        V.hair(p, QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 1,
               C("line"))

    # ── 入场编排：深色栏擦入 → 内容分块上浮 → 键帽归位 ──
    def play_intro(self):
        if V.ANIM_SCALE < 0.5:
            return
        self.rail.run_wipe(V.D_ENTER)
        for w, delay, dy in ((self.bar, 130, 14), (self.task, 210, 18),
                             (self.scroll, 300, 20), (self.foot, 380, 12)):
            V.rise(w, dy, V.D_SLOW, delay)

    # ── 同步 ──
    def sync_all(self):
        live = {e.hid for e in self.engine.entries}
        for hid in list(self.rows):
            if hid not in live:
                r = self.rows.pop(hid)
                r.setParent(None)
                r.deleteLater()
        for e in self.engine.entries:
            if e.hid not in self.rows:
                row = BindingRow(e, self)
                self.rows[e.hid] = row
                self.rows_lay.insertWidget(self.rows_lay.count() - 1, row)
            self.rows[e.hid].refresh()
        self.rail.row_qb.refresh()
        self.rail.row_pin.refresh()
        self.rail.row_auto.set_on(self.engine.autostart)
        self.rail.row_motion.set_on(V.ANIM_SCALE > 0.5)
        self.rail.set_count(len(self.engine.entries))
        self.h_chip.setText(str(len(self.engine.entries)))
        self.refresh_hero()
        self.toggle_empty()
        self.update_tray()
        self.btn_theme.set_icon("sun" if V.T.dark else "moon")
        self.btn_theme._tip = "切回亮色" if V.T.dark else "切换深浅配色"

    def refresh_hero(self):
        if self.engine.entries:
            e = self.engine.entries[-1]
            self.rail.hero.set_hotkey(e.display, "最近绑定 · " + e.title)
        else:
            self.rail.hero.set_hotkey("", "选一个窗口，录一组键")

    def toggle_empty(self):
        show = not self.engine.entries
        if self._empty_card is None:
            if not show:
                return
            self._empty_card = EmptyCard(self)
            self.rows_lay.insertWidget(0, self._empty_card)
        if self._empty_card.isVisible() == show:
            return
        self._empty_card.setVisible(show)
        if show and V.ANIM_SCALE > 0.5:
            V.fade_in(self._empty_card, V.D_SLOW, 0.0, 1.0)

    # ── 选窗口 / 录键 ──
    def open_picker(self, field):
        wins = self.engine.windows()
        if not wins:
            self.toast.post("warn", "现在没有可绑定的窗口", "先把要绑的程序打开")
            return
        self._picker = PickerPopup(self)
        self._picker.load(wins)
        self._picker.move(field.mapToGlobal(QPoint(0, field.height() + 8)))
        self._picker.show()

    def confirm_pick(self, hwnd, title, exe, path):
        self.field.set_pick(hwnd, title, exe, path)
        V.flash(self.field, 520, 0.14)
        if self.rec.value() is None:
            self.rec.arm()

    def on_recorded(self, value):
        mod_flags, vk, mouse, display = value
        which = getattr(self, "_rec_target", None)
        if which in ("quick", "pin"):
            row = self.rail.row_qb if which == "quick" else self.rail.row_pin
            row.set_recording(False)
            self._rec_target = None
            ok, msg = (self.engine.change_quick_bind(mod_flags, vk)
                       if which == "quick" else self.engine.change_pin(mod_flags, vk))
            row.refresh()
            if ok:
                self.toast.post("ok", "快捷键已更新", msg)
                V.flash(row, 520, 0.12)
            else:
                self.toast.post("bad", "这组键用不了，已退回原来那组", msg)
        else:
            self.rail.hero.set_hotkey(display, "准备绑定 · 点右边的绑定按钮")

    def record_cancelled(self):
        which = getattr(self, "_rec_target", None)
        if which in ("quick", "pin"):
            row = self.rail.row_qb if which == "quick" else self.rail.row_pin
            row.set_recording(False)
        self._rec_target = None

    def start_record(self, which=None):
        self._rec_target = which
        for r in (self.rail.row_qb, self.rail.row_pin):
            r.set_recording(r.which == which)
        self.rec.reset()
        if which in ("quick", "pin"):
            self.rec.set_hint("为「%s」按一组新键" % RAIL_TEXT[which][0])
        else:
            self.rec.set_hint("按住修饰键，再按一个键")
        self.rec.arm()

    # ── 绑定 ──
    def bind(self):
        hwnd, title, exe, path = self.field.value()
        hk = self.rec.value()
        if not hwnd:
            self.toast.post("warn", "第 1 步还没选窗口", "点左边的选窗口框")
            V.shake(self.field, 8, 3, 340)
            return
        if not hk:
            self.toast.post("warn", "第 2 步还没录键", "鼠标左/右键用 [ 和 ] 代替")
            V.shake(self.rec, 8, 3, 340)
            self.start_record()
            return
        mod_flags, vk, mouse, display = hk
        if self.engine.find_by_exe(exe):
            self.toast.post("warn", "这个程序已经绑过了", exe)
            V.shake(self.field, 8, 3, 340)
            return
        ok, msg = self.engine.add(hwnd, title, exe, mod_flags, vk, mouse, path)
        if not ok:
            self.toast.post("bad", "绑定失败", msg)
            V.shake(self.rec, 9, 3, 360)
            return
        new = self.engine.entries[-1]
        row = BindingRow(new, self)
        self.rows[new.hid] = row
        self.rows_lay.insertWidget(self.rows_lay.count() - 1, row)
        row.refresh()
        self.toggle_empty()
        if V.ANIM_SCALE > 0.5:
            V.rise(row, 16, V.D_SLOW)
        V.flash(row, 620, 0.18)
        self.rail.hero.set_hotkey(display, "已绑定 · " + title)
        self.rail.set_count(len(self.engine.entries))
        self.h_chip.setText(str(len(self.engine.entries)))
        self.toast.post("ok", "已绑定 " + display, title)
        self.rec.reset()
        self.update_tray()
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def unbind(self, hid):
        target = next((e for e in self.engine.entries if e.hid == hid), None)
        if target is None:
            return
        disp, title = target.display, target.title
        row = self.rows.get(hid)

        def finish():
            self.engine.drop(hid)
            self.sync_all()
            self.toast.post("ok", "已解除 " + disp, title)

        if row is not None and V.ANIM_SCALE > 0.5:
            eff = QGraphicsOpacityEffect(row)
            row.setGraphicsEffect(eff)
            a = QPropertyAnimation(eff, b"opacity", row)
            a.setDuration(V.dur(180))
            a.setEasingCurve(V.IN)
            a.setStartValue(1.0)
            a.setEndValue(0.0)
            a.finished.connect(finish)
            V.keep(row, a)
            a.start()
        else:
            finish()

    def unbind_all(self):
        n = self.engine.drop_all()
        self.sync_all()
        if n:
            self.toast.post("ok", "已解除全部 %d 组绑定" % n, "列表已经空了")
        else:
            self.toast.post("info", "已经没有绑定了", "")

    # ── 开关 ──
    def set_flag(self, which, on):
        if which == "autostart":
            ok, err = E.set_autostart(on)
            self.engine.autostart = bool(ok and on)
            self.engine.save()
            self.rail.row_auto.set_on(self.engine.autostart)
            if ok:
                self.toast.post("ok", "开机自启已开启" if on else "开机自启已关闭",
                                "" if on else "注册表 Run 项已删除")
            else:
                self.toast.post("bad", "开机自启改不了", err)
        elif which == "motion":
            V.ANIM_SCALE = 1.0 if on else 0.22
            self.engine.motion = bool(on)
            self.engine.save()
            self.toast.post("info", "界面动效已开启" if on else "界面动效已精简",
                            "只保留 90 毫秒以内的淡入淡出")

    def toggle_theme(self):
        V.T = V.DARK if not V.T.dark else V.LIGHT
        cfg = E.load_config()
        cfg["appearance"] = "dark" if V.T.dark else "light"
        E.save_config(cfg)
        self.engine.appearance = cfg["appearance"]
        self.apply_qss()
        for w in self.findChildren(QWidget):
            w.update()
        self.update()

    # ── 托盘 ──
    def build_tray(self):
        self._tray = QSystemTrayIcon(QIcon(self._icon_path()), self)
        self._tray.setToolTip("窗口快捷键")
        menu = QMenu()
        menu.addAction("显示主界面", self.show_window)
        menu.addSeparator()
        act = menu.addAction("开机自启")
        act.setCheckable(True)
        act.setChecked(self.engine.autostart)
        act.toggled.connect(lambda v: self.set_flag("autostart", v))
        menu.addSeparator()
        menu.addAction("退出", self.quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    def _icon_path(self):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        p = os.path.join(base, "icon.ico")
        return p if os.path.exists(p) else ""

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.isVisible():
                self.to_tray()
            else:
                self.show_window()

    def update_tray(self):
        if self._tray:
            self._tray.setToolTip("窗口快捷键 · %d 组绑定" % len(self.engine.entries))

    def bubble(self, title, body, ms=1600):
        if self._tray:
            self._tray.showMessage(title, body,
                                   QSystemTrayIcon.MessageIcon.Information, ms)

    def to_tray(self):
        self.hide()
        if self.first_run:
            self.first_run = False
            self.bubble("已收进托盘", "点右下角的图标可以再打开", 2400)

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.play_intro()

    def quit(self):
        self.engine.save()
        self.engine.shutdown()
        QApplication.instance().quit()

    # ── 事件队列 ──
    def pump(self):
        for kind, payload in self.engine.drain():
            self.handle(kind, payload)

    def handle(self, kind, payload):
        if kind == "fired":
            hid = payload.get("hid")
            disp = payload.get("display", "")
            row = self.rows.get(hid)
            title = row.e.title if row else ""
            if row:
                row.caps.press_pulse()
                row.refresh()
            self.rail.hero.fire(disp, "已切换 " + title if title else "已切换")
            if self.isVisible():
                self.toast.post("ok", disp, "已切换 " + title if title else "")
            else:
                self.bubble("窗口快捷键 " + disp, title)
        elif kind == "quick_bind":
            self.quick_bind()
        elif kind == "pin":
            hwnd, title, exe, path = E.foreground_window()
            if not hwnd:
                self.toast.post("warn", "抓不到当前窗口", "先点一下那个窗口")
                return
            on, _ = self.engine.toggle_pin(hwnd)
            self.toast.post("ok" if on else "info",
                            "已置顶" if on else "已取消置顶", title)
        elif kind == "conflict":
            self.toast.post("warn", "有一组键被别的程序占了",
                            "%s（%s）" % (payload.get("display"),
                                         payload.get("title")))

    def quick_bind(self):
        hwnd, title, exe, path = E.foreground_window()
        if not hwnd:
            self.toast.post("warn", "抓不到当前窗口",
                            "先点一下要绑定的窗口，再按这组快捷键")
            return
        if self.engine.find_by_exe(exe):
            self.toast.post("warn", "这个程序已经绑过了", exe)
            return
        if not self.isVisible():
            self.show_window()
        self.field.set_pick(hwnd, title, exe, path)
        self.start_record("quick")
        self.rec.set_hint("给「%s」按一组键" % (title or exe)[:22])
        V.flash(self.field, 520, 0.14)

    def tick(self):
        changed = self.engine.recheck()
        for r in self.rows.values():
            r.refresh()
        if not changed:
            return
        self.h_chip.setText(str(len(self.engine.entries)))
        recovered = [e for k, e in changed if k == "recovered"]
        lost = [e for k, e in changed if k == "lost"]
        if recovered:
            self.toast.post("ok", "窗口又接回来了", recovered[0].exe)
        elif lost:
            self.toast.post("warn", "窗口关闭了", lost[0].exe)


# ──────────────────────────────────────────────
# 启动
# ──────────────────────────────────────────────

SOCKET = "windowhotkey_singleton"


def raise_existing():
    sock = QLocalSocket()
    sock.connectToServer(SOCKET)
    if not sock.waitForConnected(250):
        return False
    sock.write(b"show")
    sock.flush()
    sock.waitForBytesWritten(200)
    sock.disconnectFromServer()
    return True


def install_crash_log():
    """打包成 exe 后没有控制台，未捕获异常会让进程静默消失；落一份 crash.log。"""

    def hook(kind, val, tb):
        try:
            os.makedirs(E.CONFIG_DIR, exist_ok=True)
            with open(os.path.join(E.CONFIG_DIR, "crash.log"), "a",
                      encoding="utf-8") as f:
                f.write("\n%s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                f.write("".join(traceback.format_exception(kind, val, tb)))
        except Exception:
            pass
        sys.__excepthook__(kind, val, tb)

    sys.excepthook = hook


def main(argv):
    install_crash_log()
    app = QApplication(argv)
    app.setApplicationName(E.APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    if raise_existing():
        return 0
    try:
        QLocalServer.removeServer(SOCKET)
    except Exception:
        pass
    server = QLocalServer()
    server.listen(SOCKET)

    cfg = E.load_config()
    V.T = V.DARK if cfg.get("appearance") == "dark" else V.LIGHT
    V.ANIM_SCALE = 1.0 if cfg.get("motion", True) else 0.22

    engine = E.Engine()
    engine.motion = bool(cfg.get("motion", True))
    dry = "--dry" in argv
    engine.start(register=not dry)

    win = Win(engine, shown=False)
    if "--offscreen" in argv:
        win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    else:
        win.build_tray()
    app._whk = win
    if "--min" not in argv and "--grab" not in argv:
        win.show()
        win.play_intro()

    pump = QTimer()
    pump.timeout.connect(win.pump)
    pump.start(50)
    check = QTimer()
    check.timeout.connect(win.tick)
    check.start(3000)

    def wake():
        conn = server.nextPendingConnection()
        if conn:
            conn.deleteLater()
        win.show_window()

    server.newConnection.connect(wake)

    if "--grab" in argv:
        # 打包成 exe 后 argv[0] 是 WindowHotkey.exe，不能当成截图路径
        shots = [a for a in argv[argv.index("--grab") + 1:] if not a.startswith("--")]

        def shoot(i=0):
            if i >= len(shots):
                app.quit()
                return
            win.grab().save(shots[i])
            if i == 0 and len(shots) > 1:
                win.toggle_theme()
                QTimer.singleShot(160, lambda: shoot(1))
            else:
                QTimer.singleShot(60, lambda: shoot(i + 1))

        QTimer.singleShot(1000, lambda: (win.show(), win.sync_all(),
                                         QTimer.singleShot(700, shoot)))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
