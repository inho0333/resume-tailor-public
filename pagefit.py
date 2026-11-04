"""One-page verdict measured with Word, plus repair of widow bullets (a bullet whose last line
carries only one or two words).

## Why this module exists (measured)

The arithmetic estimate in `validate.py` **cannot tell pass from fail near the boundary.** One
draft was estimated at "+0.0 lines against the template, expected to stay on one page, PASS"
and Word measured **two pages**. The same thing happened on another posting (estimate -0.3
lines PASS / measured 2 pages, and after cutting 110 characters to get back to one page the
estimate was still -0.3 lines). The canonical template itself already fills a page almost
exactly, at 45 lines, so a single extra line overflows it.

So **the Microsoft Word installed on this machine is called directly through osascript to
measure the page and line counts.** LibreOffice rendering was abandoned because of font
substitution (it renders the same template as two pages because it has no Calibri); Word has
Calibri, so it measures the canonical template as one page — which makes it trustworthy. Even
then, to rule out a font-substitution illusion, the canonical template is **measured alongside
as a control** (if the template is one page and the output is two, the overflow is real).

Word is optional. Without it nothing here runs and the page count stays an estimate that can be
wrong at the boundary; `python run.py doctor` reports that state.

## widow bullets

When a bullet takes three lines but only one or two words sit on the third, that bullet wastes
almost a whole line and eats into the one-page budget. Removing just the characters that landed
on the last line makes that line disappear, so this is **the only kind of overflow where both
the place to fix and the amount to remove are computed exactly.** Conversely, cutting a line
from a bullet whose last line is full takes deleting more than 100 characters, which damages the
content — those cases are left to a human instead of being touched automatically.

Line boundaries are read through Word's predefined bookmark `\\Line` (the same thing as VBA's
`Bookmarks("\\Line")`). Putting the cursor at the start of a paragraph and repeatedly jumping to
the end of that line yields the real rendered line boundaries.
"""

import contextlib
import json
import os
import re
import shutil
import subprocess

import docx

import resume_writer
from docx.oxml.ns import qn

# ── Tuning constants ────────────────────────────────────────────────────────
WIDOW_MAX_WORDS = 2      # At or below this many words on the last line, it is a widow.
TRIM_SAFETY_CHARS = 10   # Cut the last line's chars plus this much so the line surely disappears
                         # (absorbs rounding at word boundaries).
# Calibration: of the 14 bullets in the canonical template (a confirmed one-pager), the only ones
# ending with two or fewer words on the last line are the Skills line (1 word) and one short
# bullet in ADDITIONAL (2 words); all the rest end with 4 or more words — that is, "one or two
# words on the last line" is not the normal distribution but something to repair. By contrast,
# one generated draft had 5 of 13 bullets ending in a single word, each wasting a whole line.

# Lines whose format you fixed are excluded from automatic compression. The Skills/Languages
# lines have a settled "capability (tool)" order and item composition, so an LLM must not shorten
# them at will (your instruction).
PROTECTED_PREFIXES = ("Skills:", "Languages:")
MAX_LINE_WALK = 15       # Cap on the line walk within one paragraph (no infinite loop).
# One measurement takes 10-20 seconds for 13 bullets. Why the cap came down from 180 seconds: when
# Word puts up its "Grant File Access" dialog, osascript does not finish **until a human clicks
# it**. In an unattended daemon there is nobody to click, and the generation pipeline stalled for
# that whole time — now it gives up quickly and falls back to the estimate, stating "measurement
# failed" in the report (permissions are settled once and for all with `pagefit.py --grant`).
OSASCRIPT_TIMEOUT = 75
GRANT_TIMEOUT = 600     # --grant mode only: gives a human plenty of time to click the dialog.


class WordUnavailable(RuntimeError):
    """Microsoft Word is not in this environment, so no measurement is possible."""


