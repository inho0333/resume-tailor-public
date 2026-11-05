def extract_text(message):
    """Finds and returns the text block among a Claude response's content blocks (with
    extended thinking, content[0] may be a ThinkingBlock, so it is located by type rather
    than by a fixed index)."""
    for block in message.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise ValueError("The response contains no text block.")


# Characters a model emits that are typographically identical to an ASCII one, but are not
# ASCII. Measured once: a local model returned U+2011 (non-breaking hyphen) in "working-capital"
# and "follow-ons", every gate passed it — the dash check only looked for — and – — and the
# character reached the .docx, where it renders correctly in Word and then breaks the moment the
# text is pasted into a plain-text ATS field.
#
# The em dash and en dash are deliberately NOT in this table. They are a style violation the
# validators reject on sight, not noise to be cleaned up silently.
_TYPOGRAPHIC_EQUIVALENTS = {
    "‐": "-",   # hyphen
    "‑": "-",   # non-breaking hyphen
    "‒": "-",   # figure dash
    "−": "-",   # minus sign
    " ": " ",   # no-break space
    " ": " ",   # narrow no-break space
    " ": " ",   # thin space
    "​": "",    # zero-width space
    "﻿": "",    # zero-width no-break space
}

# What must never reach the document, after normalisation has done what it can.
NON_ASCII_DASHES = "‐‑‒–—−"


def normalize_typography(text):
    """Replace the look-alike characters above with their ASCII equivalents.

    Applied to every sentence a model proposes, before validation — so a stray non-breaking
    hyphen costs nothing (the edit is kept, the character is gone) while a real style violation
    still reaches the validators intact.
    """
    if not text:
        return text
    for bad, good in _TYPOGRAPHIC_EQUIVALENTS.items():
        text = text.replace(bad, good)
    return text
