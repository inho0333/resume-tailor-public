"""Who the résumé belongs to, and what they are looking for.

Every prompt in this project judges a posting against *a person*: their
background, their work authorisation, the kinds of role they actually want, the
places they would take a job, and the way their own sentences read. That used to
be hardcoded in fit_gate.py and jd_extractor.py, which meant a second user
silently had their postings judged against someone else's school and visa
status.

It all comes from profile.yaml now. profile.yaml is gitignored; the committed
file is profile.example.yaml, where every field says what it is for and what
breaks when it is wrong.

TWO SHAPES, ONE LOADER. The first version of this file described one kind of
person: an enrolled graduate student hunting an internship. Fields sat at the
top level (`education`, `career_summary`, `target_roles`) and everything else
was inferred — including whether this was an internship search at all, which was
guessed from the substring "intern" in a target role. That guess fires on
"internal audit" and "international tax", and the degree fields had no honest
value for someone twelve years into a career. v2 groups the fields into
`background` / `search` / `market` / `authorization` / `voice` and asks for the
search kind outright. `_normalize` reads either shape, so an existing v1
profile.yaml keeps working unchanged.

Nothing here is read at import time. A missing profile.yaml is a clear error at
the point a prompt needs it, not an ImportError, so `run.py doctor` can report
it as one item on a checklist.
"""

import os
import re

import yaml

PROFILE_PATH = os.environ.get("RESUME_TAILOR_PROFILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "profile.yaml")

EXAMPLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "profile.example.yaml")


class ProfileError(RuntimeError):
    """profile.yaml is missing or unusable. The message is the instruction."""


# Used when profile.yaml gives no style_examples. Deliberately fictional: the
# model is shown these as "this is the shape of a good bullet", and if they were
# real sentences from another person, their numbers would leak into your résumé.
_GENERIC_STYLE_EXAMPLES = (
    "Migrated billing from a monthly to a weekly cycle for 12,000 merchants, after churn "
    "interviews showed payout speed drove attrition, by modelling the working-capital "
    "impact and rebuilding settlement to carry corrections, cutting merchant churn 18%",
    "Launched a self-service returns flow after support logs showed 60% of tickets were "
    "one of three questions, coordinating support, logistics and two engineering teams to "
    "ship in a single quarter, removing 4,000 tickets a month",
    "Rebuilt onboarding around a single verification step after funnel analysis found "
    "users dropping at a document upload most of them did not need, lifting activation 11% "
    "with no change to approval rates",
)

# What kind of person is searching. Decides how a prompt names them, which
# fields `doctor` insists on, and who the recruiter persona compares them with.
BACKGROUND_KINDS = ("enrolled_student", "recent_graduate", "experienced", "career_changer")

# What kind of posting they want. Was inferred from substrings; now stated.
SEARCH_KINDS = ("internship", "new_grad", "full_time", "contract")

_SEARCH_PHRASES = {
    "internship": "internship postings",
    "new_grad": "new-graduate postings",
    "full_time": "full-time postings",
    "contract": "contract postings",
}

_APPLICANT_KINDS = {
    "internship": "internship applicant",
    "new_grad": "new-graduate applicant",
    "full_time": "applicant",
    "contract": "contract applicant",
}

WORK_ARRANGEMENTS = ("remote", "hybrid", "onsite")


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        value = [value]
    return [str(v).strip() for v in value if str(v).strip()]


def _lower_list(value):
    return [v.lower() for v in _as_list(value)]


