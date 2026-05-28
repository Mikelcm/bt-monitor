"""Text helpers for export pipelines.

Reportlab's default fonts (Helvetica/Times) don't render Latin Extended-A
(ă â î ș ț). Rather than ship a TTF, we strip diacritics for all PDF + Excel
text. HTML stays UTF-8.
"""
from __future__ import annotations

import unicodedata


# Explicit map for chars NFKD doesn't decompose cleanly across all platforms.
_EXTRA_MAP = {
    "Ă": "A", "ă": "a",
    "Â": "A", "â": "a",
    "Î": "I", "î": "i",
    "Ș": "S", "ș": "s", "Ş": "S", "ş": "s",
    "Ț": "T", "ț": "t", "Ţ": "T", "ţ": "t",
    "ä": "a", "ö": "o", "ü": "u", "ß": "ss",
    "—": "-", "–": "-", "‚": ",", "„": '"', "“": '"', "”": '"',
    "‘": "'", "’": "'", "→": "->", "←": "<-", "·": "*",
}


def strip_diacritics(s) -> str:
    """Return s with Romanian diacritics + smart quotes mapped to ASCII.

    Works for any input — strings pass through, None → '', other types → str().
    """
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    if not s:
        return s
    # 1. explicit map covers ș/ț (combining-comma) which NFKD sometimes leaves alone
    for k, v in _EXTRA_MAP.items():
        if k in s:
            s = s.replace(k, v)
    # 2. NFKD fallback for any remaining combining marks
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))
