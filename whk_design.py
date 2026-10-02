"""
WindowHotkey 视觉系统：色彩令牌、字体阶梯、图标、缓动与动效原语。

规则来自两套设计规范：
· 高端视觉规范（材质、双层嵌套卡片、超细描边图标、克制的彩色）
· 动效规范（Personality=Corporate/Premium、Duration 三元组、三层运动、stagger 预算 500ms）

Qt 样式表不支持 transition，所有「平滑变化」都靠 QPropertyAnimation / QVariantAnimation；
这个构建里没有 QBezierEasing，自定义 cubic-bezier 无法精确复现，
所以统一取 QEasingCurve 里最接近的 OutQuart / OutQuint / InOutSine 三条曲线。
"""

import ctypes
import math
import sys

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
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
    QFont,
    QFontDatabase,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QRadialGradient,
    QPen,
    QPixmap,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextOption,
)
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QFrame,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

LH_PROP = QTextBlockFormat.LineHeightTypes.ProportionalHeight.value

# ──────────────────────────────────────────────
# 字体：拉丁用 Bahnschrift（DIN 几何无衬线），中文标题 Noto Sans SC，正文微软雅黑 UI
# ──────────────────────────────────────────────

_FAMS = None


def families():
    """必须在 QApplication 建好之后调用，否则 Qt 会直接崩进程。"""
    global _FAMS
    if _FAMS is None:
        try:
            _FAMS = set(QFontDatabase.families())
        except Exception:
            _FAMS = set()
    return _FAMS


def _pick(cands):
    fams = families()
    for c in cands:
        if c in fams:
            return c
    return cands[-1]


# 字体解析必须推迟到 QApplication 之后：没有 app 时问字体会让进程直接消失。
_FACES = None


def faces():
    global _FACES
    if _FACES is None:
        _FACES = dict(
            display=_pick(["Bahnschrift SemiBold", "Bahnschrift",
                           "Segoe UI Semibold"]),
            display_l=_pick(["Bahnschrift Light", "Bahnschrift", "Segoe UI"]),
            latin=_pick(["Segoe UI", "Tahoma"]),
            cjk_head=_pick(["Noto Sans SC", "Microsoft YaHei UI"]),
            cjk=_pick(["Microsoft YaHei UI", "Noto Sans SC", "Microsoft YaHei"]),
            mono=_pick(["Cascadia Mono", "Consolas"]),
        )
    return _FACES


# 字号阶梯：11 / 12 / 13 / 15 / 20 / 34 / 56
TINY, AUX, BODY, SUB, TITLE, NUM, HERO = 11, 12, 13, 15, 20, 34, 56
LEAD_BODY, LEAD_TITLE = 1.55, 1.22


def _font(face, px, weight):
    f = QFont(face)
    f.setPixelSize(px)
    f.setWeight(weight)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f


def f_disp(px, weight=600):
    return _font(faces()["display"], px, weight)


def f_disp_light(px, weight=300):
    return _font(faces()["display_l"], px, weight)


def f_num(px, weight=500):
    return _font(faces()["mono"], px, weight)


def f_lat(px, weight=400):
    return _font(faces()["latin"], px, weight)


def f_cjk(px, weight=400):
    """中文正文：雅黑没有 500 这一档，>=520 一律升到 Bold，否则静默回退成 Regular。"""
    f = _font(faces()["cjk"], px, weight)
    f.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    return f


def f_head(px, weight=500):
    return _font(faces()["cjk_head"], px, weight if weight < 520 else 700)


def advance(text, font):
    return QFontMetrics(font).horizontalAdvance(text)


# ──────────────────────────────────────────────
# 色彩令牌
# ──────────────────────────────────────────────

def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class Theme:
    def __init__(self, dark, v):
        self.dark = dark
        self.__dict__.update(v)

    def s(self, name, alpha=1.0):
        r, g, b = getattr(self, name)
        a = ("%.3f" % alpha).rstrip("0").rstrip(".") or "0"
        return "rgba(%d,%d,%d,%s)" % (r, g, b, a)

    def c(self, name, alpha=1.0):
        r, g, b = getattr(self, name)
        return QColor(r, g, b, int(round(alpha * 255)))

    def rgba(self, name, alpha):
        return self.c(name, alpha)

    def mix(self, name, alpha, other):
        """和另一个令牌插值，用于渐变。"""
        c1, c2 = self.c(name, 1.0), self.c(other, 1.0)
        return lerp(c1, c2, alpha)


LIGHT = Theme(False, dict(
    # 结构灰阶。灰阶与语义色的具体数值是 WCAG AA 反算出来的（正文级文字最低 4.5:1），
    # 想「调淡一点」必须先重算对比度，否则会静默跌破可达线。
    paper=(243, 246, 251), surface=(255, 255, 255), surface_2=(250, 251, 254),
    inset=(238, 242, 249), line=(226, 232, 241), line_soft=(238, 242, 248),
    ink=(14, 25, 41), ink_dim=(72, 85, 105), ink_faint=(103, 110, 124),
    # 主色：深蓝，专业工具降饱和
    brand=(20, 82, 166), brand_hi=(32, 112, 214), brand_deep=(12, 52, 112),
    brand_ice=(233, 242, 252), brand_mist=(215, 231, 249),
    on_brand=(255, 255, 255),
    # 语义色：状态点、徽标，以及列表里的「已就绪」这类小字
    ok=(13, 125, 94), ok_ice=(228, 245, 238),
    warn=(158, 96, 7), warn_ice=(251, 240, 219),
    bad=(208, 62, 90), bad_ice=(251, 233, 237),
    # 深色锚点栏
    rail_0=(10, 20, 34), rail_1=(17, 31, 52), rail_2=(26, 45, 74),
    rail_ink=(240, 246, 255), rail_dim=(150, 170, 200), rail_faint=(130, 149, 175),
    rail_line=(255, 255, 255), rail_glow=(52, 124, 214),
    shadow=(13, 26, 45),
    # 键帽
    cap_face=(255, 255, 255), cap_edge=(228, 234, 244), cap_side=(206, 215, 230),
    cap_ink=(23, 38, 60), cap_top=(248, 250, 253),
))
DARK = Theme(True, dict(
    paper=(15, 21, 31), surface=(22, 30, 43), surface_2=(26, 35, 49),
    inset=(18, 25, 36), line=(40, 52, 71), line_soft=(33, 43, 60),
    ink=(232, 239, 250), ink_dim=(158, 173, 196), ink_faint=(131, 145, 166),
    brand=(90, 150, 235), brand_hi=(122, 176, 255), brand_deep=(46, 96, 176),
    brand_ice=(27, 42, 64), brand_mist=(35, 55, 82),
    on_brand=(8, 14, 24),
    ok=(52, 200, 152), ok_ice=(20, 44, 38),
    warn=(235, 168, 62), warn_ice=(46, 36, 20),
    bad=(240, 100, 128), bad_ice=(46, 24, 31),
    rail_0=(7, 12, 20), rail_1=(12, 20, 34), rail_2=(19, 32, 52),
    rail_ink=(236, 243, 255), rail_dim=(146, 166, 198), rail_faint=(118, 136, 163),
    rail_line=(255, 255, 255), rail_glow=(60, 132, 226),
    shadow=(0, 0, 0),
    cap_face=(38, 50, 68), cap_edge=(52, 66, 88), cap_side=(28, 38, 53),
    cap_ink=(222, 233, 248), cap_top=(48, 62, 84),
))

