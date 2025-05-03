# src/fatfloppy/gui/themes.py


def get_dark_theme():
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
        min-height: 25px;
        max-height: 30px;
    }
    QDockWidget {
        background-color: #2b2b2b;
        color: #ffffff;
    }
    QTreeWidget, QPlainTextEdit, QLabel {
        background-color: #3c3c3c;
        color: #ffffff;
    }
    /* Add more widget styles as needed */
    """

def get_light_theme():
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
        min-height: 25px;
        max-height: 30px;
    }
    QDockWidget {
        background-color: #ffffff;
        color: #000000;
    }
    QTreeWidget, QPlainTextEdit, QLabel {
        background-color: #ffffff;
        color: #000000;
    }
    /* Add more widget styles as needed */
    """
