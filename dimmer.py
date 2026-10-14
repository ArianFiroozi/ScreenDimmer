"""
Screen Dimmer - dims the screen below the hardware minimum brightness (Windows).

How it works: a black, click-through, always-on-top, semi-transparent window
is placed over every monitor. Mouse/keyboard input passes straight through it.

Install:  pip install PySide6
Run:      pythonw screen_dimmer.py      (or python screen_dimmer.py)

Global hotkeys:
    Ctrl+Alt+Up    less dim
    Ctrl+Alt+Down  more dim
    Ctrl+Alt+D     toggle dimming on/off
"""
import sys
import ctypes
from ctypes import wintypes

from PySide6.QtCore import (QAbstractNativeEventFilter, QSettings, Qt, QTimer)
from PySide6.QtGui import (QAction, QColor, QIcon, QPainter, QPixmap)
from PySide6.QtWidgets import (QApplication, QCheckBox, QHBoxLayout, QLabel,
                               QMenu, QPushButton, QSlider, QSystemTrayIcon,
                               QVBoxLayout, QWidget)

IS_WIN = sys.platform == "win32"
MAX_DIM = 90          # never allow a completely black screen
STEP = 5
APP_NAME = "ScreenDimmer"

# ---------------------------------------------------------------- Win32 bits
if IS_WIN:
    user32 = ctypes.windll.user32
    WM_HOTKEY = 0x0312
    MOD_ALT, MOD_CONTROL = 0x0001, 0x0002
    VK_UP, VK_DOWN, VK_D = 0x26, 0x28, 0x44
    HWND_TOPMOST = -1
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010

    def force_topmost(hwnd: int) -> None:
        user32.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(HWND_TOPMOST),
                            0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)

    def set_autostart(enabled: bool) -> None:
        import os
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE)
        try:
            if enabled:
                exe = sys.executable
                pyw = exe.replace("python.exe", "pythonw.exe")
                if os.path.exists(pyw):
                    exe = pyw
                cmd = f'"{exe}" "{os.path.abspath(sys.argv[0])}"'
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
else:
    def force_topmost(hwnd: int) -> None:
        pass

    def set_autostart(enabled: bool) -> None:
        pass


class HotkeyFilter(QAbstractNativeEventFilter):
    """Receives WM_HOTKEY messages and maps them to callbacks."""

    def __init__(self):
        super().__init__()
        self.callbacks = {}
        self._next_id = 1

    def register(self, modifiers: int, vk: int, callback) -> None:
        if not IS_WIN:
            return
        hk_id = self._next_id
        self._next_id += 1
        if user32.RegisterHotKey(None, hk_id, modifiers, vk):
            self.callbacks[hk_id] = callback

    def unregister_all(self) -> None:
        if not IS_WIN:
            return
        for hk_id in self.callbacks:
            user32.UnregisterHotKey(None, hk_id)
        self.callbacks.clear()

    def nativeEventFilter(self, event_type, message):
        if IS_WIN and event_type == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam in self.callbacks:
                self.callbacks[msg.wParam]()
                return True, 0
        return False, 0


# ------------------------------------------------------------------ Overlay
class Overlay(QWidget):
    """One click-through dark window covering a single monitor."""

    def __init__(self, screen):
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        self._color = QColor(0, 0, 0)
        self.setGeometry(screen.geometry())

    def set_look(self, opacity: float, warm: bool) -> None:
        self._color = QColor(30, 10, 0) if warm else QColor(0, 0, 0)
        self.setWindowOpacity(opacity)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.fillRect(self.rect(), self._color)

    def keep_on_top(self) -> None:
        force_topmost(int(self.winId()))