# If the permission dialog is up or Word is not responding, **do not retry inside the same
# process**. A single generation calls Word in three or more places (enforce's measurement and
# its control measurement, validate's page check) — if it is blocked, each one burns 75 seconds
# and stretches generation past four minutes. Returning the first failure reason verbatim keeps
# "measurement failed" recorded in the report only once.
_SESSION_BLOCKED = None


def _block_session(reason):
    global _SESSION_BLOCKED
    _SESSION_BLOCKED = reason


def reset_session_block():
    """Call this when there is a reason to try again — e.g. after `--grant` settled permissions."""
    global _SESSION_BLOCKED
    _SESSION_BLOCKED = None


# The **fixed path** for the temporary copy used for measuring. Why the path and the filename are
# pinned (you reported "the Grant Access dialog keeps coming up"): Microsoft Word runs inside the
# App Sandbox, so if the file the script tells it to open sits at a path Word has not yet been
# granted access to, it puts up the **"Grant Access" dialog**. Generating a different temporary
# filename every time (tempfile) makes that window reappear on every run, and in an unattended
# daemon there is nobody to click it, so osascript stalls until the timeout (180 seconds). With
# one path and one filename, allowing it once is the end of it. Avoiding `/tmp` and friends is for
# the same reason (the sandbox is more sensitive there, and the folder you have been opening in
# Word for weeks is under output/).
_PROBE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", ".pagefit")
_PROBE_NAME = "pagefit_probe.docx"


@contextlib.contextmanager
def _measure_copy(path):
    """Copy the original to the fixed path and yield that path.

    Why a copy is measured: see the comment on `measure()` — it prevents the accident of Word
    closing an original you have open. If the copy fails, the original path is yielded as is
    (the old behaviour beats not measuring at all).

    **The copy is not deleted after the measurement**: Word's "Grant File Access" dialog can
    stay on screen after osascript has finished, and if you then click allow, Word tries to open
    the file at that path again. Deleting the file makes that click fail, so the permission is
    never recorded in the end (measured: that is why the same window kept reappearing). Each run
    only overwrites it, so there is always exactly one such file, and `output/` is gitignored, so
    the repository is unaffected."""
    probe = os.path.join(_PROBE_DIR, _PROBE_NAME)
    try:
        os.makedirs(_PROBE_DIR, exist_ok=True)
        shutil.copy2(path, probe)
    except OSError as e:
        print(f"  [warning] could not copy for measurement, measuring the original: {e}")
        yield path
        return
    yield probe


def word_available():
    if not shutil.which("osascript"):
        return False
    return os.path.isdir("/Applications/Microsoft Word.app")


def bullet_paragraph_indices(path):
    """The 1-based indices of bulleted (numPr) paragraphs — the same numbering as AppleScript's
    `paragraph N`. Relies on the order of python-docx's `doc.paragraphs` matching Word's
    paragraph numbers."""
    doc = docx.Document(str(path))
    return [
        i + 1
        for i, p in enumerate(doc.paragraphs)
        if resume_writer.is_bullet(p) and p.text.strip()
    ]


_MEASURE_TEMPLATE = r'''
set targetFile to "{path}"
set idxList to {{{indices}}}
set out to ""
tell application "Microsoft Word"
    open targetFile
    set d to document "{basename}"
    if d is missing value then error "GRANT_PENDING"
    set out to out & "PAGES\t" & ((compute statistics d statistic statistic pages) as string) & linefeed
    set out to out & "LINES\t" & ((compute statistics d statistic statistic lines) as string) & linefeed
    repeat with idx in idxList
        set i to idx as integer
        set tr to text object of paragraph i of d
        set pStart to start of content of tr
        set pEnd to end of content of tr
        set cur to pStart
        set lineCount to 0
        set lastLine to ""
        set prevStart to -1
        repeat with step from 1 to {max_walk}
            select (create range d start cur end cur)
            set lr to text object of bookmark "\\Line" of d
            set ls to (start of content of lr)
            set le to (end of content of lr)
            if ls ≤ prevStart then exit repeat
            set prevStart to ls
            set lineCount to lineCount + 1
            set lastLine to (content of lr)
            if le ≥ pEnd then exit repeat
            set cur to le
        end repeat
        set out to out & "BULLET\t" & (i as string) & tab & (lineCount as string) & tab & lastLine & linefeed
    end repeat
    close d saving no
end tell
return out
'''


