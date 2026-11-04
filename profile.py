"""Who the résumé belongs to.

Every prompt in this project judges a posting against *a person*: their degree,
their graduation date, their work authorisation, the kinds of role they actually
want, and the way their own sentences read. That used to be hardcoded in
fit_gate.py and jd_extractor.py, which meant a second user silently had their
postings judged against someone else's school and visa status.

It all comes from profile.yaml now. profile.yaml is gitignored; the committed
file is profile.example.yaml, where every field says what it is for and what
breaks when it is wrong.

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
    "interviews showed payout speed was the top reason for leaving, by modelling the "
    "working-capital impact and rebuilding settlement to carry corrections forward, "
    "cutting merchant churn 18%",
    "Launched a self-service returns flow after support logs showed 60% of tickets were "
    "one of three questions, coordinating support, logistics and two engineering teams to "
    "ship in a single quarter, removing 4,000 tickets a month",
    "Rebuilt onboarding around a single verification step after funnel analysis found "
    "users dropping at a document upload most of them did not need, lifting activation 11% "
    "with no change to approval rates",
)


class Profile:
    """The person this résumé is for. Read from profile.yaml, never from code."""

    def __init__(self, data, source=PROFILE_PATH):
        data = data or {}
        self.source = source
        self.name = (data.get("name") or "").strip()

        education = data.get("education") or {}
        self.school = (education.get("school") or "").strip()
        self.program = (education.get("program") or "").strip()
        self.graduation = str(education.get("graduation") or "").strip()
        self.degree_is_technical = bool(education.get("degree_is_technical", False))

        self.work_authorization = (data.get("work_authorization") or "").strip()
        self.citizenship_requirements_block = bool(
            data.get("citizenship_requirements_block", True))
        self.sponsorship_needed_later = bool(data.get("sponsorship_needed_later", False))

        self.employers = [str(e).strip() for e in (data.get("employers") or []) if str(e).strip()]
        self.target_roles = [str(r).strip().lower()
                             for r in (data.get("target_roles") or []) if str(r).strip()]
        self.avoid_roles = [str(r).strip().lower()
                            for r in (data.get("avoid_roles") or []) if str(r).strip()]
        self.career_summary = (data.get("career_summary") or "").strip()
        self.target_term = (data.get("target_term") or "").strip()

        self.style_examples = [str(s).strip()
                               for s in (data.get("style_examples") or []) if str(s).strip()]

    # ── prompt fragments ────────────────────────────────────────────────────
    #
    # These are the only place the pipeline is allowed to state facts about you.
    # A field left empty in profile.yaml drops its line, and the prompt then
    # does not reason about it at all — which is the right behaviour: a model
    # told nothing about your visa says nothing about your visa.

    @property
    def degree_label(self):
        """'an MBA student at Example University, graduating 2027-05'."""
        bits = []
        if self.program:
            # "an MBA student", "an MS Computer Science student", "a Master of
            # Public Policy student". An initialism takes its article from how the
            # letter is read aloud ("em-bee-ay"), a word from its first sound.
            first = self.program.split()[0]
            if first.isupper() and len(first) <= 4:
                article = "an" if first[0] in "AEFHILMNORSX" else "a"
            else:
                article = "an" if first[0].lower() in "aeiou" else "a"
            bits.append(f"{article} {self.program} student")
        else:
            bits.append("a graduate student")
        if self.school:
            bits.append(f"at {self.school}")
        if self.graduation:
            bits.append(f"graduating {self.graduation}")
        return " ".join(bits)

    @property
    def degree_kind_note(self):
        """How to read degree restrictions in a posting, for this degree."""
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
        return "\n".join(lines)

    def eligibility_block(self):
        """The applicant description in the degree/authorisation screen."""
        lines = [f"The applicant is {self.degree_label}.", self.degree_kind_note]
        if self.work_authorization:
            lines.append(f"Work authorisation: {self.work_authorization}")
            if self.citizenship_requirements_block:
                lines.append(
                    "Treat a generic \"we do not sponsor\" line as NOT disqualifying, but an "
                    "explicit citizenship requirement (\"U.S. citizens only\", \"U.S. Person\" "
                    "in an ITAR/export-control sense, a security clearance) as disqualifying: "
                    "set visa_caution true only in that case.")
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
        """Fields whose absence measurably degrades the output."""
        missing = []
        if not self.program:
            missing.append("education.program")
        if not self.graduation:
            missing.append("education.graduation")
        if not self.target_roles:
            missing.append("target_roles")
        if not self.style_examples:
            missing.append("style_examples (falling back to generic examples)")
        return missing


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
            f"  cp {os.path.basename(EXAMPLE_PATH)} profile.yaml   # then fill it in\n"
            "It holds your degree, graduation date, work authorisation and target roles. "
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
