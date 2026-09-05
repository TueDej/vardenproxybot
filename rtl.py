"""Bidi helpers for Persian Telegram messages.

Emoji, punctuation and digits are directionally neutral (UAX #9): when a
line starts with them, clients guess the paragraph base direction and the
emoji/punctuation can jump to the wrong end — especially on short/wrapped
lines and reply-keyboard buttons. Forcing an RTL base with an invisible
U+200F RIGHT-TO-LEFT MARK (RLM) per line fixes the common cases.

Button labels keep their canonical (clean) constants for ``==`` comparison;
incoming text is normalized with :func:`strip_bidi` before comparison since
a pressed RLM-prefixed button echoes the RLM back.
"""

from __future__ import annotations

import re

RLM = "\u200f"  # RIGHT-TO-LEFT MARK — forces RTL base direction
LRM = "\u200e"  # LEFT-TO-RIGHT MARK
FSI = "\u2068"  # FIRST STRONG ISOLATE — auto-detects embedded direction
LRI = "\u2066"  # LEFT-TO-RIGHT ISOLATE
RLI = "\u2067"  # RIGHT-TO-LEFT ISOLATE
PDI = "\u2069"  # POP DIRECTIONAL ISOLATE

_BIDI_CONTROLS = (RLM, LRM, FSI, LRI, RLI, PDI, "\u202a", "\u202b", "\u202c", "\u202d", "\u202e")

_FA_RE = re.compile(
    r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]"
)


def contains_fa(s: str) -> bool:
    """Return True if the string contains any Persian/Arabic-script char."""
    return bool(s and _FA_RE.search(s))


def strip_bidi(s: str) -> str:
    """Remove invisible directional controls for robust text comparison."""
    if not s:
        return s
    for ch in _BIDI_CONTROLS:
        s = s.replace(ch, "")
    return s


def rtl(text: str) -> str:
    """Force an RTL paragraph base on every line containing Persian.

    Prepends one RLM to such lines (idempotent). Lines without Persian
    (e.g. ``<code>vless://...</code>`` links, URLs) are left untouched so
    LTR content keeps its natural direction. Safe inside Telegram HTML:
    the mark is a text node, tags still parse.
    """
    if not text or not contains_fa(text):
        return text
    out: list[str] = []
    for line in text.split("\n"):
        if not line or not contains_fa(line):
            out.append(line)
            continue
        if line.lstrip(" \t").startswith(RLM):
            out.append(line)
            continue
        out.append(f"{RLM}{line}")
    return "\n".join(out)


def btn(label: str) -> str:
    """Render a reply/inline keyboard label with RTL base (idempotent)."""
    if not label or not contains_fa(label):
        return label
    if label.startswith(RLM):
        return label
    return f"{RLM}{label}"


def user(name: str) -> str:
    """Wrap an already-escaped free-direction name (Latin or Persian).

    FSI/PDI isolate lets the bidi algorithm detect the name's own direction
    instead of leaking it into the surrounding Persian sentence.
    """
    if not name:
        return name
    if name.startswith(FSI):
        return name
    return f"{FSI}{name}{PDI}"
