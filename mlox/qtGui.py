#!/usr/bin/python3
import io
import logging
import re
import sys
import tempfile
import traceback
from argparse import Namespace
import urllib.request

from PyQt5.QtCore import QUrl, QObject, pyqtSignal, pyqtSlot, QSize
from PyQt5.QtGui import QImage, QIcon, QPixmap
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtQuick import QQuickImageProvider
from PyQt5.QtWidgets import QApplication, QDialog, QPlainTextEdit, QMessageBox, QProgressDialog

from mlox import version
from mlox.loadOrder import Loadorder
from mlox.resources import resource_manager

gui_logger = logging.getLogger('mlox.gui')


def colorize_text(text):
    """
    Some things are better in color.
    This function takes normal text, and applies html style tags where appropriate.
    """
    bg_colors = {
        "low": "<span style='background-color: rgb(255,180,180);'>\g<0></span>",
        "medium": "<span style='background-color: rgb(255,255,180);'>\g<0></span>",
        "high": "<span style='background-color: rgb(125,220,240);'>\g<0></span>",
        "green": "<span style='background-color: rgb(80,200,120);'>\g<0></span>",
        "yellow": "<span style='background-color: yellow;'>\g<0></span>",
        "red": "<span style='background-color: rgb(238,75,43);'>\g<0></span>"
    }

    highlighters = [
        (re.compile(r'<hide>(.*)</hide>'), "<span style='color: black; background-color: black;'>\g<1></span>"),

        # General log highlighting
        (re.compile(r'^(SUCCESS:.*)', re.MULTILINE), bg_colors["green"]),

        # Spoilers require Highlighting
        (re.compile(r'^(\[CONFLICT])', re.MULTILINE), bg_colors["red"]),
        (re.compile(r'(https?://[^\s]*)', re.IGNORECASE), "<a href='\g<0>'>\g<0></a>"),  # URLs
        (re.compile(r"^(\s*\|?\s*![^!].*)$", re.MULTILINE), bg_colors["low"]),  # '!' in mlox_base.txt
        (re.compile(r"^(\s*\|?\s*!{2}.*)$", re.MULTILINE), bg_colors["medium"]),  # '!!' in mlox_base.txt
        (re.compile(r"^(\s*\|?\s*!{3}.*)$", re.MULTILINE), bg_colors["high"]),  # '!!!' in mlox_base.txt
        (re.compile(r'^(WARNING:.*)', re.MULTILINE), bg_colors["yellow"]),
        (re.compile(r'^(ERROR:.*)', re.MULTILINE), bg_colors["red"]),
        (re.compile(r'(\[Plugins already in sorted order. No sorting needed!])', re.IGNORECASE), bg_colors["green"]),
        (re.compile(r'^(\*\d+\*\s.*\.(?i)es[mp])', re.MULTILINE), bg_colors["yellow"]),  # Changed mod order
        (re.compile(r'^(\*!\d+\*!\s.*\.(?i)es[mp])', re.MULTILINE), bg_colors["red"])  # Changed mod order
    ]
    for (regex, replacement_string) in highlighters:
        text = regex.sub(replacement_string, text)

    text = text.replace('\n', '<br>\n')
    return text


class PkgResourcesImageProvider(QQuickImageProvider):
    """
    Load an appropriate image from mlox.static

    Props to https://stackoverflow.com/a/47504480/11521987
    """

    #
    def __init__(self):
        super().__init__(QQuickImageProvider.Image)

    def requestImage(self, p_str, size: QSize):
        image_data: bytes = resource_manager.resource_string("mlox.static", p_str)
        image = QImage()
        image.loadFromData(image_data)
        return image, image.size()


class ScrollableDialog(QDialog):
    """A dialog box that contains scrollable text."""

    def __init__(self):
        QDialog.__init__(self)
        self.setModal(False)

        self.inner_text = QPlainTextEdit(self)
        self.inner_text.setReadOnly(True)

        self.setFixedSize(400, 600)
        self.inner_text.setFixedSize(400, 600)

    def set_text(self, new_text):
        self.inner_text.setPlainText(new_text)


class CustomProgressDialog(QProgressDialog):
    """
    A custom version of the progress dialog
    It's designed to have the same update mechanism as a wx.ProgressDialog
    """

    def __init__(self):
        QProgressDialog.__init__(self)
        self.setCancelButton(None)
        self.forceShow()
        self.open()

    def update_value_and_label(self, percent, label):
        self.setLabelText(label)
        self.setValue(percent)


def error_handler(typ, value, tb):
    """
    Since a command line is not normally available to a GUI application, we need to display errors to the user.
    These are only errors that would cause the program to crash, so have the program exit when the dialog box is closed.
    """
    error_box = ScrollableDialog()
    error_box.set_text(version.version_info() + "\n" + "".join(traceback.format_exception(typ, value, tb)))
    error_box.exec_()
    sys.exit(1)


