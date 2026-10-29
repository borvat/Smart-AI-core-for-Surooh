"""Text normalization for dedup and search (Arabic + Latin).

The same function is applied to stored text and to recall queries, so any
lossy step (e.g. al- stripping) is symmetric and only affects matching,
never the text that is returned to the user.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# Arabic letter variants folded to one form.
_ARABIC_MAP = str.maketrans(
    {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ى": "ي",
        "ئ": "ي",
        "ؤ": "و",
        "ة": "ه",
        "ـ": "",  # tatweel
        # Arabic-Indic and Extended Arabic-Indic digits.
        **{chr(0x0660 + i): str(i) for i in range(10)},
        **{chr(0x06F0 + i): str(i) for i in range(10)},
    }
)

# Light prefix stripping: the definite article, optionally with a one-letter
# conjunction/preposition. Longest first. Only stripped when >= 2 letters remain.
_AL_PREFIXES = ("وال", "بال", "فال", "كال", "لل", "ال")
_MIN_REMAINDER = 2

_SPACE_RE = re.compile(r"\s+")


def _strip_marks(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


# Meaning-bearing symbols kept as their own tokens, so "> 10%" and "< 10%" differ.
_KEPT_CHARS = frozenset("%-")
_KEPT_CATEGORIES = frozenset({"Sm", "Sc"})  # math (< > = + ...) and currency (€ $ ...)


def _punct_to_space(text: str) -> str:
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in _KEPT_CHARS or cat in _KEPT_CATEGORIES:
            out.append(f" {ch} ")
        else:
            out.append(" " if cat[0] in ("P", "S", "Z", "C") else ch)
    return "".join(out)


def strip_al(token: str) -> str:
    for prefix in _AL_PREFIXES:
        if token.startswith(prefix) and len(token) - len(prefix) >= _MIN_REMAINDER:
            return token[len(prefix):]
    return token


def tokens(text: str) -> list[str]:
    """Normalized tokens of ``text`` (possibly empty)."""
    t = unicodedata.normalize("NFKC", text)
    t = t.translate(_ARABIC_MAP)
    t = _strip_marks(t)
    t = t.casefold()
    t = _punct_to_space(t)
    return [strip_al(tok) for tok in _SPACE_RE.split(t) if tok]


def normalize(text: str) -> str:
    """Canonical form used for dedup hashing and full-text indexing."""
    return " ".join(tokens(text))


def content_hash(norm_text: str) -> str:
    return hashlib.sha256(norm_text.encode("utf-8")).hexdigest()