def _normalize(data):
    """Accept either the v1 flat shape or the v2 grouped shape.

    v1 is still what the first users of this tool have on disk, and a profile is
    the one file nobody wants to retype. Rather than migrate it on read (which
    would rewrite a file the user owns), both shapes are read into the same
    attributes. `run.py setup` writes v2.
    """
    data = data or {}
    out = {}

    background = data.get("background") or {}
    search = data.get("search") or {}
    market = data.get("market") or {}
    authorization = data.get("authorization") or {}
    voice = data.get("voice") or {}
    is_v2 = bool(background or search or market or authorization or voice)

    out["name"] = (data.get("name") or "").strip()

    # ── background ──────────────────────────────────────────────────────────
    education = background.get("education") or data.get("education") or {}
    out["school"] = (education.get("school") or "").strip()
    out["program"] = (education.get("program") or "").strip()
    out["graduation"] = str(education.get("graduation") or "").strip()
    # v1 spelled this education.degree_is_technical; v2 drops the prefix inside
    # a block already called education.
    out["degree_is_technical"] = bool(
        education.get("is_technical", education.get("degree_is_technical", False)))
    out["education_completed"] = bool(education.get("completed", False))

    out["career_summary"] = (background.get("summary")
                             or data.get("career_summary") or "").strip()
    out["employers"] = _as_list(background.get("employers") or data.get("employers"))

    years = background.get("years_experience")
    try:
        out["years_experience"] = int(years) if years not in (None, "") else None
    except (TypeError, ValueError):
        out["years_experience"] = None

    kind = (background.get("kind") or "").strip().lower()
    if kind not in BACKGROUND_KINDS:
        # v1 said nothing about this. A profile with a graduation date and no
        # completed flag is the student it was written for; one with years of
        # experience and no programme is not.
        if out["years_experience"] and not out["program"]:
            kind = "experienced"
        elif out["program"] and not out["education_completed"]:
            kind = "enrolled_student"
        elif out["program"]:
            kind = "recent_graduate"
        else:
            kind = "experienced" if out["years_experience"] else ""
    out["background_kind"] = kind

    # ── search ──────────────────────────────────────────────────────────────
    out["target_roles"] = _lower_list(search.get("target_roles") or data.get("target_roles"))
    out["avoid_roles"] = _lower_list(search.get("avoid_roles") or data.get("avoid_roles"))
    out["target_term"] = (search.get("term") or data.get("target_term") or "").strip()
    out["seniority"] = _lower_list(search.get("seniority"))
    out["locations"] = _as_list(search.get("locations"))
    out["relocate"] = bool(search.get("relocate", False))
    out["dealbreakers"] = _as_list(search.get("dealbreakers"))

    arrangement = _lower_list(search.get("work_arrangement"))
    out["work_arrangement"] = [a for a in arrangement if a in WORK_ARRANGEMENTS]

    search_kind = (search.get("kind") or "").strip().lower()
    if search_kind not in SEARCH_KINDS:
        if is_v2:
            # v2 asked outright and got nothing back. "unset" has a meaning here
            # — do not reason about employment type — so do not guess.
            search_kind = ""
        else:
            # v1's inference, preserved exactly so an existing profile keeps the
            # judgments it has been getting.
            term = out["target_term"].lower()
            looks_internship = (any("intern" in r for r in out["target_roles"])
                                or "summer" in term or "intern" in term)
            search_kind = "internship" if looks_internship else ""
    out["search_kind"] = search_kind

    floor = search.get("comp_floor")
    out["comp_floor"] = None
    if isinstance(floor, dict):
        try:
            amount = float(floor.get("amount"))
        except (TypeError, ValueError):
            amount = None
        if amount:
            out["comp_floor"] = {
                "amount": amount,
                "currency": (str(floor.get("currency") or "USD")).strip().upper(),
                "period": (str(floor.get("period") or "year")).strip().lower(),
            }

    # ── market ──────────────────────────────────────────────────────────────
    # Two letters, uppercase, or empty. Drives which sponsorship wording is
    # searched for, which news locale is queried, and which city names are
    # treated as noise in keyword extraction.
    country = (market.get("country") or "").strip().upper()
    out["market_country"] = country if re.fullmatch(r"[A-Z]{2}", country) else ""

    # ── authorization ───────────────────────────────────────────────────────
    out["work_authorization"] = (authorization.get("statement")
                                 or data.get("work_authorization") or "").strip()
    out["citizenship_requirements_block"] = bool(
        authorization.get("citizenship_requirements_block",
                          data.get("citizenship_requirements_block", True)))
    out["sponsorship_needed_later"] = bool(
        authorization.get("sponsorship_needed_later",
                          data.get("sponsorship_needed_later", False)))

    # ── voice ───────────────────────────────────────────────────────────────
    out["style_examples"] = _as_list(voice.get("style_examples") or data.get("style_examples"))

    return out