def _run_osascript(script, timeout=None):
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout or OSASCRIPT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _block_session("Word did not respond (likely waiting on the permission dialog)")
        raise WordUnavailable(
            f"the Word measurement did not finish within {timeout or OSASCRIPT_TIMEOUT}s — Word "
            f"may be waiting on its 'Grant File Access' permission dialog. "
            f"Run `.venv/bin/python pagefit.py --grant` once and allow it; it will not ask again"
        )
    if proc.returncode != 0:
        err = proc.stderr.strip()[:300]
        if "GRANT_PENDING" in err or "missing value" in err:
            _block_session("Word file access has not been granted")
            # The document did not open = Word is sitting on the permission dialog, waiting. The
            # original message ("missing value doesn't understand the compute statistics
            # message") told you nothing at all about the cause.
            raise WordUnavailable(
                "Word could not open the file — click allow in the 'Grant File Access' window on "
                "screen, then run `.venv/bin/python pagefit.py --grant` to settle the permission "
                "once (it will not ask again afterwards)"
            )
        raise WordUnavailable(f"osascript failed: {err}")
    return proc.stdout


def measure(path, with_bullets=True, timeout=None):
    """Measure with Word. Returns:
    {"pages": int, "lines": int,
     "bullets": [{"index", "lines", "last_line", "last_line_chars", "last_line_words", "text"}]}

    WordUnavailable if there is no Word.

    **The measurement is always done on a temporary copy.** It used to open the output and the
    canonical template in Word directly and close them with `close ... saving no`, but those are
    files you may have open and be reviewing at the same moment — especially the canonical
    template, which is measured as a control on every run. `open` on an already-open document
    hands back that document's handle, and the following `close saving no` **silently throws away
    your unsaved edits.** Measuring a copy removes that risk, and because the copy's name is
    fixed, `document "<name>"` can refer to exactly that document — the old `active document`
    measured the wrong document whenever another one was in front (two concurrent generations
    measured each other's documents; concurrent generation itself is now prevented by genlock)."""
    if not word_available():
        raise WordUnavailable(
            "Microsoft Word is not installed (no measurement in this environment)")
    if _SESSION_BLOCKED:
        raise WordUnavailable(f"{_SESSION_BLOCKED} — skipping measurement for this run "
                              f"(one run of `pagefit.py --grant` fixes it)")
    path = os.path.abspath(str(path))
    indices = bullet_paragraph_indices(path) if with_bullets else []
    with _measure_copy(path) as probe:
        script = _MEASURE_TEMPLATE.format(
            path=probe.replace("\\", "\\\\").replace('"', '\\"'),
            basename=os.path.basename(probe).replace('"', '\\"'),
            indices=", ".join(str(i) for i in indices),
            max_walk=MAX_LINE_WALK,
        )
        raw = _run_osascript(script, timeout=timeout)

    doc = docx.Document(path)
    paragraphs = doc.paragraphs
    result = {"pages": None, "lines": None, "bullets": []}
    for line in raw.splitlines():
        parts = line.split("\t")
        if parts[0] == "PAGES" and len(parts) > 1:
            result["pages"] = int(parts[1])
        elif parts[0] == "LINES" and len(parts) > 1:
            result["lines"] = int(parts[1])
        elif parts[0] == "BULLET" and len(parts) >= 3:
            idx = int(parts[1])
            last_line = ("\t".join(parts[3:]) if len(parts) > 3 else "").strip("\r\n")
            # The paragraph mark comes along with a paragraph's last line — strip whitespace only.
            last_line = last_line.strip()
            text = paragraphs[idx - 1].text.strip() if idx - 1 < len(paragraphs) else ""
            result["bullets"].append({
                "index": idx,
                "lines": int(parts[2]),
                "last_line": last_line,
                "last_line_chars": len(last_line),
                "last_line_words": len([w for w in re.split(r"\s+", last_line) if w]),
                "text": text,
            })
    if result["pages"] is None:
        raise WordUnavailable(f"could not read the page count out of Word's output: {raw[:200]!r}")
    return result


