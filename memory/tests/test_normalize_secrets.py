"""Normalization and secret-detector unit tests.

Credential-shaped fixtures are assembled at runtime so the repository itself
never contains anything a secret scanner would (rightly) flag.
"""

from __future__ import annotations

import secrets
import string

import pytest

from surooh_memory.normalize import content_hash, normalize
from surooh_memory.secrets_filter import detect


def rand(n: int, alphabet: str = string.ascii_letters + string.digits) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(n))


@pytest.mark.parametrize(
    "a,b",
    [
        ("الشحن عبر PostNL", "شحن عبر postnl"),               # al- prefix + case
        ("أسعار الشحن", "اسعار شحن"),                          # hamza folding
        ("الفاتورة جاهزة", "الفاتوره جاهزه"),                  # taa marbuta
        ("مُرَاجَعَة الطلب", "مراجعه طلب"),                       # harakat stripped
        ("سعر ١٢٣ يورو", "سعر 123 يورو"),                      # Arabic-Indic digits
        ("Use  PostNL,  always!", "use postnl always"),       # punctuation/whitespace
        ("Café", "cafe"),                                     # Latin diacritics
        ("بالشحن السريع", "شحن سريع"),                          # bi+al prefix
    ],
)
def test_normalize_equivalences(a, b):
    assert normalize(a) == normalize(b)
    assert content_hash(normalize(a)) == content_hash(normalize(b))


def test_al_prefix_keeps_short_words():
    # "ال" + one letter is not stripped (would destroy the word).
    assert normalize("الم") == "الم"


def test_different_text_different_hash():
    assert content_hash(normalize("ship with PostNL")) != content_hash(normalize("ship with DHL"))


@pytest.mark.parametrize(
    "text",
    [
        lambda: "my password is " + rand(12),
        lambda: "wachtwoord: " + rand(10),
        lambda: "كلمة السر هي " + rand(10),
        lambda: "api_key=" + rand(24),
        lambda: "token: " + rand(40),
        lambda: "AKIA" + rand(16, string.ascii_uppercase + string.digits),
        lambda: "ghp" + "_" + rand(36),
        lambda: "sk-" + "ant-" + rand(40),
        lambda: "Authorization: Bearer " + rand(40),
        lambda: "-----BEGIN RSA " + "PRIVATE KEY-----\nabc",
        lambda: "postgres://admin:" + rand(12) + "@db.example.com/x",
        lambda: "use " + rand(48) + " for the webhook",
        lambda: "eyJ" + rand(20) + ".eyJ" + rand(20) + "." + rand(20),
        lambda: "card 4111 1111 1111 1111",
    ],
)
def test_secret_detected(text):
    assert detect(text()) is not None


@pytest.mark.parametrize(
    "text",
    [
        "We ship all Bol.com orders with PostNL from now on.",
        "The token is required for every request.",
        "Password is stored in the vault, never in chat.",
        "نشحن كل الطلبات عبر PostNL",
        "Order 1234567 was refunded.",
        "subject shipping.carrier decided on 2026-10-29",
    ],
)
def test_ordinary_text_not_flagged(text):
    assert detect(text) is None