# --------------------------------------------------------------- Controller
class Dimmer(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("ScreenDimmer", "ScreenDimmer")
        self.level = min(int(self.settings.value("level", 40)), MAX_DIM)
        self.enabled = self.settings.value("enabled", True, type=bool)
        self.warm = self.settings.value("warm", False, type=bool)
        self.overlays = []

        self._build_ui()
        self._build_tray()
        self._rebuild_overlays()

        app = QApplication.instance()
        app.screenAdded.connect(lambda _s: self._rebuild_overlays())
        app.screenRemoved.connect(lambda _s: self._rebuild_overlays())

        # Re-assert topmost now and then (taskbar, fullscreen apps, etc.)
        self.keepalive = QTimer(self)
        self.keepalive.timeout.connect(self._keep_on_top)
        self.keepalive.start(2000)

    # ---- UI
    def _build_ui(self):
        self.setWindowTitle("Screen Dimmer")
        self.setWindowIcon(self._make_icon())
        self.setFixedWidth(320)

        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, MAX_DIM)
        self.slider.setValue(self.level)
        self.slider.valueChanged.connect(self.set_level)

        self.chk_enabled = QCheckBox("Dimming enabled")
        self.chk_enabled.setChecked(self.enabled)
        self.chk_enabled.toggled.connect(self.set_enabled)

        self.chk_warm = QCheckBox("Warm tint (reduces blue light)")
        self.chk_warm.setChecked(self.warm)
        self.chk_warm.toggled.connect(self.set_warm)

        self.chk_auto = QCheckBox("Start with Windows")
        self.chk_auto.setChecked(self.settings.value("autostart", False, type=bool))
        self.chk_auto.toggled.connect(self.set_autostart)
        self.chk_auto.setEnabled(IS_WIN)

        btn_hide = QPushButton("Hide to tray")
        btn_hide.clicked.connect(self.hide)
        btn_quit = QPushButton("Quit")
        btn_quit.clicked.connect(self.quit)
        row = QHBoxLayout()
        row.addWidget(btn_hide)
        row.addWidget(btn_quit)

        hint = QLabel("Hotkeys: Ctrl+Alt+Up / Down / D")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("color: gray;")

        lay = QVBoxLayout(self)
        for w in (self.label, self.slider, self.chk_enabled, self.chk_warm,
                  self.chk_auto):
            lay.addWidget(w)
        lay.addLayout(row)
        lay.addWidget(hint)
        self._update_label()

    @staticmethod
    def _make_icon() -> QIcon:
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QColor(60, 60, 60))
        p.setBrush(QColor(245, 245, 245))
        p.drawEllipse(6, 6, 52, 52)
        p.setBrush(QColor(40, 40, 40))
        p.setPen(Qt.NoPen)
        p.drawPie(6, 6, 52, 52, 90 * 16, 180 * 16)   # left half dark
        p.end()
        return QIcon(pm)

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self._make_icon(), self)
        self.tray.setToolTip("Screen Dimmer")
        menu = QMenu()
        act_show = QAction("Settings...", menu, triggered=self.show_panel)
        self.act_toggle = QAction("Dimming enabled", menu, checkable=True)
        self.act_toggle.setChecked(self.enabled)
        self.act_toggle.toggled.connect(self.set_enabled)
        act_quit = QAction("Quit", menu, triggered=self.quit)
        menu.addAction(act_show)
        menu.addAction(self.act_toggle)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.show_panel()
            if r in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self._tray_menu = menu          # keep a reference
        self.tray.show()

    # ---- overlays
    def _rebuild_overlays(self):
        for o in self.overlays:
            o.close()
            o.deleteLater()
        self.overlays = [Overlay(s) for s in QApplication.screens()]
        for o in self.overlays:
            o.show()
        self._apply()

    def _apply(self):
        opacity = self.level / 100.0
        for o in self.overlays:
            o.set_look(opacity, self.warm)
            o.setVisible(self.enabled and (self.level > 0 or self.warm))
            if o.isVisible():
                o.keep_on_top()

    def _keep_on_top(self):
        if self.enabled:
            for o in self.overlays:
                if o.isVisible():
                    o.keep_on_top()

    def _update_label(self):
        self.label.setText(f"Extra dimming: {self.level}%")

    # ---- actions
    def set_level(self, value: int):
        self.level = max(0, min(MAX_DIM, int(value)))
        if self.slider.value() != self.level:
            self.slider.blockSignals(True)
            self.slider.setValue(self.level)
            self.slider.blockSignals(False)
        self.settings.setValue("level", self.level)
        self._update_label()
        self._apply()

    def adjust(self, delta: int):
        if not self.enabled:
            self.set_enabled(True)
        self.set_level(self.level + delta)

    def set_enabled(self, value: bool):
        self.enabled = bool(value)
        for w, sig in ((self.chk_enabled, None), (self.act_toggle, None)):
            if w.isChecked() != self.enabled:
                w.blockSignals(True)
                w.setChecked(self.enabled)
                w.blockSignals(False)
        self.settings.setValue("enabled", self.enabled)
        self._apply()

    def toggle(self):
        self.set_enabled(not self.enabled)

    def set_warm(self, value: bool):
        self.warm = bool(value)
        self.settings.setValue("warm", self.warm)
        self._apply()

    def set_autostart(self, value: bool):
        self.settings.setValue("autostart", bool(value))
        try:
            set_autostart(bool(value))
        except Exception as exc:                      # noqa: BLE001
            self.tray.showMessage("Screen Dimmer", f"Autostart failed: {exc}")

    def show_panel(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):       # close button -> hide to tray
        event.ignore()
        self.hide()

    def quit(self):
        QApplication.instance().quit()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Screen Dimmer")

    dimmer = Dimmer()

    hotkeys = HotkeyFilter()
    app.installNativeEventFilter(hotkeys)
    if IS_WIN:
        mods = MOD_CONTROL | MOD_ALT
        hotkeys.register(mods, VK_UP, lambda: dimmer.adjust(-STEP))
        hotkeys.register(mods, VK_DOWN, lambda: dimmer.adjust(+STEP))
        hotkeys.register(mods, VK_D, dimmer.toggle)
    app.aboutToQuit.connect(hotkeys.unregister_all)

    dimmer.show_panel()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()