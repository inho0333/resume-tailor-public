"""`run.py setup` — the one command that gets a new machine working.

## Why this exists

Setting this up used to be eleven steps across three hand-edited files, in an
order that mattered and was only implied: pick a backend before running
bootstrap, because bootstrap makes a model call; write `profile.yaml` by copying
an example and replacing every value, including some that only make sense if you
are an enrolled student. Each step was documented. Together they were a wall,
and the two failures they produced most were silent ones — a profile left at the
example's values, so every judgment was about a fictional person; and a target
role list nobody edited, so a stranger's job search was applied to every posting.

So the order is code now, not prose. Six stages, each one asking only what the
previous answers make relevant, each writing only the keys it owns.

## Rules this file follows

- **Nothing is overwritten silently.** Existing values become the defaults, and
  `user_config.yaml` is patched key by key (`run._patch_yaml_keys`) so the
  comments in it — the explanations, and whatever notes you wrote about your own
  decisions — survive.
- **Every question is skippable, and skipping means something precise.** An
  empty answer writes the empty value, and the empty value's documented meaning
  is "do not reason about this". It is never a hidden filter.
- **A credential is checked against the real service before it is written.** A
  source that cannot run returns no postings, and no postings is
  indistinguishable from a quiet day: that is how one placeholder board token
  was polled every two minutes for a week and reported as "nothing new".
- **It can be re-run.** `--only <stage>` redoes one part.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys

import yaml

import ask
import config
import profile as profile_module

STAGES = ("environment", "resume", "identity", "search", "sources", "notifications")


# ── small helpers ───────────────────────────────────────────────────────────

def _heading(number, title):
    print(f"\n{'─' * 72}\n{number}. {title}\n{'─' * 72}")


def _choose(prompt, options, default=None):
    """Pick one of `options` by number. Returns the value, or `default`.

    options: [(value, label, detail)]
    """
    for idx, (_value, label, detail) in enumerate(options, 1):
        print(f"  {idx}. {label}")
        if detail:
            for line in detail.splitlines():
                print(f"       {line}")
    answer = ask.line(f"{prompt} [1-{len(options)}]"
                      + (f", Enter = {default}" if default is not None else "") + ": ")
    if not answer:
        return default
    try:
        index = int(answer)
    except ValueError:
        print("  not a number — keeping the default")
        return default
    if 1 <= index <= len(options):
        return options[index - 1][0]
    print("  out of range — keeping the default")
    return default


def _choose_many(prompt, options, default=()):
    """Pick several by number, comma-separated. Returns a list of values."""
    for idx, (_value, label, detail) in enumerate(options, 1):
        print(f"  {idx}. {label}")
        if detail:
            for line in detail.splitlines():
                print(f"       {line}")
    answer = ask.line(f"{prompt} (comma-separated numbers, Enter = none): ")
    if not answer:
        return list(default)
    picked = []
    for part in answer.replace(" ", "").split(","):
        try:
            index = int(part)
        except ValueError:
            continue
        if 1 <= index <= len(options):
            picked.append(options[index - 1][0])
    return picked


def _list_answer(prompt, default=()):
    """A comma-separated list. Enter keeps the default; "-" clears it."""
    shown = ", ".join(str(d) for d in default)
    answer = ask.line(f"{prompt}"
                      + (f" [{shown}]" if shown else " (Enter to skip)") + ": ")
    if not answer:
        return list(default)
    if answer.strip() == "-":
        return []
    return [part.strip() for part in answer.split(",") if part.strip()]


def _validated(prompt, default="", check=None, hint="", tries=3):
    """Ask until the answer passes `check`, or fall back to the default.

    Most fields here take anything a person might type. A few have a format the
    rest of the pipeline relies on, and silently storing a wrong one is worse
    than asking twice: `market.country` has to be two letters, and a profile
    once came out of a mis-ordered run with a country of
    "NORTHWIND DATA, HELIOS ROBOTICS" — which the loader then read as empty,
    so the file said one thing and every judgment used another.
    """
    for _ in range(tries):
        answer = ask.line(prompt)
        if not answer:
            return default
        if check is None or check(answer):
            return answer
        print(f"    {hint}")
    print(f"    keeping {default!r}")
    return default


def _int_answer(prompt, default=None):
    answer = ask.line(f"{prompt}" + (f" [{default}]" if default is not None else
                                     " (Enter to skip)") + ": ")
    if not answer:
        return default
    try:
        return int(answer)
    except ValueError:
        print("  not a number — leaving it unset")
        return default


def _load_profile_data():
    """What profile.yaml holds today, as a dict. {} when there is none."""
    if not os.path.exists(profile_module.PROFILE_PATH):
        return {}
    try:
        with open(profile_module.PROFILE_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        print(f"  [warn] your profile.yaml could not be read ({e}); starting fresh")
        return {}


PROFILE_HEADER = """\
# profile.yaml — who you are, and what you are looking for.
#
# Written by `python run.py setup`. Edit it by hand whenever you like; re-running
# setup uses whatever is here as the defaults.
#
# An empty value is not a missing setting. It means "do not reason about this" —
# an empty `locations` screens no posting on where it is, and an empty
# `comp_floor` never compares pay. profile.example.yaml explains every field and
# what breaks when it is wrong.
#
# Gitignored. Nothing in this file is committed, and nothing leaves this machine
# except into the prompts built here.