T = LIGHT          # 当前材质，切主题时整窗重建
ANIM_SCALE = 1.0   # 系统关掉动画时压缩到 0.3


def system_animations_on():
    """读系统的「窗口内动画」开关，等价于 web 的 prefers-reduced-motion。"""
    try:
        val = ctypes.c_int(0)
        ok = ctypes.windll.user32.SystemParametersInfoW(0x100F, 0, ctypes.byref(val), 0)
        return bool(ok and val.value)
    except Exception:
        return True


# ──────────────────────────────────────────────
# 图标：内置 SVG，1.25px 超细描边，一套圆头线帽
# ──────────────────────────────────────────────

_PATHS = dict(
    monitor='<rect x="3" y="4.5" width="18" height="12.5" rx="2.5"/><path d="M9.5 20.5h5M12 17v3.5"/>',
    sun='<circle cx="12" cy="12" r="3.8"/><path d="M12 3.1v2M12 18.9v2M3.1 12h2M18.9 12h2M5.6 5.6 7 7M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4"/>',
    moon='<path d="M19.8 14.4A8.4 8.4 0 0 1 9.6 4.2a8.4 8.4 0 1 0 10.2 10.2Z"/>',
    plus='<path d="M12 5.8v12.4M5.8 12h12.4"/>',
    minus='<path d="M5.8 12h12.4"/>',
    close='<path d="M6.8 6.8l10.4 10.4M17.2 6.8 6.8 17.2"/>',
    check='<path d="M5.2 12.6 9.6 17 18.8 6.9"/>',
    refresh='<path d="M19.8 12a7.8 7.8 0 1 1-2.5-5.8"/><path d="M19.8 4.6V9h-4.4"/>',
    unlink='<path d="M9.4 14.6 7.2 16.8a3.4 3.4 0 0 1-4.8-4.8l2.2-2.2M14.6 9.4l2.2-2.2a3.4 3.4 0 0 1 4.8 4.8l-2.2 2.2M8.8 8.8l6.4 6.4"/>',
    keyboard='<rect x="2.6" y="6.6" width="18.8" height="10.8" rx="2.4"/><path d="M6 10h.01M9.2 10h.01M12.4 10h.01M15.6 10h.01M6 13h.01M9.2 13h.01M12.4 13h.01M15.6 13h.01M8.6 15.6h6.8"/>',
    bolt='<path d="M13.2 3 6.2 13.2h4.6l-.9 7.4 7.9-10.8h-5.2Z"/>',
    pin='<path d="M9.5 3.8h5l-.7 5 3 3.3v1.3H7.2v-1.3l3-3.3Z"/><path d="M12 13.6v6.6"/>',
    chevron='<path d="M8.4 10.6 12 14.2l3.6-3.6"/>',
    search='<circle cx="11" cy="11" r="6.2"/><path d="M15.6 15.6 20 20"/>',
    mouse='<rect x="8" y="3.4" width="8" height="17.2" rx="4"/><path d="M12 3.6v6"/><path d="M8.1 9.6h7.8"/>',
    power='<path d="M12 3.6v7"/><path d="M18 6.4a8 8 0 1 1-12 0"/>',
    tray='<path d="M4.6 9.4h14.8L21 19.4H3Z"/><path d="M8 5.4h8M9.6 9.4V5.4M14.4 9.4V5.4"/>',
    star='<path d="M12 4.2l2.5 5.1 5.6.8-4 4 .9 5.6-5-2.7-5 2.7.9-5.6-4-4 5.6-.8Z"/>',
    zap='<path d="M13.6 2.6 5.4 13.8h4.8L9.4 21.4 18.6 9.8h-5.2Z"/>',
    link='<path d="M10.2 13.8a3.4 3.4 0 0 0 4.8 0l3-3a3.4 3.4 0 0 0-4.8-4.8l-1.2 1.2M13.8 10.2a3.4 3.4 0 0 0-4.8 0l-3 3a3.4 3.4 0 0 0 4.8 4.8l1.2-1.2"/>',
    dot='<circle cx="12" cy="12" r="3.4"/>',
    arrow='<path d="M5 12h13M12.6 6.4 18.4 12l-5.8 5.6"/>',
    edit='<path d="M4.6 19.4h4.2L19 8.2l-4.2-4.2L4.6 15.2Z"/><path d="M13.8 5.  6l4.2 4.2"/>',
)


def _svg(name, qcolor, w=1.25):
    rgb = qcolor.name(QColor.NameFormat.HexRgb)
    op = "" if qcolor.alpha() >= 255 else ' stroke-opacity="%.2f"' % (qcolor.alpha() / 255.0)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="%s" stroke-width="%.2f" stroke-linecap="round" '
            'stroke-linejoin="round"%s>') % (rgb, w, op)


