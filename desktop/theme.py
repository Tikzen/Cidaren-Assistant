"""界面配色与样式表。颜色从系统调色板推导，跟随浅色 / 深色主题。"""
from PyQt6.QtGui import QColor, QPalette


def _mix(a, b, t):
    """按比例 t 把颜色 a 混向 b，返回 #rrggbb。"""
    a, b = QColor(a), QColor(b)
    return QColor(round(a.red() + (b.red() - a.red()) * t),
                  round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t)).name()


def is_dark(palette):
    return palette.color(QPalette.ColorRole.Window).lightness() < 128


def tokens(palette):
    dark = is_dark(palette)
    role = QPalette.ColorRole
    window = palette.color(role.Window).name()
    base = palette.color(role.Base).name()
    text = palette.color(role.WindowText).name()
    accent = palette.color(role.Highlight).name()
    accent_text = palette.color(role.HighlightedText).name()
    status = ({'blue': '#6ea8ff', 'green': '#5cc98a', 'amber': '#e7b35a', 'red': '#ff7b72',
               'orange': '#f2994a', 'teal': '#4fc1b6', 'gray': '#9aa0a8'} if dark else
              {'blue': accent, 'green': '#1f8a4c', 'amber': '#a86a00', 'red': '#c4352b',
               'orange': '#c25e00', 'teal': '#0e7f76', 'gray': '#6b7079'})
    return dict(
        dark=dark, window=window, base=base, text=text, accent=accent, accent_text=accent_text,
        panel=_mix(window, base, 0.65) if not dark else _mix(window, base, 0.35),
        card=base if not dark else _mix(base, text, 0.04),
        border=_mix(base, text, 0.12 if not dark else 0.14),
        border_strong=_mix(base, text, 0.22 if not dark else 0.24),
        muted=_mix(text, base, 0.42),
        faint=_mix(text, base, 0.62),
        hover=_mix(base, text, 0.05),
        pressed=_mix(base, text, 0.10),
        subtle=_mix(base, accent, 0.10 if not dark else 0.18),
        selection=_mix(base, accent, 0.16 if not dark else 0.30),
        accent_hover=_mix(accent, '#000000' if not dark else '#ffffff', 0.10),
        accent_fg=accent if not dark else _mix(accent, '#ffffff', 0.35),
        accent_disabled=_mix(base, accent, 0.30),
        track=_mix(base, text, 0.10),
        **status)


def status_colors(t):
    """队列状态 -> (文字颜色, 浅底色)。颜色只做辅助，状态始终写成文字。"""
    def pair(color):
        return color, _mix(t['card'], color, 0.16 if not t['dark'] else 0.22)
    return {
        'waiting': pair(t['gray']), 'running': pair(t['blue']), 'pausing': pair(t['amber']),
        'paused': pair(t['amber']), 'succeeded': pair(t['green']), 'skipped': pair(t['gray']),
        'failed': pair(t['red']), 'cancelled': pair(t['gray']), 'verify': pair(t['orange']),
        'uncertain': pair(t['orange']), 'partial': pair(t['teal']),
    }


