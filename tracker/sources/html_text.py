"""Turning an ATS API's HTML description into readable text.

Greenhouse hands back the posting body as an HTML fragment; so do several
others. BeautifulSoup is already a dependency, but this is a handful of
substitutions on a fragment that is about to be read by a model, and keeping it
dependency-free means a source file can be copied out of here and run on its own.
"""

import re
from html import unescape


def to_text(fragment):
    """An HTML fragment as plain text, with paragraph breaks preserved.

    unescape runs FIRST. Some single-posting endpoints return the body
    double-escaped, so the tags arrive as `&lt;div&gt;` — strip tags before
    unescaping and not one substitution matches, leaving the markup sitting in
    the text the model then reads as the job description.
    """
    text = unescape(fragment or "")
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</(p|li|div|h[1-6]|tr)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = text.replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()
