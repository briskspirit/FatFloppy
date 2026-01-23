# src/fatfloppy/gui/themes.py
"""
Provides stylesheet strings for theming the PyQt6 application.

This module contains functions that return Qt StyleSheet (QSS) strings
for dark and light application themes. These can be applied to the main
QApplication instance to control the look and feel of the GUI.
"""


def get_dark_theme() -> str:
    """
    Returns a QSS string for a dark application theme.

    The theme uses dark backgrounds with light text, suitable for
    low-light environments or user preference.

    Returns:
        A string containing the dark theme Qt StyleSheet.
    """
    return """
    QMainWindow {
        background-color: #2b2b2b;
        color: #ffffff;
    }
    QMenuBar {
        background-color: #1e1e1e;
        color: #ffffff;
        min-height: 20px;
        max-height: 25px;
    }
    QMenu {
        background-color: #1e1e1e;
        color: #ffffff;
        padding: 5px;
    }
    QToolBar {
        background-color: #1e1e1e;
        color: #ffffff;
        spacing: 5px;
    }
    QDockWidget {
        background-color: #2b2b2b;
        color: #ffffff;
    }
    QTreeWidget, QPlainTextEdit, QLabel {
        background-color: #3c3c3c;
        color: #ffffff;
    }
    """


def get_light_theme() -> str:
    """
    Returns a QSS string for a light application theme.

    The theme uses light backgrounds with dark text, following a more
    traditional desktop application appearance.

    Returns:
        A string containing the light theme Qt StyleSheet.
    """
    return """
    QMainWindow {
        background-color: #ffffff;
        color: #000000;
    }
    QMenuBar {
        background-color: #f0f0f0;
        color: #000000;
        min-height: 20px;
        max-height: 25px;
    }
    QMenu {
        background-color: #f0f0f0;
        color: #000000;
        padding: 5px;
    }
    QToolBar {
        background-color: #f0f0f0;
        color: #000000;
        spacing: 5px;
    }
    QDockWidget {
        background-color: #ffffff;
        color: #000000;
    }
    QTreeWidget, QPlainTextEdit, QLabel {
        background-color: #ffffff;
        color: #000000;
    }
    """