def stylesheet(t, base_pt):
    small = max(9, base_pt - 1)
    return f"""
QWidget#host {{ background: {t['window']}; }}
QLabel#appTitle {{ font-size: {base_pt + 7}pt; font-weight: 600; }}
QLabel#sectionTitle {{ font-size: {base_pt + 1}pt; font-weight: 600; }}
QLabel[muted="true"] {{ color: {t['muted']}; font-size: {small}pt; }}
QLabel#summary {{ color: {t['muted']}; }}
QLabel#warnHint {{ color: {t['amber']}; font-size: {small}pt; }}

QFrame#panel {{ background: {t['panel']}; border: 1px solid {t['border']}; border-radius: 10px; }}
QFrame#detail {{ background: {t['card']}; border: 1px solid {t['border']}; border-radius: 8px; }}
QFrame#divider {{ background: {t['border']}; border: none; max-width: 1px; min-width: 1px; }}

QPushButton {{
    padding: 5px 14px; min-height: 20px; border-radius: 6px;
    border: 1px solid {t['border_strong']}; background: {t['card']}; color: {t['text']};
}}
QPushButton:hover {{ background: {t['hover']}; }}
QPushButton:pressed {{ background: {t['pressed']}; }}
QPushButton:disabled {{ color: {t['faint']}; border-color: {t['border']}; background: transparent; }}
QPushButton:focus {{ border-color: {t['accent']}; }}
QPushButton[role="primary"] {{
    background: {t['accent']}; color: {t['accent_text']}; border: 1px solid {t['accent']};
    font-weight: 600; padding: 6px 20px;
}}
QPushButton[role="primary"]:hover {{ background: {t['accent_hover']}; border-color: {t['accent_hover']}; }}
QPushButton[role="primary"]:disabled {{
    background: {t['accent_disabled']}; border-color: {t['accent_disabled']}; color: {t['accent_text']};
}}
QPushButton[role="ghost"] {{ background: transparent; border: 1px solid transparent; color: {t['accent_fg']}; padding: 5px 8px; }}
QPushButton[role="ghost"]:hover {{ background: {t['subtle']}; }}
QPushButton[role="ghost"]:disabled {{ color: {t['faint']}; background: transparent; }}
QPushButton[role="tool"] {{
    background: transparent; border: 1px solid transparent; color: {t['text']};
    padding: 3px 9px; min-height: 18px; font-size: {small}pt;
}}
QPushButton[role="tool"]:hover {{ background: {t['hover']}; border-color: {t['border']}; }}
QPushButton[role="tool"]:disabled {{ color: {t['faint']}; background: transparent; border-color: transparent; }}
QPushButton[role="danger"] {{ color: {t['red']}; background: transparent; border: 1px solid {t['red']}; }}
QPushButton[role="danger"]:hover {{ background: {_mix(t['card'], t['red'], 0.10)}; }}
QPushButton[role="danger"]:disabled {{ color: {t['faint']}; border-color: {t['border']}; }}

QLineEdit {{
    padding: 6px 10px; border: 1px solid {t['border_strong']}; border-radius: 6px;
    background: {t['base']}; color: {t['text']}; selection-background-color: {t['accent']};
}}
QLineEdit:focus {{ border-color: {t['accent']}; }}
QLineEdit:disabled {{ color: {t['faint']}; }}

QRadioButton#segment {{
    padding: 4px 14px; border: 1px solid {t['border_strong']}; background: {t['card']}; color: {t['muted']};
}}
QRadioButton#segment:checked {{ background: {t['subtle']}; color: {t['accent_fg']}; border-color: {t['accent']}; font-weight: 600; }}
QRadioButton#segment:hover:!checked {{ background: {t['hover']}; }}
QRadioButton#segment::indicator {{ width: 0px; height: 0px; image: none; }}
QRadioButton#segment[pos="left"] {{ border-top-left-radius: 6px; border-bottom-left-radius: 6px; }}
QRadioButton#segment[pos="right"] {{ border-top-right-radius: 6px; border-bottom-right-radius: 6px; }}

QTableWidget {{
    background: {t['base']}; alternate-background-color: {t['base']}; border: 1px solid {t['border']};
    border-radius: 8px; gridline-color: transparent; color: {t['text']};
    selection-background-color: {t['selection']}; selection-color: {t['text']};
}}
QTableWidget::item {{ padding: 0 8px; border-bottom: 1px solid {_mix(t['base'], t['text'], 0.06)}; }}
QTableWidget::item:selected {{ background: {t['selection']}; color: {t['text']}; }}
QTableWidget QCheckBox {{ margin-left: 10px; }}
QHeaderView::section {{
    background: {t['base']}; color: {t['muted']}; border: none; border-bottom: 1px solid {t['border']};
    padding: 6px 8px; font-size: {small}pt;
}}
QTableCornerButton::section {{ background: {t['base']}; border: none; }}

QListWidget#queue {{ background: transparent; border: none; outline: none; }}
QListWidget#queue::item, QListWidget#queue::item:selected, QListWidget#queue::item:hover {{
    background: transparent; border: none;
}}
QFrame#card {{ background: {t['card']}; border: 1px solid {t['border']}; border-radius: 8px; }}
QFrame#card[active="true"] {{ border: 1px solid {t['accent']}; }}
QFrame#card[selected="true"] {{ background: {t['subtle']}; border: 1px solid {t['accent']}; }}
QFrame#card QLabel {{ background: transparent; }}
QLabel#cardIndex {{ color: {t['faint']}; font-size: {small}pt; }}
QLabel#cardTitle {{ font-weight: 600; }}
QLabel#cardMeta, QLabel#cardMessage {{ color: {t['muted']}; font-size: {small}pt; }}
QLabel#pill {{ border-radius: 8px; padding: 1px 8px; font-size: {small}pt; font-weight: 600; }}

QProgressBar {{ border: none; background: {t['track']}; border-radius: 3px; max-height: 6px; min-height: 6px; }}
QProgressBar::chunk {{ background: {t['accent']}; border-radius: 3px; }}

QPlainTextEdit#log {{
    background: {t['base']}; color: {t['text']}; border: 1px solid {t['border']}; border-radius: 8px;
    padding: 4px 6px; selection-background-color: {t['accent']};
}}
QCheckBox {{ spacing: 6px; }}
QCheckBox:disabled {{ color: {t['faint']}; }}
QLabel#infoBadge {{
    border: 1px solid {t['border_strong']}; border-radius: 9px; color: {t['muted']}; font-weight: 600;
    background: transparent;
}}
QLabel#infoBadge:hover {{ border-color: {t['accent']}; color: {t['accent_fg']}; }}
QSplitter::handle {{ background: transparent; }}
QStatusBar {{ color: {t['muted']}; }}
QToolTip {{ padding: 6px 8px; }}
"""