class MloxGui(QObject):
    """Mlox's GUI (Using PyQt5)"""

    lo = None  # Load order

    # Signals (use emit(...) to change values)
    enable_updateButton = pyqtSignal(bool, arguments=['is_enabled'])
    set_status = pyqtSignal(str, arguments=['text'])
    set_message = pyqtSignal(str, arguments=['text'])
    set_new = pyqtSignal(str, arguments=['text'])
    set_old = pyqtSignal(str, arguments=['text'])

    def __init__(self):
        QObject.__init__(self)
        self.Dbg = io.StringIO()  # debug output
        self.Stats = io.StringIO()  # status output
        self.New = ""  # new sorted loadorder
        self.Old = ""  # old original loadorder
        self.Msg = ""  # messages output
        self.can_update = True  # If the load order can be saved or not

        # Set up logging
        dbg_formatter = logging.Formatter('%(levelname)s (%(name)s): %(message)s')
        dbg_log_stream = logging.StreamHandler(stream=self.Dbg)
        dbg_log_stream.setFormatter(dbg_formatter)
        dbg_log_stream.setLevel(logging.DEBUG)
        logging.getLogger('').addHandler(dbg_log_stream)
        gui_formatter = logging.Formatter('%(levelname)s: %(message)s')
        gui_log_stream = logging.StreamHandler(stream=self.Stats)
        gui_log_stream.setFormatter(gui_formatter)
        gui_log_stream.setLevel(logging.WARNING)
        logging.getLogger('').addHandler(gui_log_stream)

        # This is a little cheat so the INFO messages still display, but without the tag
        class FilterInfo:
            @staticmethod
            def filter(record):
                return record.levelno == logging.INFO

        info_formatter = logging.Formatter('%(message)s')
        gui_info_stream = logging.StreamHandler(stream=self.Stats)
        gui_info_stream.setFormatter(info_formatter)
        gui_info_stream.setLevel(logging.INFO)
        gui_info_stream.addFilter(FilterInfo())
        logging.getLogger('').addHandler(gui_info_stream)

    def start(self, args: Namespace):
        """Display the GUI"""
        my_app = QApplication(sys.argv)
        sys.excepthook = lambda typ, val, tb: error_handler(typ, val, tb)

        my_app.setOrganizationDomain('mlox')
        my_app.setOrganizationName('mlox')

        icon_data: bytes = resource_manager.resource_string("mlox.static", "mlox.ico")
        icon = QIcon()
        pixmap = QPixmap()
        pixmap.loadFromData(icon_data)
        icon.addPixmap(pixmap)
        my_app.setWindowIcon(icon)

        my_engine = QQmlApplicationEngine()
        # Need to set these before loading
        my_engine.rootContext().setContextProperty("python", self)
        my_engine.addImageProvider('static', PkgResourcesImageProvider())

        qml: bytes = resource_manager.resource_string("mlox.static", "window.qml")
        my_engine.loadData(qml)

        # These two are hacks, because getting them in the __init__ and RAII working isn't
        self.debug_window = ScrollableDialog()
        self.clipboard = my_app.clipboard()

        self.analyze_loadorder()

        sys.exit(my_app.exec())

    def display(self):
        """Update the GUI after an operation"""
        self.debug_window.set_text(self.Dbg.getvalue())
        self.enable_updateButton.emit(self.can_update)
        self.set_status.emit(colorize_text(self.Stats.getvalue()))
        self.set_message.emit(colorize_text(self.Msg))
        self.set_new.emit(colorize_text(self.New))
        self.set_old.emit(colorize_text(self.Old))

    def analyze_loadorder(self, fromfile=None):
        """
        This is where the magic happens
        If fromfile is None, then it operates out of the current directory.
        """

        # Clear all the outputs (except Dbg)
        self.Stats.truncate(0)
        self.Stats.seek(0)
        self.New = ""
        self.Old = ""
        self.Msg = ""

        current_version = version.VERSION
        gui_logger.info("Version: %s\t\t\t\t %s " % (current_version, "Hello!"))

        # check for update
        url = "https://github.com/rfuzzo/mlox/releases/latest"
        try:
            connection = urllib.request.urlopen(url)
            remote_url: str = connection.url
            remote_version = remote_url.split('/')[-1]
            if remote_version != current_version:
                gui_logger.warning(f"MLOX Update available: {current_version} -> {remote_version}. Link: {remote_url}")
        except Exception as e:
            gui_logger.warning('Unable to connect to {0}, skipping update check.'.format(url))

        self.lo = Loadorder()
        if fromfile is not None:
            self.lo.read_from_file(fromfile)
        else:
            self.lo.get_active_plugins()

        progress = CustomProgressDialog()
        self.Msg = self.lo.update(progress, False)

        for p in self.lo.get_original_order():
            self.Old += p + '\n'
        for p in self.lo.get_new_order():
            self.New += p + '\n'
        if self.lo.is_sorted:
            self.can_update = False

        # Go ahead and display everything
        self.display()

    @pyqtSlot()
    def show_debug_window(self):
        """
        Updates the text of the debug window, then shows it.
        Note:  The debug window is also updated every time `self.display()` is called
        """
        self.debug_window.set_text(self.Dbg.getvalue())
        self.debug_window.open()

    @pyqtSlot()
    def paste_handler(self):
        """Open a load order from the clipboard"""
        file_handle = tempfile.NamedTemporaryFile()
        file_handle.write(self.clipboard.text().encode('utf8'))
        file_handle.seek(0)
        self.analyze_loadorder(file_handle.name)

    @pyqtSlot(str)
    def open_file(self, file_path):
        """Analyze the file passed in"""
        file_path = QUrl(file_path).path()  # Adjust from a file:// format to a regular path
        self.analyze_loadorder(file_path)

    @pyqtSlot()
    def reload(self):
        self.can_update = True
        # TODO:  Properly handle reloading from a file
        self.analyze_loadorder()

    @pyqtSlot()
    def commit(self):
        """Write the requested changes to the file/directory"""
        if not self.can_update:
            gui_logger.error("Attempted an update, when no update is possible/needed.")
            self.display()
            return
        self.lo.write_new_order()
        gui_logger.info("SUCCESS: LOAD ORDER UPDATED!")
        self.can_update = False
        self.display()

    @pyqtSlot()
    def about_handler(self):
        """
        Show information about mlox
        """
        about_box = QMessageBox()
        about_box.setText(version.about())
        about_box.exec_()