def _path(name, qcolor, w=1.25):
    return (_svg(name, qcolor, w) % () if False else
            (_svg_head(name, qcolor, w) + _PATHS[name] + "</svg>"))


def _svg_head(name, qcolor, w):
    rgb = qcolor.name(QColor.NameFormat.HexRgb)
    op = "" if qcolor.alpha() >= 255 else ' stroke-opacity="%.2f"' % (qcolor.alpha() / 255.0)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="%s" stroke-width="%.2f" stroke-linecap="round" '
            'stroke-linejoin="round"%s>') % (rgb, w, op)


def _dpr():
    app = QApplication.instance()
    if app and app.primaryScreen():
        return float(app.primaryScreen().devicePixelRatio())
    return 1.0


def glyph_pixmap(name, color, px, stroke=1.25):
    sc = _dpr()
    side = max(1, int(round(px * sc)))
    rdr = QSvgRenderer(_path(name, color, stroke).encode("utf-8"))
    pm = QPixmap(side, side)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    rdr.render(p, QRectF(0, 0, side, side))
    p.end()
    pm.setDevicePixelRatio(sc)
    return pm


def paint_glyph(p, name, color, cx, cy, px, stroke=1.25):
    rdr = QSvgRenderer(_path(name, color, stroke).encode("utf-8"))
    p.save()
    p.translate(cx - px / 2.0, cy - px / 2.0)
    rdr.render(p, QRectF(0, 0, px, px))
    p.restore()


# ──────────────────────────────────────────────
# 绘制原语
# ──────────────────────────────────────────────

def rounded(p, rect, r, brush=None, pen=None):
    path = QPainterPath()
    path.addRoundedRect(QRectF(rect), r, r)
    p.setBrush(QBrush(brush) if brush is not None else Qt.BrushStyle.NoBrush)
    p.setPen(pen if pen is not None else Qt.PenStyle.NoPen)
    p.drawPath(path)


def rounded_path(rect, r):
    path = QPainterPath()
    path.addRoundedRect(QRectF(rect), r, r)
    return path


def lerp(c1, c2, t):
    """QColor.lerpColor 在这个构建里没绑定。"""
    t = max(0.0, min(1.0, t))
    return QColor(
        int(c1.red() + (c2.red() - c1.red()) * t),
        int(c1.green() + (c2.green() - c1.green()) * t),
        int(c1.blue() + (c2.blue() - c1.blue()) * t),
        int(c1.alpha() + (c2.alpha() - c1.alpha()) * t),
    )


def hair(p, rect, r, color, w=1.0):
    """1px 以内的发丝描边：Qt 的 pen 会向内外各扩 w/2，用 inset 修正。"""
    pen = QPen(color)
    pen.setWidthF(w)
    rounded(p, rect.adjusted(w / 2, w / 2, -w / 2, -w / 2), max(0, r - w / 2), None, pen)