def word_pages(path):
    """For callers that only need the page count (validate.py). None if it cannot be measured —
    the caller then falls back to the estimate."""
    try:
        return measure(path, with_bullets=False)["pages"]
    except (WordUnavailable, subprocess.SubprocessError, OSError, ValueError):
        return None


def is_protected(text):
    return any(text.strip().startswith(p) for p in PROTECTED_PREFIXES)


def find_widows(measurement):
    """Bullets wasting a line because only one or two words are left on the last one. Sorted by
    how much is wasted. Only bullets of two or more lines count (for a one-line bullet the last
    line is the whole thing, so nothing is wasted). Lines with a fixed format (Skills etc.) are
    excluded."""
    widows = []
    for b in measurement.get("bullets", []):
        if b["lines"] < 2 or is_protected(b["text"]):
            continue
        if b["last_line_words"] <= WIDOW_MAX_WORDS:
            widows.append(b)
    return sorted(widows, key=lambda b: (b["last_line_words"], b["last_line_chars"]))


def trim_target_chars(bullet):
    """How many characters to remove from this bullet to make its last line disappear."""
    return bullet["last_line_chars"] + TRIM_SAFETY_CHARS


def set_bullet_text(path, index, new_text):
    """Replace the text of the paragraph at `index` (1-based) and save. Keeps the formatting of
    the first run and deletes the rest — the bullets in generated output are all a single run, so
    no formatting is lost (resume_writer writes them that way)."""
    doc = docx.Document(str(path))
    p = doc.paragraphs[index - 1]
    if not p.runs:
        raise ValueError(f"paragraph {index} has no runs, so its text cannot be replaced")
    for r in p.runs[1:]:
        r._r.getparent().remove(r._r)
    p.runs[0].text = new_text
    doc.save(str(path))


def format_report(state):
    """Turn enforce()'s result into a human-readable multi-line string."""
    lines = []
    base = f", template {state['baseline_lines']} lines" if state.get("baseline_lines") else ""
    lines.append(f"Word measurement: {state['pages_after']} pages / "
                 f"{state['lines_after']} lines{base}")
    for t in state.get("trimmed", []):
        lines.append(f"  [auto-compressed] {t['before_chars']} chars → {t['after_chars']} chars "
                     f"(paragraph {t['index']})")
        lines.append(f"    before: {t['before'][:90]}")
        lines.append(f"    after : {t['after'][:90]}")
    for w in state.get("widows_left", []):
        lines.append(f"  [wasted line remains] paragraph {w['index']}: "
                     f"{w['last_line_words']} words on the last line"
                     f" (\"{w['last_line'][:40]}\")")
    if state.get("notes"):
        for n in state["notes"]:
            lines.append(f"  {n}")
    return "\n".join(lines)


