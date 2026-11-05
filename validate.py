"""Mechanical checks on a generated résumé .docx. Page overflow is estimated by
arithmetic alone, without rendering. Three layers:
  - GUARDRAILS: double dashes, bullet length, mixed fonts — decided immediately.
  - PAGE-FIT: fixed formatting values (margins and the like are read from the actual
    docx) plus a character-count line estimate — advisory.
  - VERDICT: the final call is always a human opening the file in Google Docs or Word.
    This script does not claim that authority.

CALIBRATION: CHARS_PER_LINE_AT_11PT assumes 11pt because this project's baseline
template almost never sets a font size per run (the document default = Word's default
11pt Calibri). If you open real output in Google Docs and it overflows differently than
estimated, nudge this value and leave a dated note.

## Why absolute thresholds were dropped for "relative to the template" (measured once)

Measured with this same estimator, the baseline template itself — the résumé you are
already using on one page — comes to 58.9/60.0 lines = 98%, with 1.1 lines to spare.
In other words, under the old rule (WARN below `HEADROOM_LINES=2`) **even the known-good
original résumé triggers a warning.** In practice, 8 of 13 generated drafts came back
WARN and 3 came back FAIL, and those FAILs were estimated to be over by only 1.7 to 2.7
lines — noise from an absolute scale that sits a line or two off.

So the reference point became "the same estimate run against the baseline template"
rather than "the page height". Measuring both documents with the same estimator cancels
the calibration error, which makes `generated − template` the amount that genuinely grew.
The template fitting on one page is a measured fact, so that difference is all you need.

Counting real rendered pages with LibreOffice (`soffice --convert-to pdf`) was tried and
abandoned: on a machine without Calibri installed **even the baseline template renders as
two pages** (font substitution changes the line height). It looks like an authoritative
verdict while being wrong on every file, so it is not used.
"""

import argparse
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

import docx
from docx.oxml.ns import qn

import llm_utils

# ── Calibration (tune these) ─────────────────────────────────────────────────
CHARS_PER_LINE_AT_11PT = 95  # Estimated chars per 11pt line at full width. Unverified.
LINE_HEIGHT_TWIPS = 240      # One line at single spacing (twips), close to most body text.
# Allowed growth over the baseline template's estimate, in lines. The template is
# measured to fit on one page, so this much is considered safe. The 0.5 line absorbs
# the estimator's rounding error (a ceil per paragraph).
TEMPLATE_HEADROOM_LINES = 0.5
BULLET_WARN_CHARS = 260      # Guards the prompt's 250-char bullet cap, 10 to spare.
BULLET_INDENT_TWIPS = 360    # Estimated bullet hanging indent (corrects numPr width).


def _numpr(paragraph):
    return paragraph._p.find(f".//{qn('w:numPr')}")


def _has_page_break(paragraph):
    if paragraph.paragraph_format.page_break_before:
        return True
    return any(br.get(qn("w:type")) == "page" for br in paragraph._p.iter(qn("w:br")))


def _paragraph_font_size_pt(paragraph, default=11.0):
    for run in paragraph.runs:
        if run.text.strip() and run.font.size is not None:
            return run.font.size.pt
    if paragraph.style is not None and paragraph.style.font.size is not None:
        return paragraph.style.font.size.pt
    return default


def _paragraph_fonts(paragraph):
    return {run.font.name for run in paragraph.runs if run.text.strip() and run.font.name}


def _est_lines(paragraph, usable_width_twips):
    text = paragraph.text.strip()
    if not text:
        return 1
    width = usable_width_twips - (BULLET_INDENT_TWIPS if _numpr(paragraph) is not None else 0)
    width = max(width, 1000)
    size_pt = _paragraph_font_size_pt(paragraph)
    chars_per_line = max(20, round(CHARS_PER_LINE_AT_11PT * (width / usable_width_twips) * (11.0 / size_pt)))
    return max(1, math.ceil(len(text) / chars_per_line))


def _spacing_twips(paragraph):
    fmt = paragraph.paragraph_format
    before = fmt.space_before.twips if fmt.space_before is not None else 0
    after = fmt.space_after.twips if fmt.space_after is not None else 0
    return before, after


