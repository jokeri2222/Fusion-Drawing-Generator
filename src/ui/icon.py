"""The application icon, drawn with QPainter (no image assets to ship).

A blue rounded tile holding a white drawing sheet: a part outline with a
dimension line and arrowheads - what the program produces.
"""
from PyQt5 import QtCore, QtGui

SIZES = (16, 24, 32, 48, 64, 128, 256)


def _arrow(p, tip, direction, size):
    d = QtCore.QPointF(*direction)
    n = QtCore.QPointF(-d.y(), d.x())
    base = tip - d * size
    p.drawPolygon(QtGui.QPolygonF([tip, base + n * size * 0.45, base - n * size * 0.45]))


def render(size=256):
    """The icon as a QImage of `size` x `size` pixels."""
    img = QtGui.QImage(size, size, QtGui.QImage.Format_ARGB32)
    img.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(img)
    p.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.SmoothPixmapTransform)
    p.scale(size / 256.0, size / 256.0)

    tile = QtGui.QLinearGradient(0, 0, 0, 256)
    tile.setColorAt(0, QtGui.QColor('#3b82f6'))
    tile.setColorAt(1, QtGui.QColor('#1e40af'))
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(tile)
    p.drawRoundedRect(QtCore.QRectF(8, 8, 240, 240), 52, 52)

    # sheet with a folded corner
    sheet = QtGui.QPolygonF([QtCore.QPointF(52, 40), QtCore.QPointF(168, 40), QtCore.QPointF(204, 76),
                             QtCore.QPointF(204, 216), QtCore.QPointF(52, 216)])
    p.setBrush(QtGui.QColor('#ffffff'))
    p.drawPolygon(sheet)
    p.setBrush(QtGui.QColor('#c7d6f5'))
    p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(168, 40), QtCore.QPointF(168, 76), QtCore.QPointF(204, 76)]))

    # the part: an L-shaped bracket with a hole
    ink = QtGui.QColor('#1e3a8a')
    pen = QtGui.QPen(ink, 7, QtCore.Qt.SolidLine, QtCore.Qt.SquareCap, QtCore.Qt.MiterJoin)
    p.setPen(pen)
    p.setBrush(QtGui.QColor('#dbe7ff'))
    p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(76, 78), QtCore.QPointF(150, 78), QtCore.QPointF(150, 124),
                                   QtCore.QPointF(180, 124), QtCore.QPointF(180, 162), QtCore.QPointF(76, 162)]))
    p.setBrush(QtCore.Qt.NoBrush)
    p.drawEllipse(QtCore.QPointF(112, 120), 15, 15)

    # dimension line with arrowheads (orange)
    accent = QtGui.QColor('#f97316')
    p.setPen(QtGui.QPen(accent, 6, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
    y = 190
    p.drawLine(QtCore.QPointF(76, 166), QtCore.QPointF(76, y + 8))
    p.drawLine(QtCore.QPointF(180, 166), QtCore.QPointF(180, y + 8))
    p.drawLine(QtCore.QPointF(86, y), QtCore.QPointF(170, y))
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(accent)
    _arrow(p, QtCore.QPointF(76, y), (-1, 0), 18)
    _arrow(p, QtCore.QPointF(180, y), (1, 0), 18)
    p.end()
    return img


def app_icon():
    icon = QtGui.QIcon()
    for s in SIZES:
        icon.addPixmap(QtGui.QPixmap.fromImage(render(s)))
    return icon


def save_files(folder):
    """Write icon.png (256 px) and icon.ico (all sizes) into `folder`."""
    import os
    os.makedirs(folder, exist_ok=True)
    render(256).save(os.path.join(folder, 'icon.png'))
    # Qt's ICO writer stores one image; Pillow packs the whole size ladder.
    try:
        from PIL import Image
        base = Image.open(os.path.join(folder, 'icon.png'))
        base.save(os.path.join(folder, 'icon.ico'), sizes=[(s, s) for s in SIZES if s <= 256])
    except Exception:
        render(256).save(os.path.join(folder, 'icon.ico'))