def enforce(path, baseline_path=None, shorten_fn=None, max_rounds=3, log=print):
    """**The one-page guarantee loop.** Measure with Word; if the result is not one page or is
    over the line budget, pick a widow bullet, compress it with `shorten_fn`, and measure again,
    repeating until it is one page (at most max_rounds times).

    shorten_fn(text, target_chars, bullet) -> the new text (or None). On None that bullet is
    skipped. Passing None (or having no Word) only measures and diagnoses; the file is untouched.

    Returns: {"available", "pages_before", "pages_after", "lines_before", "lines_after",
           "baseline_lines", "trimmed": [...], "widows_left": [...], "level", "text", "notes"}
    level/text are the exact format used for notify.Report's "length" line (the same contract as
    validate.pagefit)."""
    state = {"available": False, "attempted": False, "failure": None,
             "trimmed": [], "widows_left": [], "notes": [],
             "pages_before": None, "pages_after": None,
             "lines_before": None, "lines_after": None, "baseline_lines": None,
             "level": "tight", "text": "not measured"}
    if not word_available():
        state["notes"].append("no Microsoft Word, so the measurement is skipped "
                              "(only validate.py's estimate applies)")
        state["text"] = "cannot measure (no Word)"
        return state

    # A marker that in an environment with Word the measurement **was attempted**. If it was
    # attempted and failed, the caller (run.py) states that fact in the report — it used to fall
    # back to the estimate silently, so an estimate already judged untrustworthy went out to you
    # as "expected to stay on one page" (the case in the comment at the top of this module).
    state["attempted"] = True
    try:
        m = measure(path)
    except (WordUnavailable, subprocess.SubprocessError, OSError, ValueError) as e:
        state["notes"].append(f"measurement failed (skipped): {e}")
        state["failure"] = str(e)[:200]
        state["text"] = "measurement failed"
        return state

    state["available"] = True
    state["pages_before"] = state["pages_after"] = m["pages"]
    state["lines_before"] = state["lines_after"] = m["lines"]

    if baseline_path and os.path.exists(str(baseline_path)):
        try:
            state["baseline_lines"] = measure(baseline_path, with_bullets=False)["lines"]
        except (WordUnavailable, subprocess.SubprocessError, OSError, ValueError) as e:
            state["notes"].append(f"could not measure the canonical template (no control): {e}")

    budget = state["baseline_lines"]
    rounds = 0
    while rounds < max_rounds:
        over_pages = (m["pages"] or 1) > 1
        over_budget = budget is not None and m["lines"] is not None and m["lines"] > budget
        widows = find_widows(m)
        if not (over_pages or over_budget):
            break
        if shorten_fn is None:
            state["notes"].append("no compression function, so this only measured "
                                  "(a manual edit is needed)")
            break
        if not widows:
            state["notes"].append(
                "no line-wasting (widow) bullet, so nothing was compressed automatically — "
                "cutting a line from a bullet whose last line is full would take deleting 100+ "
                "characters and would damage the content. A human should drop a bullet")
            break

        target_bullet = widows[0]
        need = trim_target_chars(target_bullet)
        log(f"  [one-page fix] paragraph {target_bullet['index']} has only "
            f"{target_bullet['last_line_words']} words left on its last line, wasting a line → "
            f"trying to compress by at least {need} chars")
        try:
            new_text = shorten_fn(target_bullet["text"], need, target_bullet)
        except Exception as e:  # keeps an LLM call failure from blocking the whole pipeline
            state["notes"].append(f"the compression call failed (stopping): {e}")
            break
        if not new_text or len(new_text) >= len(target_bullet["text"]):
            state["notes"].append(f"compression of paragraph {target_bullet['index']} failed "
                                  f"(the result is not shorter) — stopping")
            break

        set_bullet_text(path, target_bullet["index"], new_text)
        state["trimmed"].append({
            "index": target_bullet["index"],
            "before": target_bullet["text"], "after": new_text,
            "before_chars": len(target_bullet["text"]), "after_chars": len(new_text),
        })
        rounds += 1
        try:
            m = measure(path)
        except (WordUnavailable, subprocess.SubprocessError, OSError, ValueError) as e:
            state["notes"].append(f"re-measurement failed: {e}")
            break
        state["pages_after"] = m["pages"]
        state["lines_after"] = m["lines"]

    state["widows_left"] = find_widows(m)
    pages = state["pages_after"] or 1
    waste = f" ({len(state['widows_left'])} wasted lines)" if state["widows_left"] else ""
    if pages > 1:
        state["level"] = "over"
        state["text"] = f"measured {pages} pages — over one page{waste}"
    elif budget is not None and state["lines_after"] and state["lines_after"] > budget:
        # The page count is one, but there are more lines than in the canonical template — the
        # template fills a page almost exactly, so from this state one more edit can overflow it.
        state["level"] = "tight"
        state["text"] = (f"measured one page, {state['lines_after'] - budget} lines more than "
                         f"the template{waste}")
    else:
        # A remaining widow is not an overflow but information that a line is still free — it is
        # not compressed automatically (a bullet is supposed to fill the lines it takes, and we
        # are inside the one-page budget right now).
        state["level"] = "ok"
        state["text"] = f"measured one page, confirmed{waste}"
    return state