def _word_page_count(path):
    """The page count as Word actually renders it, or None when there is no way to
    measure it here. The arithmetic estimate can be wrong, so this value always wins
    when it exists.

    Two paths: (1) on WSL, powershell.exe plus word_pagecount.ps1; (2) on macOS,
    driving Word through osascript (`pagefit.word_pages`). The estimate was used alone
    for a while on the assumption that macOS had no way to measure it, when in fact Word
    was installed and osascript could count exactly — and in the meantime two drafts the
    estimate had passed as one page were really two."""
    import pagefit

    pages = pagefit.word_pages(path)
    if pages is not None:
        return pages

    ps = shutil.which("powershell.exe")
    wsl = shutil.which("wslpath")
    if not ps or not wsl:
        return None
    script = Path(__file__).with_name("word_pagecount.ps1")
    try:
        win_doc = subprocess.run([wsl, "-w", str(Path(path).resolve())], capture_output=True, text=True, check=True).stdout.strip()
        win_ps = subprocess.run([wsl, "-w", str(script)], capture_output=True, text=True, check=True).stdout.strip()
        out = subprocess.run(
            [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", win_ps, "-Path", win_doc],
            capture_output=True, text=True, timeout=120,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    m = re.search(r"PAGES=(\d+)", out)
    return int(m.group(1)) if m else None


def estimate_lines(path):
    """Estimated line count up to the first explicit page break (= the part meant to be
    page one).
    Returns: (used_lines, budget_lines, contributors[(height_twips, lines, text), ...])"""
    doc = docx.Document(str(path))
    section = doc.sections[0]
    usable_height_twips = section.page_height.twips - section.top_margin.twips - section.bottom_margin.twips
    usable_width_twips = section.page_width.twips - section.left_margin.twips - section.right_margin.twips

    used = 0
    contributors = []
    for p in doc.paragraphs:
        if _has_page_break(p):
            break
        lines = _est_lines(p, usable_width_twips)
        before, after = _spacing_twips(p)
        h = lines * LINE_HEIGHT_TWIPS + before + after
        used += h
        contributors.append((h, lines, p.text.strip()[:60] or "(empty paragraph)"))

    return used / LINE_HEIGHT_TWIPS, usable_height_twips / LINE_HEIGHT_TWIPS, contributors


def bullet_chars_total(path):
    """Total characters across bulleted paragraphs. Measured against the baseline
    template it becomes the measured budget for "how much fits on one page", and it is
    passed straight into the tailor_engine prompt so generation respects it up front."""
    doc = docx.Document(str(path))
    return sum(len(p.text.strip()) for p in doc.paragraphs
               if _numpr(p) is not None and p.text.strip())


def validate_docx(path, baseline_path=None):
    """Run the checks and print a report to the console.

    baseline_path: the baseline template (= a document measured to fit on one page).
    Given one, page overflow is judged by "growth over the template" rather than by an
    absolute scale (see the module docstring for why).

    Returns: {"result": "PASS"|"PASS (with warnings)"|"FAIL",
              "pagefit": {"level": "ok"|"tight"|"over",
                          "text": "<one line, used as-is in the notification>"}}
    """
    path = Path(path)
    msgs = []

    def fail(m):
        msgs.append(("FAIL", m))

    def warn(m):
        msgs.append(("WARN", m))

    def ok(m):
        msgs.append(("OK", m))

    if not path.exists():
        print(f"FAIL  file not found: {path}")
        return {"result": "FAIL", "pagefit": {"level": "over", "text": "file not found"}}

    doc = docx.Document(str(path))
    paragraphs = doc.paragraphs
    full_text = "\n".join(p.text for p in paragraphs)

    if "—" in full_text:
        fail("found an em dash (—) — never use one on a résumé (it reads as AI). "
             "Rewrite with a comma, a period, a semicolon or a different clause structure")
    else:
        ok("no em dash")

    if "--" in full_text:
        fail("found a double dash (--) — probably a typo, check it")
    else:
        ok("no double dash")

    # Look-alike characters: a non-breaking hyphen renders correctly in Word and then breaks
    # when the text is pasted into a plain-text ATS field. The en dash is included here rather
    # than in the em dash check so the message can name the character.
    exotic = sorted({ch for ch in full_text if ch in llm_utils.NON_ASCII_DASHES and ch != "—"})
    if exotic:
        fail("found a non-ASCII dash/hyphen "
             f"({', '.join('U+%04X' % ord(ch) for ch in exotic)}) — it looks right in Word and "
             "breaks in a plain-text paste. Replace it with a plain hyphen")
    else:
        ok("no look-alike hyphen")

    fonts_found = set()
    for p in paragraphs:
        fonts_found |= _paragraph_fonts(p)
    if len(fonts_found) > 1:
        warn(f"mixed fonts: {sorted(fonts_found)}")
    elif fonts_found:
        ok(f"font is consistent: {sorted(fonts_found)[0]}")
    else:
        ok("no font name set on any run (the document default is presumably in use) — skipped")

    long_bullets = [
        p.text for p in paragraphs if _numpr(p) is not None and len(p.text) > BULLET_WARN_CHARS
    ]
    if long_bullets:
        for t in long_bullets:
            warn(f"bullet is {len(t)} characters, estimated over the 250 cap: \"{t[:55]}...\"")
    else:
        ok("every bullet is estimated to be within the length cap")

    # ── PAGE-FIT (up to the first explicit page break = page one) ────────────
    used_lines, budget_lines, contributors = estimate_lines(path)
    pct = 100 * used_lines / budget_lines if budget_lines else 0

    baseline_lines = None
    if baseline_path and Path(baseline_path).exists():
        try:
            baseline_lines, _, _ = estimate_lines(baseline_path)
        except Exception as e:  # changed or unreadable: fall back to the absolute scale
            warn(f"could not read the baseline template (falling back to the absolute scale): {e}")

    if baseline_lines is not None:
        delta = used_lines - baseline_lines
        shared = (f"{delta:+.1f} lines vs the template "
                  f"(estimated {used_lines:.1f}, template {baseline_lines:.1f})")
        if delta <= TEMPLATE_HEADROOM_LINES:
            ok(f"page estimate: {shared} — should stay on one page")
            pagefit = {"level": "ok", "text": f"one page expected ({delta:+.1f} lines)"}
        elif delta <= 1.5:
            warn(f"page estimate: {shared} — borderline, check it in Google Docs yourself")
            pagefit = {"level": "tight",
                       "text": f"borderline for one page ({delta:+.1f} lines vs the template)"}
        else:
            fail(f"page estimate: {shared} — likely to spill onto a second page, "
                 "cut a bullet or write it shorter")
            pagefit = {"level": "over",
                       "text": f"may exceed one page ({delta:+.1f} lines vs the template)"}
            for h, lines, text in sorted(contributors, reverse=True)[:5]:
                msgs.append(("INFO", f"  {h / LINE_HEIGHT_TWIPS:.1f} lines: {text}"))
    else:
        # The absolute scale, used only when no baseline could be read. The template
        # itself measures 98%, so the headroom is set to 0 lines (a 2-line rule turned
        # the known-good document into a warning).
        margin_lines = budget_lines - used_lines
        shared = f"{used_lines:.1f}/{budget_lines:.1f} lines ({pct:.0f}%)"
        if margin_lines >= 0:
            ok(f"page estimate: {shared} — no baseline (absolute scale)")
            pagefit = {"level": "tight", "text": f"one page at {pct:.0f}% (no baseline)"}
        else:
            fail(f"page estimate: over budget by {-margin_lines:.1f} lines ({pct:.0f}%)")
            pagefit = {"level": "over",
                       "text": f"estimated {-margin_lines:.1f} lines over one page"}

    # ── Real rendering check (only where it is possible) ─────────────────────
    # Works only on WSL with Word and powershell. It is missing on macOS, and a WARN
    # saying "could not verify" is not raised every time — it was pure noise, identical
    # on all 11 files, and the last line of the console output already says the estimate
    # is advisory.
    pages = _word_page_count(path)
    if pages is not None:
        if pages > 1:
            fail(f"Word renders this as {pages} pages — over one page. Cut a bullet")
            pagefit = {"level": "over", "text": f"measured {pages} pages — over one page"}
        else:
            ok("Word renders this as one page")
            # If the measurement says one page, an arithmetic FAIL was an overestimate,
            # so it is downgraded to a WARN.
            msgs[:] = [("WARN", m + " (downgraded to a warning: measured as one page)")
                       if k == "FAIL" and m.startswith("page estimate") else (k, m)
                       for k, m in msgs]
            pagefit = {"level": "ok", "text": "measured one page"}

    width = max(len(k) for k, _ in msgs)
    for kind, m in msgs:
        print(f"{kind:<{width}}  {m}")

    failed = any(k == "FAIL" for k, _ in msgs)
    warned = any(k == "WARN" for k, _ in msgs)
    result = "FAIL" if failed else ("PASS (with warnings)" if warned else "PASS")
    print(f"\nRESULT: {result}")
    print("The final call is always what a human sees after opening it in Google Docs or Word.")
    return {"result": result, "pagefit": pagefit}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("docx")
    ap.add_argument("--baseline", help="the baseline template .docx "
                                       "(default: config.DEFAULT_RESUME_PATH)")
    args = ap.parse_args()
    baseline = args.baseline
    if baseline is None:
        import config
        baseline = config.DEFAULT_RESUME_PATH
    report = validate_docx(args.docx, baseline_path=baseline)
    sys.exit(1 if report["result"] == "FAIL" else 0)


if __name__ == "__main__":
    main()
