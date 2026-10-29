"""Secret detectors. Anything that looks like a credential is rejected before storage.

Bias: false positives (rejecting a harmless sentence) are acceptable; storing a
real credential is not. The matched value is never echoed back or logged —
only the detector name is reported.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

# Well-known credential formats.
_FORMAT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[A-Z0-9]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("slack_webhook", re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]+", re.I)),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("openai_anthropic_key", re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{20,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer_token", re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{16,}", re.I)),
    ("basic_auth_header", re.compile(r"\bbasic\s+[A-Za-z0-9+/]{16,}={0,2}", re.I)),
    ("url_credentials", re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@", re.I)),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}\b")),
    # 15-19 digits + Luhn. 13/14-digit EAN/GTIN product codes are deliberately excluded.
    # Contiguous 15-19 digits, or standard grouped layouts (4-4-4-4[-1..3], Amex 4-6-5).
    ("card_number", re.compile(
        r"(?<![\d-])(?:\d{15,19}|\d{4}([ -])\d{4}\1\d{4}\1\d{4}(?:\1\d{1,3})?|\d{4}([ -])\d{6}\2\d{5})(?![\d-])"
    )),
]

# "<credential word> <sep> <value>" in English, Dutch and Arabic.
_CRED_WORDS = (
    r"pass(?:word|wd|phrase)?|pwd|wachtwoord|secret|client[_ -]?secret|token|"
    r"api[_ -]?key|apikey|access[_ -]?key|secret[_ -]?key|private[_ -]?key|"
    r"auth(?:orization)?|credentials?|pin(?:code)?|otp|2fa|"
    r"كلمة\s*(?:ال)?(?:سر|مرور)|كلمه\s*(?:ال)?(?:سر|مرور)|الباسورد|باسورد|الرمز\s*السري|رمز\s*سري|"
    r"التوكن|توكن|المفتاح(?:\s*السري)?|مفتاح\s*(?:ال)?(?:api|سري)"
)
_ASSIGNMENT = re.compile(
    rf"(?:{_CRED_WORDS})\s*(?:[:=]|\bis\b|\bis:|\bwas\b|\bequals\b|\bis\s+set\s+to\b|هي|هو|هيه|=|:)\s*['\"`]?(?P<value>[^\s'\"`,;]{{4,}})",
    re.I,
)
_ASSIGNMENT_WORDS_ONLY = re.compile(r"^[a-z؀-ۿ]+$", re.I)  # value is a plain word
_PLAIN_WORD_ALLOWED = {
    # "the token is required", "password is stored in vault" ... are not values.
    "required", "needed", "stored", "missing", "expired", "invalid", "valid", "rotated",
    "revoked", "set", "configured", "managed", "kept", "saved", "secret", "private",
    "public", "optional", "mandatory", "the", "a", "an", "in", "not", "never", "always",
    "only", "used", "generated", "reset", "changed", "strong", "weak", "long", "short",
}

_HIGH_ENTROPY_TOKEN = re.compile(r"[A-Za-z0-9+/_=.-]{32,}")
# URLs without userinfo are removed before the entropy check (credential URLs are caught above).
_PLAIN_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s@]+(?=\s|$)", re.I)


def _shannon_entropy(s: str) -> float:
    counts = Counter(s)
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def detect(text: str, *, entropy: bool = True) -> str | None:
    """Return the name of the first detector that fires, or None.

    ``entropy=False`` skips the generic high-entropy check, for identifier
    fields (e.g. ``source_ref`` holding a conversation UUID) where random-looking
    strings are expected; format and assignment detectors still apply.
    """
    if not text:
        return None
    t = unicodedata.normalize("NFKC", text)

    for name, pat in _FORMAT_PATTERNS:
        for m in pat.finditer(t):
            if name == "card_number":
                digits = re.sub(r"\D", "", m.group(0))
                if 15 <= len(digits) <= 19 and _luhn_ok(digits):
                    return name
                continue
            return name

    for m in _ASSIGNMENT.finditer(t):
        value = m.group("value")
        if _ASSIGNMENT_WORDS_ONLY.match(value) and value.casefold() in _PLAIN_WORD_ALLOWED:
            continue
        return "credential_assignment"

    if not entropy:
        return None
    for m in _HIGH_ENTROPY_TOKEN.finditer(_PLAIN_URL.sub(" ", t)):
        tok = m.group(0)
        classes = sum(
            bool(re.search(p, tok)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]")
        )
        if classes >= 2 and _shannon_entropy(tok) >= 3.5:
            return "high_entropy_string"
    return None


def contains_secret(*fields: str | None, entropy: bool = True) -> str | None:
    for f in fields:
        hit = detect(f or "", entropy=entropy)
        if hit:
            return hit
    return None