def grant_access(sample_docx=None, log=print):
    """**The manual command that settles Word's file access in one go** (`pagefit.py --grant`).

    Word is a sandboxed app, so when a script asks it to open a file at a path it has not been
    granted yet, it puts up the "Grant File Access" dialog and stops until a human clicks. The
    old code opened **the generated resume file itself**, and because that filename changed every
    time, the window appeared on every generation (you reported: "the Grant Access dialog keeps
    coming up"). Now only a copy at a fixed path is opened, so granting that one path once means
    it never appears again — and this command handles that one time while a human is present.

    Returns: whether it succeeded."""
    if not word_available():
        log("No Microsoft Word, so this environment does not use measurement — "
            "no permission setup needed either.")
        return False
    if not sample_docx:
        import config
        sample_docx = config.DEFAULT_RESUME_PATH
    if not (sample_docx and os.path.exists(sample_docx)):
        log(f"Cannot find a sample docx: {sample_docx!r} — pass any .docx path as an argument.")
        return False

    reset_session_block()
    probe = os.path.join(_PROBE_DIR, _PROBE_NAME)
    log("When Word shows the 'Grant File Access' window, click **Grant Access** once.")
    log(f"  Path being granted: {probe}")
    log("  This path never changes, so after this one time you will not be asked again.")
    try:
        m = measure(sample_docx, with_bullets=False, timeout=GRANT_TIMEOUT)
    except (WordUnavailable, subprocess.SubprocessError, OSError, ValueError) as e:
        log(f"Failed: {e}")
        return False
    log(f"Success — measurement confirmed working ({m['pages']} pages, {m['lines']} lines).")

    # Check that a second measurement finishes with no dialog (= verify the permission really
    # stuck).
    try:
        m2 = measure(sample_docx, with_bullets=False, timeout=OSASCRIPT_TIMEOUT)
    except (WordUnavailable, subprocess.SubprocessError, OSError, ValueError) as e:
        log(f"⚠️ The second measurement failed — the permission does not look like it stuck: {e}")
        return False
    log(f"Re-check succeeded ({m2['pages']} pages) — the permission holds. "
        f"Measurement will now run on the automatic path too.")
    return True


def main():
    import argparse

    ap = argparse.ArgumentParser(
        description="Check whether it is one page, and find widow bullets, by measuring with Word")
    ap.add_argument("docx", nargs="?")
    ap.add_argument("--grant", action="store_true",
                    help="get Word file access granted once (a human clicks the dialog)")
    ap.add_argument("--baseline",
                    help="the canonical template .docx "
                         "(default: config.DEFAULT_RESUME_PATH)")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args()
    if args.grant:
        raise SystemExit(0 if grant_access(args.docx) else 1)
    if not args.docx:
        ap.error("a .docx path to measure is required (or --grant)")
    baseline = args.baseline
    if baseline is None:
        import config
        baseline = config.DEFAULT_RESUME_PATH
    state = enforce(args.docx, baseline_path=baseline, shorten_fn=None)
    print(json.dumps(state, ensure_ascii=False, indent=2) if args.json else format_report(state))


if __name__ == "__main__":
    main()
