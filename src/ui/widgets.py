"""Small shared widgets."""
from PyQt5 import QtCore, QtWidgets


class ThemedCombo(QtWidgets.QComboBox):
    """Combo box whose popup is a rounded, flat list matching the theme."""

    def __init__(self):
        super().__init__()
        view = QtWidgets.QListView()
        view.setObjectName('comboList')
        self.setView(view)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        popup = view.window()
        popup.setWindowFlags(QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint | QtCore.Qt.NoDropShadowWindowHint)
        popup.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setMaxVisibleItems(14)

    def showPopup(self):
        super().showPopup()
        popup = self.view().window()
        popup.move(popup.x(), self.mapToGlobal(QtCore.QPoint(0, self.height() + 4)).y())
