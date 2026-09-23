"""Tier 0: undo the disguises before anything tries to read the text.

Every screen downstream matches on characters, so an attacker who changes the
characters without changing the meaning defeats all of them at once. The 2026
evasion literature is almost entirely this: character injection, homoglyphs,
zero-width padding, spacing, encoding (arXiv 2504.11168, and the seven
techniques catalogued by infosec.qa).

Normalisation is therefore not an optimisation, it is the precondition. On the
hard corpus, `encoded` and `spacing` scored 0/9 before this module existed.

Two rules keep it honest:

* Never mutate what gets stored. This produces a *probe* string used only for
  matching; the original text is what reaches the graph.
* Decode additively. A decoded payload is appended rather than replacing the
  text, so a rule that legitimately *mentions* base64 keeps its own wording and
  only gains the decoded content - which is what a screen needs to see.
"""
import base64
import binascii
import codecs
import re
import unicodedata

# Zero-width, joiners, and bidirectional overrides: invisible to a reviewer,
# fully visible to a model, and the cheapest way past a keyword list.
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁤﻿­]")

# Cyrillic and Greek lookalikes. NFKC does not fold these - they are distinct
# characters that merely render identically - so they need an explicit map.
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "с": "c", "р": "p", "х": "x", "у": "y",
    "і": "i", "ѕ": "s", "ԁ": "d", "һ": "h", "ⅼ": "l", "ν": "v", "ｇ": "g",
    "А": "A", "Е": "E", "О": "O", "С": "C", "Р": "P", "Х": "X", "У": "Y",
    "Ι": "I", "Ν": "N", "Ѕ": "S", "α": "a", "ο": "o", "ρ": "p", "τ": "t",
})

_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})

# "I g n o r e", "I.g.n.o.r.e", "I-g-n-o-r-e": single characters separated by a
# single space, dot or dash. Requires 4+ in a row so ordinary initialisms and
# hyphenated words are untouched.
_SPACED = re.compile(r"(?:(?<![A-Za-z])[A-Za-z][ .\-]){3,}[A-Za-z](?![A-Za-z])")

_B64 = re.compile(r"\b[A-Za-z0-9+/]{16,}={0,2}\b")
_HEX = re.compile(r"\b(?:[0-9a-fA-F]{2}){8,}\b")
_URLENC = re.compile(r"(?:%[0-9a-fA-F]{2}){4,}")


def _despace(text: str) -> str:
    return _SPACED.sub(lambda m: re.sub(r"[ .\-]", "", m.group(0)), text)


def _decoded_fragments(text: str) -> list[str]:
    """Anything hidden inside the text, decoded. Failures are skipped silently:
    a screen must never break on malformed input."""
    out: list[str] = []
    for m in _B64.findall(text):
        try:
            pad = m + "=" * (-len(m) % 4)
            decoded = base64.b64decode(pad, validate=True).decode("utf-8", "ignore")
            if decoded.isprintable() and len(decoded) > 6:
                out.append(decoded)
        except (binascii.Error, ValueError):
            pass
    for m in _HEX.findall(text):
        try:
            decoded = bytes.fromhex(m).decode("utf-8", "ignore")
            if decoded.isprintable() and len(decoded) > 6:
                out.append(decoded)
        except ValueError:
            pass
    for m in _URLENC.findall(text):
        try:
            from urllib.parse import unquote
            out.append(unquote(m))
        except Exception:
            pass
    # ROT13 is its own inverse and cheap, so always add the rotation: harmless
    # noise unless the original was rotated, in which case it reveals the payload.
    try:
        out.append(codecs.encode(text, "rot_13"))
    except Exception:
        pass
    # Reversed text reads as noise to a human but not to a model.
    out.append(text[::-1])
    return out


def probe(text: str) -> str:
    """The string a screen should match against: the original plus every
    de-obfuscated and decoded view of it, concatenated.

    Additive by design - matching the union means a disguise can only ever
    *add* a signal, never remove one, so normalisation cannot make detection
    worse than matching the raw text.
    """
    if not text:
        return ""
    base = unicodedata.normalize("NFKC", text)
    base = _INVISIBLE.sub("", base)
    base = base.translate(_CONFUSABLES)
    despaced = _despace(base)
    views = [base, despaced, base.translate(_LEET), despaced.translate(_LEET)]
    views.extend(_decoded_fragments(base))
    return "\n".join(v for v in views if v)