"""


def _save_profile(data):
    """Write profile.yaml, keeping a .bak of what was there."""
    path = profile_module.PROFILE_PATH
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as f:
        f.write(PROFILE_HEADER)
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    os.chmod(path, 0o600)
    profile_module.reset_cache()
    print(f"\n  wrote {os.path.basename(path)}")


def _patch_user_config(updates):
    """Patch user_config.yaml in place, comments intact. updates: {key: [lines]}.

    The writing itself lives in `run._patch_user_config_keys`, next to the
    comment-preserving `_patch_yaml_keys` it depends on; this adds the line the
    wizard prints.
    """
    import run  # imported here: run imports this module

    keys = run._patch_user_config_keys(updates)
    print(f"  wrote {os.path.basename(config.USER_CONFIG_PATH)}: {', '.join(keys)}")


def _yaml_block(key, value):
    """One top-level key as YAML lines, for _patch_yaml_keys."""
    dumped = yaml.safe_dump({key: value}, allow_unicode=True, sort_keys=False,
                            default_flow_style=False)
    return dumped.rstrip("\n").splitlines()


# ── 1. environment ──────────────────────────────────────────────────────────

def stage_environment():
    """Find out what this machine can do, then choose the backends.

    This runs first because the next stage needs a working model: bootstrap tags
    every bullet in your résumé with one light call. The old setup guide listed
    the backend step before bootstrap and never said why, so getting the order
    wrong failed in the middle of reading your document.
    """
    _heading(1, "What this machine can do")

    import llm_client
    import pagefit

    availability = llm_client.available_backends()
    for name, ok in availability.items():
        print(f"  {'yes' if ok else ' no'}  {name}")
    usable = [name for name, ok in availability.items() if ok]
    if not usable:
        print("\n  No model backend is available, and nothing here works without one.\n"
              "  The cheapest to set up, in order:\n"
              "    brew install ollama && ollama serve & && ollama pull gpt-oss:20b\n"
              "        free, local, nothing leaves this machine\n"
              "    export ANTHROPIC_API_KEY=sk-ant-...\n"
              "        best quality, costs per posting\n"
              "    install the Claude Code CLI and log in\n"
              "        uses a subscription you may already have")
        return False

    print("\n  What each role is for:")
    print("    light — reading postings, judging fit. Runs many times. A local model is fine.")
    print("    heavy — writing the sentences you submit. Runs once per draft, and this is")
    print("            where quality shows. A frontier model is strongly preferred.")

    options = [("auto", "auto — pick the best available each time (recommended)",
                "light and heavy are resolved separately; see llm_client.py")]
    options += [(name, name, "") for name in usable]
    light = _choose("\n  Backend for the light role", options, default="auto")
    heavy = _choose("  Backend for the heavy role", options, default="auto")

    updates = {"light_backend": [f"light_backend: {light}"],
               "heavy_backend": [f"heavy_backend: {heavy}"]}
    _patch_user_config(updates)

    print("\n  One-page measurement:")
    if pagefit.word_available():
        print("    Microsoft Word found — the page count is measured, not estimated.")
        print("    Run `python pagefit.py --grant` once to clear Word's file-access dialog;")
        print("    without that, an unattended run waits for a click nobody is there to make.")
    else:
        print("    Microsoft Word not found. The page count falls back to an estimate")
        print("    against your own template, which is usually right and can be wrong at")
        print("    the boundary. Open the finished .docx once before you send it.")
        if shutil.which("soffice") or shutil.which("libreoffice"):
            print("    LibreOffice is installed but is not used for this: without Calibri it")
            print("    renders a correct one-pager as two pages, which is worse than the")
            print("    estimate.")
    return True


# ── 2. your résumé ──────────────────────────────────────────────────────────

def stage_resume():
    """Turn your own .docx into the experience database and the approved baseline.

    All three files this writes come from one document, which is the point:
    `resume_db.yaml` (everything you could say), `base_resume.yaml` (what your
    approved résumé actually says — the quality floor minimal mode edits), and
    `user_config.yaml`'s paths and per-section bullet caps, which are the counts
    that made your résumé fit on one page in the first place.
    """
    _heading(2, "Your résumé")

    import run

    template = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "templates", "resume_template.docx")
    current = config.DEFAULT_RESUME_PATH or ""
    print("  This tool edits a résumé you have already approved; it does not invent one.")
    print("  What it needs: a .docx that already fits on one page, with an EXPERIENCE")
    print("  heading, and real Word bullets rather than prose paragraphs.\n")
    if current:
        print(f"  Currently: {current}")

    # Reading a document writes the database from scratch, which is right exactly
    # once. Offering it a second time is offering to throw away the `context` the
    # user wrote — the slow, valuable part — along with every `retired` flag and
    # the ids base_resume.yaml references.
    if os.path.exists(config.DB_PATH):
        print(f"\n  {os.path.basename(config.DB_PATH)} already exists. Re-reading a document")
        print("  here writes it from scratch and would discard the `context` you wrote,")
        print("  every `retired` flag, and the ids base_resume.yaml points at.")
        print("\n  To fold a NEWER résumé in without losing any of that:")
        print("    python run.py import --resume <newer.docx> --dry-run")
        print("    python run.py import --resume <newer.docx>")
        if not ask.confirm("\n  Start the database over from a document anyway? (y/N): ",
                           default=False):
            print("  Left it alone.")
            return True

    path = ask.line("  Path to your résumé .docx"
                    + (f" [{current}]" if current else "") + ": ") or current
    if not path:
        if ask.confirm(f"\n  No path given. Use the bundled example résumé instead?\n"
                       f"  ({os.path.relpath(template)} — a fictional one-pager in the right\n"
                       f"  shape, so you can see the whole pipeline work before using your\n"
                       f"  own document.) (Y/n): ", default=True):
            path = template
        else:
            print("  Skipped. Re-run with: python run.py setup --only resume")
            return False
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        print(f"  No such file: {path}")
        return False

    heading = ask.line("  The heading your work history sits under [EXPERIENCE]: ") \
        or "EXPERIENCE"
    print("\n  Reading it...")
    args = _namespace(resume=path, section_heading=heading, backend=None)
    try:
        run.cmd_bootstrap(args)
    except Exception as e:
        print(f"  Could not read it: {type(e).__name__}: {e}")
        print("  SETUP.md's 'Get your résumé into the right shape' section lists the four "
              "requirements.")
        return False

    _detect_protected_prefixes(path)
    return True


def _namespace(**kwargs):
    return argparse.Namespace(**kwargs)


def _detect_protected_prefixes(path):
    """Find this document's labelled lines, so no edit reorders them.

    A "Skills:" line has a settled order and composition, and shortening it is
    the easiest way for a model to win back a line. The protected list was
    hardcoded as exactly ("Skills:", "Languages:"), so a résumé reading
    "Technical Skills:" or "Tools:" had no protection at all and was reworded
    freely.
    """
    import re

    import docx

    try:
        doc = docx.Document(path)
    except Exception:
        return
    found = []
    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        match = re.match(r"^([A-Z][A-Za-z /&-]{1,24}):\s", text)
        if match:
            label = match.group(1) + ":"
            if label not in found:
                found.append(label)
    if not found:
        return
    print(f"\n  Labelled lines found in your résumé: {', '.join(found)}")
    print("  These are protected from editing, swapping and automatic shortening.")
    if ask.confirm("  Protect all of them? (Y/n): ", default=True):
        _patch_user_config({"protected_prefixes": _yaml_block("protected_prefixes", found)})


# ── 3. who you are ──────────────────────────────────────────────────────────

_BACKGROUND_OPTIONS = [
    ("enrolled_student", "A student now",
     "applying for an internship or a first role"),
    ("recent_graduate", "Recently finished studying",
     "little or no full-time history yet"),
    ("experienced", "Years of relevant work",
     "the pile you are compared against is other experienced hires, not students"),
    ("career_changer", "Years of work, in a different field",
     "the fit judgment weighs transferable evidence rather than a title match"),
]


def stage_identity():
    """Who the prompts are about. The one stage where a wrong answer is invisible.

    Every judgment this tool makes is about a person, and the failure mode is
    quiet: a profile left at the example's values produces a complete,
    plausible, confidently-reported résumé for somebody who does not exist. So
    the four identity fields are blocking in `doctor`, and this stage exists to
    make sure they are answered once rather than skipped forever.
    """
    _heading(3, "Who you are")
    data = _load_profile_data()
    # Normalising through Profile means an existing v1 profile.yaml supplies the
    # defaults too, and setup is how a v1 file becomes a v2 one.
    current = profile_module.Profile(data)

    name = ask.line(f"  Your name, as the résumé spells it"
                    + (f" [{current.name}]" if current.name else "") + ": ") or current.name

    print("\n  Which of these are you? It decides how every prompt introduces you,")
    print("  which questions follow, and who the recruiter persona compares you with.")
    kind = _choose("  Your situation", _BACKGROUND_OPTIONS,
                   default=current.background_kind or "experienced")

    years = current.years_experience
    if kind in ("experienced", "career_changer"):
        print("\n  Years of relevant professional experience. This is one half of the")
        print("  years check: a posting stating '8+ years required' against 3 is reported")
        print("  as a mismatch instead of being quietly tiered 'possible fit'.")
        years = _int_answer("  Years of experience", years)
    else:
        years = None

    print("\n  One or two sentences on your career, in the third person. This is the only")
    print("  place the fit judgment learns what you have actually done, so write it the")
    print("  way you would say it out loud, not as a list of titles.")
    summary = ask.line(f"  Your career in a sentence"
                       + (f" [{current.career_summary[:40]}...]"
                          if current.career_summary else "") + ": ") or current.career_summary

    employers = _list_answer("  Past employers, as the résumé spells them",
                             current.employers)

    education = {}
    if kind in ("enrolled_student", "recent_graduate"):
        print("\n  Postings gate on the degree and the class year, so these do real work.")
        education["school"] = ask.line(
            f"  School" + (f" [{current.school}]" if current.school else "") + ": ") \
            or current.school
        print("  Write the degree the way postings write it: 'MBA', 'MS Computer Science'.")
        print("  The string goes into the eligibility prompt verbatim, so 'MBA' matches a")
        print("  posting that says 'MBA candidates' and 'Master's in Business' does not.")
        education["program"] = ask.line(
            f"  Degree" + (f" [{current.program}]" if current.program else "") + ": ") \
            or current.program
        education["graduation"] = ask.line(
            f"  Graduation, YYYY-MM"
            + (f" [{current.graduation}]" if current.graduation else "") + ": ") \
            or current.graduation
        education["completed"] = kind == "recent_graduate"
        education["is_technical"] = ask.confirm(
            "  Is it a technical or engineering degree? (y/N): ",
            default=current.degree_is_technical)
    else:
        print("\n  A degree, if you want postings judged against one. Skip it and a degree")
        print("  requirement is reported as unclear rather than as met or failed.")
        education["school"] = ask.line(
            "  School (Enter to skip)"
            + (f" [{current.school}]" if current.school else "") + ": ") or current.school
        education["program"] = ask.line(
            "  Degree (Enter to skip)"
            + (f" [{current.program}]" if current.program else "") + ": ") or current.program
        education["graduation"] = current.graduation
        education["completed"] = True
        # Asked whenever a degree was given, not only of students. The eligibility
        # screen reads this for everybody — and an experienced hire who answered
        # "MS Computer Science" was being told, in every prompt, that their degree
        # is NOT a technical one, which invites a false "ineligible" on exactly
        # the postings that ask for one.
        education["is_technical"] = (
            ask.confirm("  Is it a technical or engineering degree? (y/N): ",
                        default=current.degree_is_technical)
            if education["program"] else current.degree_is_technical)

    print("\n  Where you are looking, as a two-letter country code (US, GB, DE, CA, SG).")
    print("  It adds that country's own immigration wording to the sponsorship scan — an")
    print("  employer in the UK writes 'Certificate of Sponsorship', not 'H-1B' — and sets")
    print("  the locale company news is read in. The country-neutral scan runs either way.")
    country = _validated(
        "  Country code"
        + (f" [{current.market_country}]" if current.market_country else "") + ": ",
        default=current.market_country,
        check=lambda a: re.fullmatch(r"[A-Za-z]{2}", a.strip()),
        hint="two letters, e.g. US, GB, DE, CA, SG — or Enter to skip").upper()

    print("\n  Your right to work, in one sentence, in the words a recruiter uses:")
    print("    'Citizen' / 'Permanent resident, no sponsorship needed' /")
    print("    'Student visa; internships under authorisation my school approves' /")
    print("    'Requires employer sponsorship for full-time work'")
    print("  Leave it empty and no prompt reasons about your status at all. The")
    print("  sponsorship scan still runs — it reads the posting, not you.")
    statement = ask.line("  Work authorisation (Enter to skip): ") or current.work_authorization

    authorization = {"statement": statement,
                     "citizenship_requirements_block": current.citizenship_requirements_block,
                     "sponsorship_needed_later": current.sponsorship_needed_later}
    if statement:
        authorization["citizenship_requirements_block"] = ask.confirm(
            "  Do 'citizens only' or security-clearance requirements rule you out? (Y/n): ",
            default=current.citizenship_requirements_block)
        authorization["sponsorship_needed_later"] = ask.confirm(
            "  Will you need employer sponsorship for full-time work later? (y/N): ",
            default=current.sponsorship_needed_later)

    print("\n  Two or three of your own strongest bullets, verbatim. The model is shown")
    print("  these as 'this is what a good bullet looks like here' before it edits")
    print("  anything — they set sentence length, how much context a bullet carries, and")
    print("  how numbers are written. The highest-leverage field in the file.")
    style = list(current.style_examples)
    if style:
        print(f"\n  You already have {len(style)}:")
        for example in style:
            print(f"    - {example[:96]}...")
        keep = not ask.confirm("  Replace them? (y/N): ", default=False)
    else:
        style, keep = _suggest_style_examples(), False
        if style:
            # Asking someone to retype two sentences this tool has just read out
            # of their own document is busywork, and the documented penalty for
            # skipping it is "fictional examples, which is safe but weaker" — on
            # the field this file calls the highest-leverage one. So the bullets
            # from the résumé are the proposal, and typing is the fallback.
            print("\n  The two longest work bullets from the résumé you pointed at:")
            for example in style:
                print(f"    - {example[:96]}...")
            keep = ask.confirm("  Use these as the voice reference? (Y/n): ", default=True)
    if not keep:
        typed = []
        while len(typed) < 3:
            bullet = ask.line(f"  Bullet {len(typed) + 1} (Enter to stop): ")
            if not bullet:
                break
            typed.append(bullet)
        if typed:
            style = typed
    if not style:
        print("  None given — the prompts fall back to fictional examples, which is safe")
        print("  but weaker. Your own voice is the point; add them later in profile.yaml.")

    existing = _load_profile_data()
    data = {
        "name": name,
        "background": {"kind": kind, "years_experience": years, "summary": summary,
                       "employers": employers, "education": education},
        # The search block belongs to the next stage. Carried over as it is, or
        # seeded from a v1 profile's flat fields, so running only this stage
        # never wipes what the other one owns.
        "search": existing.get("search") or {
            "kind": current.search_kind, "term": current.target_term,
            "target_roles": current.target_roles, "avoid_roles": current.avoid_roles,
            "seniority": [], "locations": [], "work_arrangement": [],
            "relocate": False, "comp_floor": None, "dealbreakers": [],
        },
        "market": {"country": country},
        "authorization": authorization,
        "voice": {"style_examples": style},
    }
    _save_profile(data)
    return True


def _suggest_style_examples(count=2):
    """The longest work bullets from the database the résumé stage just wrote.

    Longest, because length correlates with carrying the whole shape this field
    exists to demonstrate — verb, what, why, how, impact. A one-clause bullet
    teaches the model nothing about how much context to keep.

    Labelled lines ("Skills:", "Languages:", "Open Source:") are skipped: their
    format is fixed and they are the opposite of a model sentence.
    """
    try:
        import resume_db

        sections = resume_db.load_db(config.DB_PATH)
    except Exception:
        return []
    bullets = []
    for section in sections:
        for bullet in section.get("bullets") or []:
            text = (bullet.get("text") or "").strip()
            if not text or bullet.get("retired"):
                continue
            label, _, rest = text.partition(":")
            if rest and len(label.split()) <= 3:
                continue
            bullets.append(text)
    bullets.sort(key=len, reverse=True)
    return bullets[:count]


# ── 4. what you are looking for ─────────────────────────────────────────────

_SEARCH_KINDS = [
    ("internship", "An internship", "a fixed term, usually while studying"),
    ("new_grad", "A first full-time role", "new-grad and graduate programmes"),
    ("full_time", "A full-time role", "the ordinary case for an experienced hire"),
    ("contract", "Contract or freelance work", ""),
]

_ARRANGEMENTS = [
    ("remote", "Remote", ""),
    ("hybrid", "Hybrid", ""),
    ("onsite", "On site", ""),
]


def stage_search():
    """The axes a posting is screened on — and every one of them is optional.

    This is the stage the first version of the tool did not have. Fit was one
    question ("can this career evidence this posting") plus a substring match on
    the job title, so a posting in the wrong country, at the wrong level, for the
    wrong employment type or below your pay floor arrived looking exactly like a
    good one.

    Each axis filled in here is checked in code rather than by a model — a
    location mismatch is arithmetic — and each mismatch is named in the digest
    instead of being folded into a score. Each axis left empty is not screened.
    """
    _heading(4, "What you are looking for")
    data = _load_profile_data()
    current = profile_module.Profile(data)
    search = dict(data.get("search") or {})

    print("  Every question here is optional, and skipping one is a real answer: that")
    print("  axis is then not screened at all. Nothing here hides a posting from you —")
    print("  a mismatch is reported by name and sorted lower.\n")

    kind = _choose("  What kind of role", _SEARCH_KINDS,
                   default=current.search_kind or "full_time")
    search["kind"] = kind or ""

    if kind == "internship":
        print("\n  Which term, so postings for the wrong year are not offered.")
        search["term"] = ask.line("  Term, e.g. Summer 2027"
                                  + (f" [{current.target_term}]" if current.target_term
                                     else "") + ": ") or current.target_term
    else:
        search["term"] = ""

    print("\n  The roles you actually want, as they appear in job titles, lowercase, one")
    print("  concept each. Short fragments match more titles: 'product manage' catches")
    print("  'Product Management Intern' and 'Product Manager, Payments'.")
    print("  These do real work — a posting whose title matches one can never be tiered")
    print("  'weak fit', and 'strong fit' needs the model and these to agree.")
    search["target_roles"] = _list_answer("  Target roles", current.target_roles)
    if not search["target_roles"]:
        print("  Nothing set. The fit judgment then has only the model's reading to go on,")
        print("  and no posting can reach 'strong fit'. Worth coming back to.")

    print("\n  Roles you keep declining. A match is capped at 'possible fit' and sinks in")
    print("  the digest, but is still shown. Leave it empty on day one and fill it in")
    print("  after a week — `run.py decisions` prints what you have been skipping.")
    search["avoid_roles"] = _list_answer("  Roles to avoid", current.avoid_roles)

    print("\n  Levels you want, as postings title them: senior, staff, principal.")
    print("  Empty means any level.")
    search["seniority"] = [s.lower() for s in
                           _list_answer("  Target levels", current.seniority)]

    print("\n  Places you would take a job, written as a person would: 'San Francisco Bay")
    print("  Area', 'London', 'Berlin'. Empty means anywhere, and no posting is screened")
    print("  on location. A remote posting counts as being in every one of them.")
    search["locations"] = _list_answer("  Locations", current.locations)

    if search["locations"]:
        search["relocate"] = ask.confirm(
            "  Would you move for a role somewhere else? (y/N): ", default=current.relocate)
    else:
        search["relocate"] = False

    print("\n  Which working arrangements you would accept. Empty means all three.")
    search["work_arrangement"] = _choose_many("  Arrangements", _ARRANGEMENTS,
                                              default=current.work_arrangement)

    print("\n  The least you would accept, for postings that state a range. Empty means")
    print("  pay is never considered — the right setting for most internship searches,")
    print("  since those postings rarely state one.")
    amount = _int_answer("  Minimum annual pay",
                         current.comp_floor["amount"] if current.comp_floor else None)
    if amount:
        currency = (ask.line("  Currency [USD]: ") or "USD").upper()
        search["comp_floor"] = {"amount": amount, "currency": currency, "period": "year"}
    else:
        search["comp_floor"] = None

    print("\n  Conditions you will not take, in your own words: 'shift work', 'field")
    print("  operations', 'five days on site', 'on-call rotation'. These replace a list")
    print("  that used to be hardcoded into the fit prompt, so everyone inherited one")
    print("  person's taste — including people looking for exactly what it down-ranked.")
    search["dealbreakers"] = _list_answer("  Will not take", current.dealbreakers)

    data["search"] = search
    _save_profile(data)

    prof = profile_module.Profile(data)
    lines = prof.requirement_lines()
    print("\n  How a posting will be screened from now on:")
    for line in lines or ["    nothing — no posting is screened on type, place, level or pay"]:
        print(f"    {line}")
    return True


# ── 5. where postings come from ─────────────────────────────────────────────

def stage_sources():
    """Choose the job sources, then prove each one actually answers.

    The order they are offered in is the order they are worth adopting, not a
    ranking: an aggregator is better when you are searching broadly, and a
    company's own board is better when you already know the twenty employers you
    care about. tracker/CHANNELS.md is the long version.
    """
    _heading(5, "Where postings come from")

    import tracker

    print("  This step is optional. Without it, postings arrive the way they always")
    print("  did: `run.py tailor --url <posting url>`. With it, `run.py watch` finds")
    print("  them and queues the ones worth asking you about.\n")

    options = []
    for name in tracker.SOURCE_NAMES:
        source = tracker.build(name, {})
        label = f"{name}" + ("" if source.implemented else "  (not implemented)")
        options.append((name, label, source.summary))
    chosen = _choose_many("  Which sources", options,
                          default=[n for n in config.TRACKER_SOURCES])

    usable = []
    for name in chosen:
        if tracker.build(name, {}).implemented:
            usable.append(name)
        else:
            print(f"\n  {name} has no implementation. tracker/sources/{name}.py says why, "
                  f"and what to use instead.")
    chosen = usable
    if not chosen:
        print("\n  No sources chosen. `run.py watch` will say so rather than sit silent.")
        return True

    settings = {}
    for name in chosen:
        settings[name] = _configure_source(name)

    # Check each one BEFORE writing it down, and offer to leave out the ones that
    # cannot run. Selecting greenhouse and then skipping the company list wrote a
    # source with nothing to watch — after which every `doctor` run reported a
    # blocking failure on a setup the user had only just finished, and every
    # `watch` tick repeated it. A source you declined to finish configuring
    # should not become a permanent error.
    print("\n  Checking each one against its API:")
    keep = []
    for name in chosen:
        source = tracker.build(name, settings.get(name) or {})
        status = source.check_auth()
        print(f"    {'ok  ' if status.ok else 'FAIL'}  {name} — {status.detail}")
        if status.ok:
            keep.append(name)
            continue
        for line in (status.fix or "").splitlines():
            print(f"            {line}")
        if ask.confirm(f"    Leave {name} out for now? (Y/n): ", default=True):
            settings.pop(name, None)
            print(f"    {name} not enabled. Add it later with: "
                  f"python run.py setup --only sources")
        else:
            keep.append(name)
            print(f"    keeping {name} — `doctor` will keep reporting it until it works")

    block = {"sources": keep, **settings}
    existing = _tracker_block()
    for key in ("relevance_filter", "block_titles", "block_companies"):
        if key in existing:
            block[key] = existing[key]
    _patch_user_config({"tracker": _yaml_block("tracker", block)})
    if not keep:
        print("\n  No source is enabled. `run.py watch` will say so rather than sit silent,")
        print("  and postings can still arrive with: run.py tailor --url <posting url>")
    return True


def _tracker_block():
    try:
        with open(config.USER_CONFIG_PATH, "r", encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("tracker") or {}
    except (OSError, yaml.YAMLError):
        return {}


_BOARD_KEYS = {"greenhouse": "board_token", "lever": "client_name", "ashby": "org"}
_BOARD_HINTS = {
    "greenhouse": "boards.greenhouse.io/<token> — sometimes only in the embedded iframe",
    "lever": "jobs.lever.co/<client_name>",
    "ashby": "jobs.ashbyhq.com/<org>",
}


def _configure_source(name):
    existing = _tracker_block().get(name) or {}
    print(f"\n  — {name} —")

    if name in _BOARD_KEYS:
        key = _BOARD_KEYS[name]
        print(f"    Companies to watch. Find the identifier in their careers page URL:")
        print(f"      {_BOARD_HINTS[name]}")
        print("    Enter them as  Name=identifier, one per line (Enter to stop).")
        companies = list(existing.get("companies") or [])
        for company in companies:
            print(f"      already: {company}")
        while True:
            entry = ask.line("      Name=identifier: ")
            if not entry:
                break
            display, _, identifier = entry.partition("=")
            identifier = identifier.strip() or display.strip()
            companies.append({"name": display.strip() or identifier, key: identifier})
        return {"companies": companies}

    if name == "apple":
        print("    Apple team ids, from the URL when you filter on jobs.apple.com")
        print("      e.g. internships-STDNT-INTRN")
        teams = _list_answer("      Teams", existing.get("teams") or [])
        print("    Apple is the only source with an exclude list: one team posts hundreds")
        print("    of unrelated roles, so 'business but not hardware' is the useful filter.")
        return {
            "teams": teams,
            "location": ask.line("      Location filter [united-states-USA]: ")
                         or existing.get("location") or "united-states-USA",
            "include_keywords": _list_answer("      Title must contain one of",
                                             existing.get("include_keywords") or []),
            "exclude_keywords": _list_answer("      Title must not contain",
                                             existing.get("exclude_keywords") or []),
        }

    if name == "trackly":
        print("    Install and authenticate:")
        print("      npm install -g trackly-cli")
        print("      trackly api-key create     # then: export TRACKLY_API_KEY=...")
        print("    Prefer the API key over `trackly login`: it needs no browser, which")
        print("    matters when `watch` runs under a scheduler.")
        print()
        print("    IMPORTANT — coverage is set on their site, not here. Your saved")
        print("    preferences (function, region, employment type, sponsorship) decide")
        print("    what their API will return at all; the setting below only narrows that")
        print("    set further. Set your preferences in the Trackly web app first.")
        print("    A company they do not crawl is invisible however well it matches:")
        print("    `trackly request-company <name> --url <careers url>` asks them to add it.")
        return {
            "limit": _int_answer("      How many postings per poll", existing.get("limit") or 50),
            "functions": _list_answer("      Narrow to these functions",
                                      existing.get("functions") or []),
        }

    if name == "remotive":
        import tracker

        source = tracker.build("remotive", {})
        categories = source.categories()
        if categories:
            print("    Their categories (an unknown name returns nothing, not an error):")
            for index in range(0, len(categories), 3):
                print("      " + " · ".join(categories[index:index + 3]))
        return {
            "category": ask.line("      Category (Enter for all)"
                                 + (f" [{existing.get('category')}]"
                                    if existing.get("category") else "") + ": ")
                         or existing.get("category") or "",
            "limit": _int_answer("      How many postings per poll", existing.get("limit") or 50),
        }

    return {}


# ── 6. how you are asked ────────────────────────────────────────────────────

def stage_notifications():
    """Where an approval request goes, and how you answer it.

    No channel is required, and the default needs no credentials: requests land
    in `state/inbox.md` and `run.py inbox` reads them. Telegram is the only one
    that can answer its own messages, which is why it is offered first.
    """
    _heading(6, "How you are asked")

    import channels

    print("  Nothing here is required. With no channel configured, requests are written")
    print("  to state/inbox.md and answered with `run.py pending --make <id>`.\n")

    options = []
    for row in channels.describe(["auto"]):
        factory = channels.CHANNELS[row["name"]]
        channel = factory()
        label = row["name"] + ("  [buttons]" if row["buttons"] else "")
        options.append((row["name"], label, channel.status_line()))
    chosen = _choose_many("  Which channels", options,
                          default=[c.name for c in channels.enabled(config.NOTIFY_CHANNELS)])
    if not chosen:
        chosen = ["local"]
        print("\n  Keeping the local inbox only.")

    if "telegram" in chosen and not channels.CHANNELS["telegram"]().is_configured():
        print("\n  Telegram, in four steps:")
        print("    1. message @BotFather on Telegram and send /newbot")
        print("    2. export TG_BOT_TOKEN=<the token it gives you>")
        print("    3. send your new bot any message, then open")
        print("       https://api.telegram.org/bot$TG_BOT_TOKEN/getUpdates")
        print("       and read result[0].message.chat.id")
        print("    4. export TG_CHAT_ID=<that id>, then run `python bot_listener.py`")
        print("  Put both exports in your shell profile so a scheduled run sees them.")

    hours = _list_answer("\n  Hours to send the digest at, 0-23 "
                         "(Enter keeps twice a day; '-' sends each posting as it arrives)",
                         [str(h) for h in config.DIGEST_HOURS])
    try:
        digest_hours = sorted({int(h) % 24 for h in hours})
    except ValueError:
        print("  not numbers — keeping the current hours")
        digest_hours = list(config.DIGEST_HOURS)

    print("\n  Batching matters more than it sounds: sending one message per posting"
          "\n  produced 15 in a single day, 11 of them inside two and a half hours, and"
          "\n  13 answers out of 87 requests. Batched twice a day, each posting keeps its"
          "\n  own ✅/⏭️ pair on its own row.")

    ttl = _int_answer("\n  Days before an unanswered request expires", config.CONFIRM_TTL_DAYS)

    _patch_user_config({
        "notify_channels": _yaml_block("notify_channels", chosen),
        "digest_hours": _yaml_block("digest_hours", digest_hours),
        "confirm_ttl_days": [f"confirm_ttl_days: {ttl or 7}"],
    })

    if ask.confirm("\n  Send a test message now? (Y/n): ", default=True):
        import importlib

        importlib.reload(config)
        delivered = False
        for channel in channels.enabled(chosen):
            if channel.send_message("<b>resume-tailor</b> — setup test. "
                                    "If you can read this, approval requests will arrive here."):
                print(f"    sent via {channel.name}")
                delivered = True
            else:
                print(f"    {channel.name} did not deliver — {channel.status_line()}")
        if not delivered:
            print("    Nothing delivered. The local inbox still has it, and `run.py inbox`"
                  " reads the queue with no channel at all.")
    return True


# ── the wizard ──────────────────────────────────────────────────────────────

_STAGE_FUNCS = {
    "environment": stage_environment,
    "resume": stage_resume,
    "identity": stage_identity,
    "search": stage_search,
    "sources": stage_sources,
    "notifications": stage_notifications,
}


def run_wizard(only=None):
    """Walk the stages. Returns the exit code.

    A stage that reports failure stops the walk, because the stages after it
    depend on what it was supposed to write — there is no point asking which job
    sources to poll when the experience database they would be filtered against
    does not exist.
    """
    # Every default here is an empty value, so "no terminal, use the default" —
    # the right behaviour everywhere else in this project — would answer every
    # question with nothing and report success. See ask.require_answers.
    ask.require_answers()

    if only:
        if only not in _STAGE_FUNCS:
            print(f"Unknown stage {only!r}. One of: {', '.join(STAGES)}")
            return 2
        return 0 if _STAGE_FUNCS[only]() else 1

    print("resume-tailor setup\n")
    print("Six stages. Every question can be skipped, and skipping is a real answer —")
    print("an axis you say nothing about is one no posting is screened on.")
    print("Re-run any part later with:  python run.py setup --only <stage>")
    print(f"Stages: {', '.join(STAGES)}")

    for name in STAGES:
        if not _STAGE_FUNCS[name]():
            print(f"\nStopped at the '{name}' stage. Fix what it reported, then:")
            print(f"  python run.py setup --only {name}")
            return 1

    print(f"\n{'─' * 72}\nDone. Checking the result:\n{'─' * 72}")
    return _finish()


def _finish():
    """Run doctor, then name the first thing worth doing.

    Doctor runs as a subprocess, not as a call: it re-reads user_config.yaml and
    profile.yaml from disk, and this process has stale copies of both — every
    stage above just rewrote them.
    """
    print()
    # Flushed first: this process has buffered output, and without the flush the
    # child's report interleaves ahead of the headings that introduce it.
    sys.stdout.flush()
    code = subprocess.call([sys.executable, os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "run.py"), "doctor"])
    sys.stdout.flush()
    print(f"\n{'─' * 72}")
    if code == 0:
        print("Nothing is missing. Try it on a real posting:\n")
        print("  python run.py tailor --url '<a job posting URL>'")
        if config.TRACKER_SOURCES:
            print("\nOr let it find postings for you:\n")
            print("  python run.py watch        # then: python run.py inbox")
    else:
        print("`doctor` found problems above. Each line names what to do about it.")
    return 0 if code == 0 else 1