def keycap(p, rect, legend, face, edge, side, ink, press=0.0, radius=None,
           font=None, align_center=True, glyph=None):
    """画一颗真实的键帽：侧壁 + 顶面 + 内高光 + 字模。press 0→1 让它按下去。"""
    r = radius if radius is not None else min(rect.height() * 0.26, 9)
    depth = max(2.0, rect.height() * 0.16)
    dy = rect.height() * 0.10 * press
    body = QRectF(rect.left(), rect.top() + dy, rect.width(), rect.height() - dy)

    # 侧壁
    wall = QRectF(body.left(), body.top() + depth * 0.35, body.width(),
                  body.height() - depth * 0.35)
    rounded(p, wall, r, QColor(side))
    # 顶面渐变
    top = QRectF(body.left(), body.top(), body.width(), body.height() - depth * 0.55)
    g = QLinearGradient(top.topLeft(), top.bottomLeft())
    g.setColorAt(0.0, lerp(face, QColor(255, 255, 255), 0.55 if face.value() > 0 else 0.06))
    g.setColorAt(1.0, face)
    path = rounded_path(top, r)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g))
    p.drawPath(path)
    # 描边
    hair(p, top, r, QColor(edge))
    # 顶部内高光
    hi = QColor(255, 255, 255, 60) if face.lightness() < 128 else QColor(255, 255, 255, 90)
    p.setPen(QPen(hi, 1.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    inset = QRectF(top.left() + 1.6, top.top() + 1.4, top.width() - 3.2,
                   top.height() - 3.2)
    p.drawPath(rounded_path(inset, max(0, r - 1.4)))

    if legend:
        f = font or f_disp(int(rect.height() * 0.42), 600)
        p.setFont(f)
        p.setPen(QColor(ink))
        flags = (Qt.AlignmentFlag.AlignCenter if align_center
                 else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        p.drawText(QRectF(top.left(), top.top() - depth * 0.12, top.width(),
                          top.height()), flags, legend)
    if glyph:
        paint_glyph(p, glyph, QColor(ink), top.center().x(), top.center().y() - depth * 0.1,
                    min(top.width(), top.height()) * 0.46)


def keycap_size(legend, px, min_w=None):
    f = f_disp(int(px * 0.42), 600)
    w = advance(legend, f) + px * 0.62
    return QSize(int(max(w, min_w or px)), int(px))


# ──────────────────────────────────────────────
# 动效原语
# ──────────────────────────────────────────────

OUT = QEasingCurve.Type.OutQuart        # 签名缓动
OUT_SOFT = QEasingCurve.Type.OutQuint
IN = QEasingCurve.Type.InQuad
SETTLE = QEasingCurve.Type.OutBack      # 只给 3% 过冲
LOOP = QEasingCurve.Type.InOutSine

D_TAP, D_QUICK, D_STD, D_SLOW, D_ENTER = 90, 140, 260, 420, 520


def dur(ms):
    return int(ms * ANIM_SCALE)


def keep(widget, *anims):
    """Qt 的动画对象没有引用就会被回收，必须挂在控件上。

    同一个桶里既有 QPropertyAnimation 也有 rise 的错峰 QTimer，
    后者没有 state()，直接调会把整个进程干掉。
    """
    bucket = getattr(widget, "_anim_bucket", None)
    if bucket is None:
        bucket = widget._anim_bucket = []

    def alive(a):
        if isinstance(a, QTimer):
            return a.isActive()
        return a.state() != QPropertyAnimation.State.Stopped

    bucket[:] = [a for a in bucket if alive(a)]
    bucket.extend(anims)
    return anims


def move_to(widget, prop, target, ms, ease=OUT):
    a = QPropertyAnimation(widget, prop, widget)
    a.setDuration(dur(ms))
    a.setEasingCurve(ease)
    a.setStartValue(widget.pos())
    a.setEndValue(target)
    keep(widget, a)
    a.start()
    return a


def run_prop(widget, name, start, end, ms, ease=OUT, on_frame=None, on_done=None):
    a = QPropertyAnimation(widget, name.encode(), widget)
    a.setDuration(dur(ms))
    a.setEasingCurve(ease)
    a.setStartValue(start)
    a.setEndValue(end)
    if on_frame:
        a.valueChanged.connect(on_frame)
    if on_done:
        a.finished.connect(on_done)
    keep(widget, a)
    a.start()
    return a


def fade_in(widget, ms=D_STD, from_=0.0, to=1.0, on_done=None):
    eff = widget.graphicsEffect()
    if not isinstance(eff, QGraphicsOpacityEffect):
        eff = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(eff)
    eff.setOpacity(from_)
    a = QPropertyAnimation(eff, b"opacity", widget)
    a.setDuration(dur(ms))
    a.setEasingCurve(OUT)
    a.setStartValue(from_)
    a.setEndValue(to)
    if on_done:
        a.finished.connect(on_done)
    keep(widget, a)
    a.start()
    return a


def rise(widget, dy=18, ms=D_ENTER, delay=0, on_done=None):
    """入场：淡入 + 上移，位移与透明度必须同时动（禁止纯 opacity 状态变化）。"""
    final = widget.pos()
    widget.move(final + QPoint(0, dy))
    eff = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(eff)
    eff.setOpacity(0.0)

    def go():
        fade_in(widget, ms, on_done=_finish)
        move_to(widget, b"pos", final, ms, OUT_SOFT)

    def _finish():
        # 补间对象的 finished 信号里直接删 effect 会踩到已释放对象，推到下一轮事件循环
        def drop():
            if widget.graphicsEffect() is eff:
                widget.setGraphicsEffect(None)
            if on_done:
                on_done()
        QTimer.singleShot(0, drop)

    if delay and ANIM_SCALE > 0.5:
        t = QTimer(widget)
        t.setSingleShot(True)
        t.timeout.connect(go)
        keep(widget, t)
        t.start(dur(delay))
    else:
        go()


def shake(widget, amp=9, cycles=3, ms=360):
    """错误反馈：水平往复，ease-in-out，不带过冲。"""
    base = widget.pos()
    v = QVariantAnimation(widget)
    v.setDuration(dur(ms))
    v.setEasingCurve(QEasingCurve.Type.InOutSine)
    steps = [0.0]
    n = cycles * 2
    for i in range(1, n + 1):
        steps.append(((-1) ** i) * amp * (1 - i / (n + 1.0)))
    steps.append(0.0)
    frames = [base + QPoint(int(s), 0) for s in steps]
    v.setStartValue(0.0)
    v.setEndValue(1.0)

    def on_t(t):
        i = min(len(frames) - 1, int(t * (len(frames) - 1)))
        widget.move(frames[i])

    v.valueChanged.connect(on_t)
    v.finished.connect(lambda: widget.move(base))
    keep(widget, v)
    v.start()


def pop(widget, ms=240):
    """成功反馈：短暂放大再回落，Corporate 档只给 4%。"""
    base = widget.geometry()

    def scaled(k):
        dw = int(base.width() * (k - 1) / 2)
        dh = int(base.height() * (k - 1) / 2)
        widget.setGeometry(base.adjusted(-dw, -dh, dw, dh))

    v = QVariantAnimation(widget)
    v.setDuration(dur(ms))
    v.setEasingCurve(SETTLE)
    v.setStartValue(1.0)
    v.setEndValue(1.0)

    def frame(t):
        k = 1.0 + 0.045 * math.sin(math.pi * min(1.0, t * 1.35))
        scaled(k)

    v.valueChanged.connect(frame)
    v.finished.connect(lambda: widget.setGeometry(base))
    keep(widget, v)
    v.start()


def flash(widget, ms=520, strength=0.16):
    """新条目/成功：底色泛一下主色，比缩放安全（缩放和布局管理器的位置会打架）。"""
    v = QVariantAnimation(widget)
    v.setDuration(dur(ms))
    v.setEasingCurve(OUT)
    v.setStartValue(1.0)
    v.setEndValue(0.0)

    def frame(t):
        widget._flash = t * strength
        widget.update()

    v.valueChanged.connect(frame)
    v.finished.connect(lambda: (setattr(widget, "_flash", 0.0), widget.update()))
    keep(widget, v)
    v.start()


def breathe(widget, ms=3200, lo=0.42, hi=1.0, on_attr="_breath"):
    """环境层：状态点的呼吸。"""
    setattr(widget, on_attr, hi)

    def frame(t):
        setattr(widget, on_attr, lo + (hi - lo) * (0.5 - 0.5 * math.cos(2 * math.pi * t)))
        widget.update()

    v = QVariantAnimation(widget)
    v.setDuration(dur(ms))
    v.setLoopCount(-1)
    v.setEasingCurve(LOOP)
    v.setStartValue(0.0)
    v.setEndValue(1.0)
    v.valueChanged.connect(frame)
    keep(widget, v)
    v.start()
    return v


def set_shadow(widget, blur, dy, alpha, color=None):
    """Qt 一个控件只能挂一个 effect：装阴影时要确认没有别的 effect 占用。"""
    eff = widget.graphicsEffect()
    if not isinstance(eff, QGraphicsDropShadowEffect):
        if eff is not None:
            return None
        eff = QGraphicsDropShadowEffect(widget)
        widget.setGraphicsEffect(eff)
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    eff.setColor(color or QColor(T.shadow[0], T.shadow[1], T.shadow[2],
                                int(alpha * 255)))
    return eff


def anim_shadow(widget, blur, dy, alpha, ms=D_QUICK, color=None):
    """阴影不能补间（会整层重算），只换目标值 + 让控件重绘，视觉上用位移补偿。"""
    eff = set_shadow(widget, blur, dy, alpha, color)
    widget.update()
    return eff


def detach_shadow(widget):
    if isinstance(widget.graphicsEffect(), QGraphicsDropShadowEffect):
        widget.setGraphicsEffect(None)


def _setter(key):
    def set_(self, v):
        setattr(self, key, v)
        self.update()
    return set_


def _getter(key):
    def get_(self):
        return getattr(self, key)
    return get_


# ──────────────────────────────────────────────
# 文本
# ──────────────────────────────────────────────

def resolve(color):
    """允许把令牌名（"ink"）当颜色传进来，绘制时才取当前主题，切主题不用重建界面。"""
    if isinstance(color, str):
        if color.startswith("#") or color.startswith("rgb"):
            return QColor(color)
        name, _, a = color.partition("@")
        return T.c(name, float(a) if a else 1.0)
    return QColor(color)


class Paragraph(QWidget):
    """需要行高的文字统一走这里（QLabel 不支持 line-height）。"""

    def __init__(self, text, font, color, leading=LEAD_BODY, align=None,
                 width=None, parent=None):
        super().__init__(parent)
        self._text = text
        self._font = font
        self._color = color
        self._leading = leading
        self._align = align or (Qt.AlignmentFlag.AlignLeft |
                               Qt.AlignmentFlag.AlignTop)
        self._fixed_w = width
        if width:
            self.setFixedWidth(width)
        sp = self.sizePolicy()
        sp.setHeightForWidth(True)
        sp.setRetainSizeWhenHidden(True)
        self.setSizePolicy(sp)
        self.setMinimumHeight(int(font.pixelSize() * leading) + 2)

    def hasHeightForWidth(self):
        return True

    def setText(self, t):
        if t == self._text:
            return
        self._text = t
        self._relayout()

    def setColor(self, c):
        self._color = c
        self.update()

    def _doc(self, w):
        d = QTextDocument()
        d.setPlainText(self._text)
        d.setDefaultFont(self._font)
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        opt.setAlignment(self._align)
        d.setDefaultTextOption(opt)
        cur = QTextCursor(d)
        cur.select(QTextCursor.SelectionType.Document)
        bf = cur.blockFormat()
        bf.setLineHeight(self._leading * 100.0, LH_PROP)
        cur.mergeBlockFormat(bf)
        cf = QTextCharFormat()
        cf.setForeground(QBrush(resolve(self._color)))
        cur.mergeCharFormat(cf)
        d.setTextWidth(max(1.0, float(w)))
        return d

    def heightForWidth(self, w):
        return int(self._doc(w).size().height()) + 2

    def sizeHint(self):
        w = self._fixed_w or max(self.width(), 120)
        return QSize(w, self.heightForWidth(w))

    def resizeEvent(self, _e):
        self._relayout()

    def _relayout(self):
        if self.width() <= 0:
            return
        h = self.heightForWidth(self.width())
        if abs(h - self.height()) > 1:
            self.setMinimumHeight(h)
            self.updateGeometry()
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self._doc(self.width()).drawContents(p, QRectF(0, 0, self.width(),
                                                       self.height()))


class Label(QWidget):
    """单行文本，自己画，保证 pixelSize 与字体权重严格生效，并且能省略号截断。"""

    def __init__(self, text, font, color, align=None, elide=False, parent=None,
                 tracking=0.0):
        super().__init__(parent)
        self._text = text
        self._font = font
        self._color = color
        self._align = align or (Qt.AlignmentFlag.AlignLeft |
                                Qt.AlignmentFlag.AlignVCenter)
        self._elide = elide
        self._tracking = tracking
        if tracking:
            f = QFont(font)
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, tracking)
            self._font = f
        self.setFixedHeight(int(font.pixelSize() * 1.5) + 2)

    def setText(self, t):
        if t == self._text:
            return
        self._text = t
        self.updateGeometry()
        self.update()

    def text(self):
        return self._text

    def setColor(self, c):
        self._color = c
        self.update()

    def setFont(self, f):
        self._font = f
        self.setFixedHeight(int(f.pixelSize() * 1.5) + 2)
        self.updateGeometry()
        self.update()

    def sizeHint(self):
        w = advance(self._text, self._font) + 2
        return QSize(w, int(self._font.pixelSize() * 1.5) + 2)

    def minimumSizeHint(self):
        if self._elide:
            return QSize(20, self.sizeHint().height())
        return self.sizeHint()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setFont(self._font)
        p.setPen(resolve(self._color))
        rect = QRectF(0, 0, self.width(), self.height())
        text = self._text
        if self._elide:
            fm = QFontMetrics(self._font)
            text = fm.elidedText(text, Qt.TextElideMode.ElideRight, self.width())
        p.drawText(rect, self._align, text)


class MonoDot(QWidget):
    pass


# ──────────────────────────────────────────────
# 容器：双层嵌套（外槽 + 内面板）
# ──────────────────────────────────────────────

class Groove(QWidget):
    """外层浅槽，内面板坐在里面，像玻璃板压在铝槽里。"""

    def __init__(self, radius=22, pad=8, fill=None, parent=None):
        super().__init__(parent)
        self._r = radius
        self._pad = pad
        self._fill = fill
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("Groove")

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
        rounded(p, r, self._r, QColor(self._fill) if self._fill else T.c("inset"))


# ──────────────────────────────────────────────
# 控件
# ──────────────────────────────────────────────

class Card(QWidget):
    """主卡片：白面板 + 发丝边 + 环境阴影；hover 会抬 2px。"""

    _lift = 0.0

    def __init__(self, radius=18, parent=None, hover=False, flat=False,
                 border=True, bg="surface"):
        super().__init__(parent)
        self._r = radius
        self._hover = hover
        self._flat = flat
        self._border = border
        self._bg = bg
        self._lift = 0.0
        self._base = None
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        if hover:
            self.setMouseTracking(True)

    def set_bg(self, name):
        self._bg = name
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0, 0, self.width(), self.height())
        rounded(p, r, self._r, T.c(self._bg))
        if not self._flat:
            if self._border:
                c = T.c("brand", 0.30 if self._lift else 0.10)
                hair(p, r, self._r, c, 1.0)
            if self._hover:
                tint = T.c("brand", 0.045 * (self._lift / 2.0)) if self._lift else None
                if tint:
                    rounded(p, r, self._r, tint)
        if self._focus_ring:
            hair(p, r.adjusted(-2, -2, 2, 2), self._r + 2, T.c("brand", 0.45), 1.5)

    _focus_ring = False

    def enterEvent(self, e):
        if self._hover:
            self._hover_lift(2)
        super().enterEvent(e)

    def leaveEvent(self, e):
        if self._hover:
            self._hover_lift(0)
        super().leaveEvent(e)

    def _hover_lift(self, target):
        if self._base is None:
            self._base = self.pos()
        move_to(self, b"pos", self._base + QPoint(0, -target), D_QUICK, OUT_SOFT)
        self._lift = target
        self.update()


# ──────────────────────────────────────────────
# 按钮：主按钮是渐变实心，次按钮是描边浅底
# ──────────────────────────────────────────────

class _Pressable(QWidget):
    """hover 抬 2px、press 落到 1px、松开回弹；阴影同步收放。"""

    def __init__(self, height=44, radius=13, parent=None):
        super().__init__(parent)
        self._h = height
        self._r = radius
        self._lift = 0.0
        self._press = 0.0
        self._base = None
        self._anim = None
        self._enabled = True
        self.setFixedHeight(height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

    def setEnabled(self, on):
        self._enabled = bool(on)
        QWidget.setEnabled(self, on)
        self.update()

    def isEnabled(self):
        return self._enabled

    def animate_to(self, lift, press, ms=D_TAP):
        if self._anim:
            self._anim.stop()
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(dur(max(ms, 60)))
        self._anim.setEasingCurve(OUT)
        f0, p0 = self._lift, self._press
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)

        def frame(t):
            self._lift = f0 + (lift - f0) * t
            self._press = p0 + (press - p0) * t
            if self._base is not None:
                self.move(self._base + QPoint(0, -int(round(self._lift))
                                              + int(round(self._press))))
            self.update()

        self._anim.valueChanged.connect(frame)
        keep(self, self._anim)
        self._anim.start()

    def enterEvent(self, e):
        if self._enabled:
            self.animate_to(2, 0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.animate_to(0, 0)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._enabled:
            self.animate_to(1, 1.6, 60)
            self._pressed = True
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        if getattr(self, "_pressed", False) and self._enabled:
            self._pressed = False
            inside = self.rect().contains(e.position().toPoint())
            self.animate_to(2 if inside else 0, 0, D_SLOW // 2)
            if inside:
                self.clicked()
        super().mouseReleaseEvent(e)

    _pressed = False

    def clicked(self):
        pass

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Space):
            self.animate_to(1, 1.6, 60)
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Space):
            self.animate_to(0, 0, D_STD, )
            self.clicked()
        super().keyReleaseEvent(e)


class Button(_Pressable):
    def __init__(self, text, icon=None, height=44, radius=13, kind="primary",
                 parent=None, on_click=None, wide=False, tip=""):
        super().__init__(height, radius, parent)
        self._text = text
        self._icon = icon
        self._kind = kind
        self._on_click = on_click
        self._focus = 0.0
        if tip:
            attach_tip(self, tip)
        fs = f_head(int(height * 0.36), 500) if kind == "primary" \
            else f_cjk(int(height * 0.35), 400)
        self._font = fs
        w = advance(text, fs) + height * (1.5 if icon else 1.2)
        self.setFixedWidth(int(w) + (16 if icon else 0))
        if wide:
            self.setSizePolicy(QSizePolicy.Policy.Expanding,
                               QSizePolicy.Policy.Fixed)

    def setText(self, t):
        self._text = t
        w = advance(t, self._font) + self._h * (1.5 if self._icon else 1.2)
        self.setFixedWidth(int(w) + (16 if self._icon else 0))
        self.update()

    def clicked(self):
        if self._on_click:
            self._on_click()

    def sizeHint(self):
        return QSize(advance(self._text, self._font) + self._h * 2, self._h)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        r = QRectF(0, 0, self.width(), self.height())
        if self._kind == "primary":
            if self._enabled:
                g = QLinearGradient(r.topLeft(), r.bottomRight())
                g.setColorAt(0.0, T.c("brand_hi"))
                g.setColorAt(1.0, T.c("brand"))
                rounded(p, r, self._r, QBrush(g))
                p.setPen(QPen(QColor(255, 255, 255, int(46 - 26 * self._press)), 1.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(rounded_path(r.adjusted(0.5, 0.5, -0.5, -0.5),
                                        self._r - 0.5))
            else:
                rounded(p, r, self._r, T.c("brand", 0.30))
            ink = T.c("on_brand") if self._enabled else T.c("on_brand", 0.6)
        else:
            bg = "surface" if self._enabled else "inset"
            rounded(p, r, self._r, T.c(bg))
            hair(p, r, self._r, T.c("line") if not self._lift else
                 T.c("brand_mist"), 1.0)
            ink = T.c("ink") if self._enabled else T.c("ink_faint")
        p.setFont(self._font)
        cx = self.width() / 2.0
        tw = advance(self._text, self._font)
        gw = 0 if not self._icon else int(self._h * 0.52) + 8
        left = cx - (tw + gw) / 2.0
        if self._icon:
            paint_glyph(p, self._icon, ink, left + gw / 2, self._h / 2,
                        int(self._h * 0.46), 1.5)
        p.setPen(ink)
        p.drawText(QRectF(left + gw, 0, tw + 4, self._h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self._text)


class IconButton(_Pressable):
    def __init__(self, icon, tip="", size=34, on_click=None, parent=None,
                 tone="ink", filled=False):
        super().__init__(size, size // 2, parent)
        self._icon = icon
        self._tip = tip
        self._on_click = on_click
        self._tone = tone
        self._filled = filled
        self.setFixedSize(size, size)
        if tip:
            attach_tip(self, tip)

    def set_icon(self, name):
        self._icon = name
        self.update()

    def clicked(self):
        if self._on_click:
            self._on_click()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        tone = T.c(self._tone) if self._tone != "brand" else T.c("brand")
        if self._filled:
            rounded(p, r, self._r, T.c("brand") if self._lift else T.c("brand_ice"))
            tone = T.c("on_brand") if self._lift else T.c("brand")
        elif self._lift:
            rounded(p, r, self._r, T.c("inset"))
            hair(p, r, self._r, T.c("line_soft"))
        else:
            hair(p, r, self._r, T.c("line_soft"))
        paint_glyph(p, self._icon, tone, self.width() / 2, self.height() / 2,
                    int(self._h * 0.46))


# ──────────────────────────────────────────────
# 开关
# ──────────────────────────────────────────────

class Switch(_Pressable):
    _knob = 0.0

    def __init__(self, on=False, on_change=None, parent=None, w=44, h=25):
        super().__init__(h, h // 2, parent)
        self._w = w
        self._on = on
        self._on_change = on_change
        self._knob = 1.0 if on else 0.0
        self.setFixedSize(w, h)
        self.setAccessibleName("开关")

    def set_on(self, v, animate=True):
        v = bool(v)
        if v == self._on:
            return
        self._on = v
        if animate:
            self._slide()
        else:
            self._knob = 1.0 if v else 0.0
            self.update()

    def is_on(self):
        return self._on

    def _slide(self):
        a = QPropertyAnimation(self, b"knob", self)
        a.setDuration(dur(D_STD))
        a.setEasingCurve(OUT)
        a.setStartValue(self._knob)
        a.setEndValue(1.0 if self._on else 0.0)
        keep(self, a)
        a.start()

    def clicked(self):
        self._on = not self._on
        self._slide()
        if self._on_change:
            self._on_change(self._on)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        t = self._knob
        track = lerp(T.c("line"), T.c("brand"), t)
        rounded(p, r, r.height() / 2, track)
        if t > 0.02:
            g = QRectF(r)
            rounded(p, g, g.height() / 2, T.c("brand", 0.16 * t))
        d = r.height() - 6
        x = 3 + (r.width() - d - 6) * t
        knob = QRectF(r.left() + x, r.top() + 3, d, d)
        p.setPen(QPen(QColor(T.shadow[0], T.shadow[1], T.shadow[2], 40), 1))
        rounded(p, knob, d / 2, QColor(255, 255, 255))


Switch.knob = pyqtProperty(float, fget=lambda s: s._knob,
                           fset=lambda s, v: (setattr(s, "_knob", v), s.update()))


# ──────────────────────────────────────────────
# 键帽
# ──────────────────────────────────────────────

def caps_of(display):
    """'Ctrl + Alt + W' → ['Ctrl','Alt','W']"""
    if not display:
        return []
    return [t.strip() for t in display.replace("＋", "+").split("+") if t.strip()]


class Caps(QWidget):
    """一行键帽。press 会同时把每颗键帽按下去。"""

    def __init__(self, display="", size=26, parent=None, mono=False, gap=5):
        super().__init__(parent)
        self._size = size
        self._gap = gap
        self._tokens = caps_of(display)
        self._press = 0.0
        self._mono = mono
        self.setFixedHeight(int(size * 1.55))
        self._remeasure()

    def _font(self):
        px = int(self._size * 0.46)
        return f_num(px, 500) if self._mono else f_disp(px, 600)

    def _token_w(self, t):
        base = advance(t, self._font())
        if len(t) <= 1:
            return int(base + self._size * 0.74)
        if t in ("鼠标左键", "鼠标右键"):
            return int(self._size * 2.05)
        return int(base + self._size * 0.62)

    def _remeasure(self):
        w = sum(self._token_w(t) for t in self._tokens)
        w += self._gap * max(0, len(self._tokens) - 1)
        self._width = int(w) if self._tokens else 0
        self.setFixedWidth(max(1, self._width))
        self.updateGeometry()
        self.update()

    def set_display(self, display):
        self._tokens = caps_of(display)
        self._remeasure()

    def press_pulse(self, ms=200):
        v = QVariantAnimation(self)
        v.setDuration(dur(ms))
        v.setEasingCurve(OUT)
        v.setStartValue(0.0)
        v.setEndValue(1.0)
        v.valueChanged.connect(lambda t: (setattr(self, "_press",
                                                  math.sin(math.pi * t)), self.update()))
        keep(self, v)
        v.start()

    def sizeHint(self):
        return QSize(self._width, self._size)

    def paintEvent(self, _e):
        if not self._tokens:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        x = 0.0
        h = self._size
        for i, t in enumerate(self._tokens):
            w = self._token_w(t)
            r = QRectF(x, (self.height() - h) / 2.0 + 1, w, h - 2)
            keycap(p, r, t, T.c("cap_face"), T.c("cap_edge"), T.c("cap_side"),
                   T.c("cap_ink"), press=self._press, font=self._font(),
                   radius=min(7, h * 0.28))
            x += w + self._gap


# ──────────────────────────────────────────────
# 状态点 · 徽标 · 提示气泡
# ──────────────────────────────────────────────

class StatusDot(QWidget):
    """生效=实心点；等待=空心环 + 呼吸；丢失=灰环。"""

    def __init__(self, state="ok", size=9, parent=None):
        super().__init__(parent)
        self._state = state
        self._size = size
        self._t = 1.0
        self.setFixedSize(size + 6, size + 6)
        if state == "wait":
            breathe(self, 3200, 0.35, 1.0, "_t")

    def set_state(self, s):
        self._state = s
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        r = self._size / 2.0
        if self._state == "ok":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.c("ok"))
            p.drawEllipse(QPointF(cx, cy), r, r)
        elif self._state == "warn":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.c("warn", 0.35 + 0.65 * self._t))
            p.drawEllipse(QPointF(cx, cy), r, r)
        elif self._state == "wait":
            pen = QPen(T.c("warn", 0.35 + 0.55 * self._t), 1.6)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), r - 0.8, r - 0.8)
        else:
            p.setPen(QPen(T.c("ink_faint", 0.7), 1.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), r - 0.7, r - 0.7)


class Chip(QWidget):
    def __init__(self, text, tone="ink_dim", bg="inset", parent=None, font=None):
        super().__init__(parent)
        self._text = text
        self._tone = tone
        self._bg = bg
        self._font = font or f_num(TINY, 500)
        w = advance(text, self._font) + 18
        self.setFixedSize(int(w), 21)

    def setText(self, t):
        self._text = t
        self.setFixedSize(int(advance(t, self._font) + 18), 21)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        r = QRectF(0, 0, self.width(), self.height())
        rounded(p, r, r.height() / 2, T.c(self._bg))
        p.setFont(self._font)
        p.setPen(T.c(self._tone))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self._text)


class _Tip(QWidget):
    """自绘提示：Qt 自带 tooltip 是灰盒子，跟这套材质不搭。"""

    _inst = None

    def __init__(self):
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._label = ""
        self._a = 0.0
        hide_tip_timer = QTimer(self)
        hide_tip_timer.setSingleShot(True)
        hide_tip_timer.timeout.connect(self.hide)
        self._timer = hide_tip_timer

    @classmethod
    def instance(cls):
        if cls._inst is None:
            cls._inst = _Tip()
        return cls._inst

    def show_for(self, widget, text):
        if not text:
            return
        self._label = text
        f = f_cjk(BODY, 400)
        w = advance(text, f) + 22
        h = 30
        self.setFixedSize(w, h)
        geo = widget.mapToGlobal(QPoint(widget.width() // 2, 0))
        self.move(geo.x() - w // 2, geo.y() - h - 8)
        self.show()
        self._a = 0.0
        fade_in(self, D_QUICK, 0.0, 1.0)
        self._timer.start(2600)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        rounded(p, r, 9, T.c("rail_1", 0.97))
        hair(p, r, 9, T.c("rail_line", 0.10))
        p.setFont(f_cjk(BODY, 400))
        p.setPen(T.c("rail_ink"))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self._label)


def attach_tip(widget, text, delay=420):
    widget._tip = text
    widget._tip_delay = delay
    if not getattr(widget, "_tip_filter", None):
        f = _TipFilter(widget)
        widget._tip_filter = f
        widget.installEventFilter(f)


class _TipFilter(QObject):
    def eventFilter(self, obj, e):
        t = e.type()
        if t == QEvent.Type.Enter:
            obj._tip_job = QTimer(obj)
            obj._tip_job.setSingleShot(True)
            obj._tip_job.timeout.connect(
                lambda o=obj: _Tip.instance().show_for(o, getattr(o, "_tip", "")))
            obj._tip_job.start(getattr(obj, "_tip_delay", 420))
        elif t == QEvent.Type.Leave:
            job = getattr(obj, "_tip_job", None)
            if job:
                job.stop()
            _Tip.instance().hide()
        elif t == QEvent.Type.MouseButtonPress:
            _Tip.instance().hide()
        return False


# ──────────────────────────────────────────────
# Toast
# ──────────────────────────────────────────────

class Toast(QWidget):
    KIND = {"ok": ("check", "ok"), "warn": ("bolt", "warn"),
            "bad": ("close", "bad"), "info": ("dot", "brand")}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._title = ""
        self._sub = ""
        self._kind = "ok"
        self._h1 = f_head(SUB, 500)
        self._h2 = f_cjk(BODY, 400)
        self.hide()

    def post(self, kind, title, sub=""):
        self._kind, self._title, self._sub = kind, title, sub
        w = max(advance(title, self._h1), advance(sub, self._h2)) + 86
        self.resize(int(max(w, 260)), 60 if sub else 44)
        host = self.parent()
        self._home = QPoint(host.width() - self.width() - 22,
                            host.height() - self.height() - 22)
        self.move(self._home.x(), self._home.y() + 14)
        self.show()
        self.raise_()
        # 提示条要的是投影，不是淡入：一个控件只能挂一个 effect
        set_shadow(self, 26, 8, 0.20)
        move_to(self, b"pos", self._home, D_STD, OUT)
        QTimer.singleShot(2600, self._bye)

    def _bye(self):
        a = move_to(self, b"pos", self._home + QPoint(0, 12), D_QUICK, IN)
        a.finished.connect(self.hide)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        rounded(p, r, 14, T.c("surface", 0.99))
        hair(p, r, 14, T.c("line"))
        icon, tone = self.KIND[self._kind]
        badge = QRectF(12, (self.height() - 24) / 2, 24, 24)
        rounded(p, badge, 8, T.c(tone, 0.14))
        paint_glyph(p, icon, T.c(tone), badge.center().x(), badge.center().y(), 14)
        x = 46
        if self._sub:
            p.setFont(self._h1)
            p.setPen(T.c("ink"))
            p.drawText(QRectF(x, 9, self.width() - x - 14, 20),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       self._title)
            p.setFont(self._h2)
            p.setPen(T.c("ink_dim"))
            p.drawText(QRectF(x, 29, self.width() - x - 14, 18),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       self._sub)
        else:
            p.setFont(self._h1)
            p.setPen(T.c("ink"))
            p.drawText(QRectF(x, 0, self.width() - x - 14, self.height()),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       self._title)