class Profile:
    """The person this résumé is for. Read from profile.yaml, never from code."""

    def __init__(self, data, source=PROFILE_PATH):
        self.source = source
        fields = _normalize(data)
        for key, value in fields.items():
            setattr(self, key, value)

    # ── prompt fragments ────────────────────────────────────────────────────
    #
    # These are the only place the pipeline is allowed to state facts about you.
    # A field left empty in profile.yaml drops its line, and the prompt then
    # does not reason about it at all — which is the right behaviour: a model
    # told nothing about your visa says nothing about your visa.

    @property
    def degree_phrase(self):
        """'an MBA' / 'an MS Computer Science' / '' when no programme is given."""
        if not self.program:
            return ""
        return f"{self._article_for(self.program)} {self.program}"

    @property
    def effective_years(self):
        """Years of experience to screen a posting's requirement against.

        `years_experience` is left empty for a student or a new graduate, which
        is the honest value — and it meant the years check did not run for them
        at all, because it needs a number on both sides. So the group most
        likely to be mismatched on experience was the one group with no
        protection: a posting requiring eight years came back with nothing
        conflicting, ranked alongside the graduate schemes.

        A student or new graduate is screened as zero. Anyone else with no
        number set is screened not at all, since "I did not say" is different
        from "none".
        """
        if self.years_experience:
            return self.years_experience
        if self.background_kind in ("enrolled_student", "recent_graduate"):
            return 0
        return None

    @property
    def experience_phrase(self):
        """'8 years of experience' / '' when the years are not given."""
        if not self.years_experience:
            return ""
        unit = "year" if self.years_experience == 1 else "years"
        return f"{self.years_experience} {unit} of experience"

    @property
    def degree_label(self):
        """How a prompt introduces the applicant.

        One sentence fragment, and the single most load-bearing one in the file:
        it opens the eligibility screen and the pre-fit prompt. v1 rendered
        everyone as a student, down to a literal "a graduate student" fallback
        for a profile with no programme — so a twelve-year engineer was screened
        as a student with an unnamed degree.
        """
        kind = self.background_kind
        bits = []
        if kind == "enrolled_student" and self.program:
            bits.append(f"{self.degree_phrase} student")
            if self.school:
                bits.append(f"at {self.school}")
            if self.graduation:
                bits.append(f"graduating {self.graduation}")
        elif kind == "recent_graduate" and self.program:
            bits.append(f"{self.degree_phrase} graduate")
            if self.school:
                bits.append(f"of {self.school}")
            if self.graduation:
                bits.append(f"({self.graduation})")
        else:
            if self.experience_phrase:
                bits.append(f"a candidate with {self.experience_phrase}")
            else:
                bits.append("a candidate")
            if self.program:
                holds = "holding" if self.education_completed or kind != "enrolled_student" \
                    else "studying for"
                bits.append(f"{holds} {self.degree_phrase}")
                if self.school:
                    bits.append(f"from {self.school}")
            if kind == "career_changer":
                bits.append("moving into a new field")
        return " ".join(bits)

    @property
    def degree_kind_note(self):
        """How to read degree restrictions in a posting, for this degree."""
        if not self.program:
            return ("The applicant's profile names no degree programme, so treat a degree "
                    "requirement as unclear rather than satisfied or failed.")
        if self.degree_is_technical:
            return ("The applicant's degree is a technical one, so a posting asking for a "
                    "technical or engineering degree is not by itself disqualifying.")
        return ("The applicant's degree is NOT a technical or engineering degree. A posting "
                "that requires one (\"technical degree\", \"computer science\", "
                "\"engineering\", \"STEM\") does not literally match.")

    def applicant_block(self):
        """The '[Applicant]' block of the pre-fit prompt."""
        lines = [f"- {self.degree_label}."]
        if self.work_authorization:
            lines.append(f"- Work authorisation: {self.work_authorization}")
        if self.career_summary:
            lines.append(f"- Career so far: {self.career_summary}")
        elif self.employers:
            lines.append(f"- Career so far: {', '.join(self.employers)}")
        if self.target_roles:
            target = ", ".join(self.target_roles)
            term = f"{self.target_term} " if self.target_term else ""
            lines.append(f"- Looking for: {term}roles in {target}.")
        for line in self.requirement_lines():
            lines.append(f"- {line}")
        return "\n".join(lines)

    @property
    def sponsorship_exclusion_matters(self):
        """Does "we do not sponsor" affect this applicant?

        The scan that finds that sentence is deliberately country-neutral and
        runs for everyone, because a posting whose real disqualifier is one line
        of boilerplate is too expensive to miss. Whether the line *matters* is a
        different question, and it is one this file can answer.

        It does not matter to someone who has stated a status and said they will
        need no sponsorship later: that wording appears in a large share of
        postings, and flagging all of them for a citizen is the kind of noise the
        batched digest exists to remove. It does matter when they will need
        sponsorship, and when they have said nothing at all — an empty statement
        means "do not reason about my status", which is not the same as "it is
        fine".
        """
        if not self.work_authorization:
            return True
        return bool(self.sponsorship_needed_later)

    def requirement_lines(self):
        """The applicant's own hard requirements, one line each.

        fit_gate checks these in code, not with a model (see
        fit_gate.check_hard_axes) — a location mismatch is arithmetic, not
        judgment. They are still stated in the prompt so the model's `reason`
        does not recommend a posting the code is about to cap.
        """
        lines = []
        if self.search_kind:
            lines.append(f"Wants: {_SEARCH_PHRASES[self.search_kind]}.")
        if self.seniority:
            lines.append(f"Target level: {', '.join(self.seniority)}.")
        if self.locations:
            where = ", ".join(self.locations)
            move = "willing to relocate" if self.relocate else "not willing to relocate"
            lines.append(f"Locations: {where} ({move}).")
        if self.work_arrangement:
            lines.append(f"Work arrangement: {', '.join(self.work_arrangement)}.")
        if self.comp_floor:
            floor = self.comp_floor
            lines.append(f"Compensation floor: {floor['currency']} "
                         f"{floor['amount']:,.0f} per {floor['period']}.")
        if self.dealbreakers:
            lines.append(f"Will not take: {', '.join(self.dealbreakers)}.")
        return lines

    @property
    def targets_internship(self):
        """Is this an internship search?

        v1 guessed from the substring "intern" in a target role, which made
        "internal audit" and "international tax" internship searches. v2 reads
        search.kind; a v1 profile still gets the old guess, in `_normalize`.
        """
        return self.search_kind == "internship"

    @property
    def search_phrase(self):
        """'internship postings' — what the screening prompts say they screen."""
        return _SEARCH_PHRASES.get(self.search_kind, "job postings")

    @property
    def applicant_phrase(self):
        """'an MBA internship applicant' — how a prompt names the person it writes for.

        The rewrite, review and polish prompts used to say "an MBA internship applicant"
        literally. For anyone who is not doing an MBA internship search that is a prompt
        describing a different person, on the calls where quality actually matters.
        """
        kind = _APPLICANT_KINDS.get(self.search_kind, "applicant")
        if self.background_kind in ("enrolled_student", "recent_graduate") and self.program:
            return f"{self._article_for(self.program)} {self.program} {kind}"
        if self.experience_phrase:
            return f"an {kind} with {self.experience_phrase}" if kind[0] in "aeiou" \
                else f"a {kind} with {self.experience_phrase}"
        return f"an {kind}" if kind[0] in "aeiou" else f"a {kind}"

    @property
    def peer_pool_phrase(self):
        """Who else is in the pile, for the recruiter persona.

        The recruiter's whole job in that prompt is comparison, so the comparison set has to
        be the applicant's real one. "MS Computer Science students from strong programmes" is
        the wrong pile for someone eight years into a career — and it was what v1 produced for
        anyone who filled in a programme, because `doctor` asked them to.
        """
        if self.background_kind == "enrolled_student" and self.program:
            return f"{self.program} students from strong programmes"
        if self.background_kind == "recent_graduate" and self.program:
            return f"recent {self.program} graduates from strong programmes"
        if self.experience_phrase:
            return f"applicants with roughly {self.experience_phrase} in comparable roles"
        return "strong applicants for this role"

    @staticmethod
    def _article_for(phrase):
        """'MBA' -> 'an' (em-bee-ay), 'Master of Public Policy' -> 'a'. An initialism takes its
        article from how the letter is read aloud, a word from its first sound."""
        first = phrase.split()[0]
        if first.isupper() and len(first) <= 4:
            return "an" if first[0] in "AEFHILMNORSX" else "a"
        return "an" if first[0].lower() in "aeiou" else "a"

    def eligibility_block(self):
        """The applicant description in the degree/authorisation screen."""
        lines = [f"The applicant is {self.degree_label}.", self.degree_kind_note]
        if self.experience_phrase:
            lines.append(f"The applicant has {self.experience_phrase}.")
        if self.work_authorization:
            lines.append(f"Work authorisation: {self.work_authorization}")
            if self.citizenship_requirements_block:
                lines.append(
                    "Treat a generic \"we do not sponsor\" line as NOT disqualifying, but an "
                    "explicit citizenship requirement (\"citizens only\", a national-security "
                    "or export-control residency status, a security clearance) as "
                    "disqualifying: set visa_caution true only in that case.")
        return "\n".join(lines)

    def style_blob(self):
        """Reference sentences shown to the model as the target voice.

        Yours if profile.yaml has style_examples, otherwise fictional ones. Never
        another real person's.
        """
        examples = self.style_examples or list(_GENERIC_STYLE_EXAMPLES)
        return "\n".join(examples) + "\n"

    def uses_generic_style(self):
        return not self.style_examples

    # ── role matching ───────────────────────────────────────────────────────

    @staticmethod
    def _to_pattern(phrase):
        """'product manager' -> r'product\\s+manager' (whitespace-tolerant)."""
        return r"\s+".join(re.escape(w) for w in phrase.split())

    def target_role_patterns(self):
        return tuple(self._to_pattern(r) for r in self.target_roles)

    def avoid_role_patterns(self):
        return tuple(self._to_pattern(r) for r in self.avoid_roles)

    # ── completeness ────────────────────────────────────────────────────────

    def missing_fields(self):
        """Fields whose absence measurably degrades the output.

        What counts as missing depends on who is searching. v1 listed
        education.program and education.graduation unconditionally, so someone
        twelve years into a career carried two permanent `doctor` warnings for
        not being enrolled anywhere — and the documented way to clear them was
        to fill in a programme, which then told the recruiter prompt the pile was
        made of students.
        """
        missing = []
        if not self.background_kind:
            missing.append("background.kind")
        if self.background_kind in ("enrolled_student", "recent_graduate"):
            if not self.program:
                missing.append("background.education.program")
            if not self.graduation:
                missing.append("background.education.graduation")
        elif self.background_kind in ("experienced", "career_changer"):
            if not self.years_experience:
                missing.append("background.years_experience")
        if not self.search_kind:
            missing.append("search.kind")
        if not self.target_roles:
            missing.append("search.target_roles")
        if not self.style_examples:
            missing.append("voice.style_examples (falling back to generic examples)")
        return missing

    # Example values that make every judgment be about a different person: who you are, where
    # you study, what you have done, and which roles you are hunting. A run in this state is
    # not degraded, it is wrong, so `run.py doctor` treats these as blocking.
    #
    # target_roles is here rather than in PREFERENCE_FIELDS because of what it does: a posting
    # whose title matches one can never be tiered low, and nothing else can be tiered high
    # (fit_gate._clamp). Left at the example's product/strategy list it silently applies a
    # stranger's search to every posting, and a warning was not enough to make anyone edit it.
    IDENTITY_FIELDS = (("name", "name"),
                       ("school", "background.education.school"),
                       ("career_summary", "background.summary"),
                       ("employers", "background.employers"),
                       ("target_roles", "search.target_roles"))

    # The subset with no legitimate empty value. Staleness and emptiness are not
    # the same question, and conflating them blocked a real setup: someone with
    # no degree they wanted considered left the school empty — which
    # profile.example.yaml documents as supported — and the tool refused to run.
    #
    # The others are all honestly empty for somebody. A school, for anyone who
    # does not want a degree weighed. `employers`, for a first job (the career
    # summary is what the fit judgment actually reads). `target_roles`, for
    # anyone who would rather the model's reading stood alone — the cost is that
    # no posting can reach "strong fit", which is a warning, not a fault.
    #
    # An empty NAME and an empty SUMMARY are different: the summary is the only
    # place the fit judgment learns what this person has done, and without it
    # every judgment is about nobody.
    REQUIRED_IDENTITY_FIELDS = (("name", "name"),
                                ("career_summary", "background.summary"))
    # Example values that cost quality but describe no false fact: the fallback voice.
    PREFERENCE_FIELDS = (("style_examples", "voice.style_examples"),)

    def unedited_fields(self, example=None, fields=None):
        """Fields still identical to profile.example.yaml.

        Copying the example and forgetting to edit it is silent, and it is the
        one mistake this whole file exists to prevent: the prompts would then
        judge every posting against a fictional person's degree and describe
        someone else's bullets as your voice.

        fields: which group to check (default: both). `run.py doctor` asks for
        IDENTITY_FIELDS and PREFERENCE_FIELDS separately so it can fail on the
        first and merely warn about the second.
        """
        try:
            example = example or load(path=EXAMPLE_PATH, required=False)
        except ProfileError:
            return []
        stale = []
        for field, label in (fields or (self.IDENTITY_FIELDS + self.PREFERENCE_FIELDS)):
            mine, theirs = getattr(self, field), getattr(example, field)
            if mine and theirs and mine == theirs:
                stale.append(label)
        return stale

    def unedited_identity_fields(self, example=None):
        return self.unedited_fields(example=example, fields=self.IDENTITY_FIELDS)

    def empty_identity_fields(self):
        """Identity fields with nothing in them at all.

        The staleness check above catches "copied the example and never edited
        it". This catches the other half, which the wizard made the more likely
        one: a profile that was written but never answered. An empty string does
        not equal the example's value, so `unedited_identity_fields` returns
        nothing for it — and a `profile.yaml` whose name and career summary were
        both `''` once passed `doctor` with a single warning, after which the
        wizard printed "Nothing is missing."

        Only REQUIRED_IDENTITY_FIELDS count here: see the comment there for why
        an empty school is a supported answer and an empty summary is not.
        """
        return [label for field, label in self.REQUIRED_IDENTITY_FIELDS
                if not getattr(self, field)]

    def blocking_identity_problems(self):
        """(empty, stale) — the two ways an identity can be unusable.

        One call for both, because every caller needs both and the fix differs:
        an empty field has never been answered, a stale one is a stranger's.
        """
        return self.empty_identity_fields(), self.unedited_identity_fields()

    def unedited_preference_fields(self, example=None):
        return self.unedited_fields(example=example, fields=self.PREFERENCE_FIELDS)


def load(path=None, required=True):
    """Read profile.yaml.

    required=False returns an empty Profile instead of raising, for callers that
    want to report the problem themselves (`run.py doctor`).
    """
    path = path or PROFILE_PATH
    if not os.path.exists(path):
        if not required:
            return Profile({}, source=path)
        raise ProfileError(
            f"profile.yaml not found at {path}.\n"
            "  python run.py setup          # the wizard writes it, and asks only what it needs\n"
            f"  cp {os.path.basename(EXAMPLE_PATH)} profile.yaml   # or fill it in by hand\n"
            "It holds your background, work authorisation and what you are looking for. "
            "Without it the prompts would have to guess who you are.")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ProfileError(f"profile.yaml is not valid YAML ({path}): {e}") from e
    if not isinstance(data, dict):
        raise ProfileError(f"profile.yaml must be a mapping of fields, got {type(data).__name__}")
    return Profile(data, source=path)


_cached = None


def get():
    """Process-wide profile. Loaded once, on first use."""
    global _cached
    if _cached is None:
        _cached = load()
    return _cached


def reset_cache():
    """For tests, which load profiles from fixtures."""
    global _cached
    _cached = None
