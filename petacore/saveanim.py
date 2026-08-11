"""The little floppy disk that flies up the screen when Ctrl+Esc saves a file.

It starts small near the bottom, grows as it reaches the middle, then shrinks
and fades as it leaves the top. The drawing itself is plain vector maths so
the exact same shape is used by the GTK (Cairo) and Qt (QPainter) builds.

Timing helpers return, for a progress value p in 0..1:
    y_fraction   where the disk sits vertically (1 = bottom, 0 = top)
    scale        size multiplier
    alpha        opacity
"""

DURATION_MS = 700
BASE_SIZE = 96          # size in px at scale 1.0


def y_fraction(p: float) -> float:
    """Bottom (1.05) to above the top (-0.15), moving steadily upward."""
    return 1.05 - 1.2 * p


def scale(p: float) -> float:
    """Smallest at the ends, biggest exactly in the middle."""
    return 0.55 + 0.75 * (1.0 - abs(p - 0.5) * 2.0)


def alpha(p: float) -> float:
    """Fade in quickly, hold, fade out towards the top."""
    fade_in = min(1.0, p / 0.15) if p < 0.15 else 1.0
    fade_out = min(1.0, (1.0 - p) / 0.25) if p > 0.75 else 1.0
    return max(0.0, min(1.0, fade_in * fade_out))


# Shape description, expressed as fractions of the disk size so both
# toolkits can draw it identically.
CORNER = 0.08
CUT = 0.18                      # cut corner, bottom-right (real 3.5" disk)
SHUTTER = (0.28, 0.06, 0.44, 0.30)      # x, y, w, h
SHUTTER_SLOT = (0.55, 0.09, 0.12, 0.24)
LABEL = (0.18, 0.46, 0.64, 0.40)
LABEL_LINES = 3
LABEL_LINE = (0.24, 0.54, 0.52, 0.045)   # x, y0, w, h
LABEL_LINE_GAP = 0.09

BODY_RGB = (0.16, 0.18, 0.22)
SHUTTER_RGB = (0.75, 0.78, 0.82)
LABEL_RGB = (0.95, 0.95, 0.93)
LINE_RGB = (0.55, 0.58, 0.62)


def draw_cairo(cr, cx, cy, size, opacity):
    """Draw the floppy centred on (cx, cy) with a Cairo context (GTK build)."""
    import math
    s = size
    cr.save()
    cr.translate(cx - s / 2, cy - s / 2)
    r = s * CORNER

    cr.set_source_rgba(*BODY_RGB, opacity)
    cr.new_path()
    cr.arc(r, r, r, math.pi, 1.5 * math.pi)
    cr.line_to(s - r, 0)
    cr.arc(s - r, r, r, 1.5 * math.pi, 0)
    cr.line_to(s, s - s * CUT)
    cr.line_to(s - s * CUT, s)
    cr.line_to(r, s)
    cr.arc(r, s - r, r, 0.5 * math.pi, math.pi)
    cr.close_path()
    cr.fill()

    cr.set_source_rgba(*SHUTTER_RGB, opacity)
    cr.rectangle(s * SHUTTER[0], s * SHUTTER[1], s * SHUTTER[2], s * SHUTTER[3])
    cr.fill()
    cr.set_source_rgba(*BODY_RGB, opacity)
    cr.rectangle(s * SHUTTER_SLOT[0], s * SHUTTER_SLOT[1],
                 s * SHUTTER_SLOT[2], s * SHUTTER_SLOT[3])
    cr.fill()

    cr.set_source_rgba(*LABEL_RGB, opacity)
    cr.rectangle(s * LABEL[0], s * LABEL[1], s * LABEL[2], s * LABEL[3])
    cr.fill()
    cr.set_source_rgba(*LINE_RGB, opacity)
    for i in range(LABEL_LINES):
        cr.rectangle(s * LABEL_LINE[0],
                     s * (LABEL_LINE[1] + i * LABEL_LINE_GAP),
                     s * LABEL_LINE[2], s * LABEL_LINE[3])
        cr.fill()
    cr.restore()


def draw_qt(painter, cx, cy, size, opacity):
    """Draw the same floppy with QPainter (Qt build)."""
    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QColor, QPainterPath

    def col(rgb):
        c = QColor.fromRgbF(*rgb)
        c.setAlphaF(opacity)
        return c

    s = size
    x0, y0 = cx - s / 2, cy - s / 2
    r = s * CORNER

    path = QPainterPath()
    path.moveTo(x0 + r, y0)
    path.lineTo(x0 + s - r, y0)
    path.quadTo(QPointF(x0 + s, y0), QPointF(x0 + s, y0 + r))
    path.lineTo(x0 + s, y0 + s - s * CUT)
    path.lineTo(x0 + s - s * CUT, y0 + s)
    path.lineTo(x0 + r, y0 + s)
    path.quadTo(QPointF(x0, y0 + s), QPointF(x0, y0 + s - r))
    path.lineTo(x0, y0 + r)
    path.quadTo(QPointF(x0, y0), QPointF(x0 + r, y0))
    painter.fillPath(path, col(BODY_RGB))

    painter.fillRect(QRectF(x0 + s * SHUTTER[0], y0 + s * SHUTTER[1],
                            s * SHUTTER[2], s * SHUTTER[3]), col(SHUTTER_RGB))
    painter.fillRect(QRectF(x0 + s * SHUTTER_SLOT[0], y0 + s * SHUTTER_SLOT[1],
                            s * SHUTTER_SLOT[2], s * SHUTTER_SLOT[3]),
                     col(BODY_RGB))
    painter.fillRect(QRectF(x0 + s * LABEL[0], y0 + s * LABEL[1],
                            s * LABEL[2], s * LABEL[3]), col(LABEL_RGB))
    for i in range(LABEL_LINES):
        painter.fillRect(
            QRectF(x0 + s * LABEL_LINE[0],
                   y0 + s * (LABEL_LINE[1] + i * LABEL_LINE_GAP),
                   s * LABEL_LINE[2], s * LABEL_LINE[3]), col(LINE_RGB))
