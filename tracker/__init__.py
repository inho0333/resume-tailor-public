"""Finding the postings in the first place.

The rest of this project starts from a posting you already found: a URL, a file,
or text pasted into the terminal. This package is the half in front of that —
it watches the places postings appear, filters them against your own profile,
and hands the survivors to the same approval queue a `tailor --auto` run uses.

Nothing here submits an application, logs into an account on your behalf, or
scrapes a site that forbids it. Every implemented source is either a public,
unauthenticated API or a service you hold your own credentials for.

## The shape

    sources/*.py  →  Posting  →  relevance.py  →  dedupe.py  →  confirm_state
                                 (is this even                 (the approval
                                  about my career?)             queue)

A source does one thing: return the postings it can see right now. Everything
after that is shared, so adding a source is one file and one line in SOURCES.
See docs/tracker.md.

## Why `__init__` imports nothing at module level

`jd_extractor` imports `tracker.jsonld`, and `tracker.jd_lookup` imports
`jd_extractor`. Importing submodules here would close that loop and leave a
half-initialised module on the import path. The registry is therefore resolved
on first use.
"""

# Display order everywhere: `run.py setup`, `run.py doctor`, and `watch`. It is
# the recommended order to adopt them in, not an internal priority — see
# tracker/CHANNELS.md for what each one costs and covers.
SOURCE_NAMES = ("trackly", "greenhouse", "lever", "ashby", "apple", "remotive",
                "linkedin")

_REGISTRY = None


def registry():
    """{name: Source subclass}, resolved on first use.

    Not called `sources`: importing `tracker.sources` binds that name on this
    module as the submodule, which would shadow a function of the same name.
    """
    global _REGISTRY
    if _REGISTRY is None:
        from tracker.sources import (ats_apple, ats_ashby, ats_greenhouse, ats_lever,
                                     linkedin, remotive, trackly)
        _REGISTRY = {
            "trackly": trackly.Source,
            "greenhouse": ats_greenhouse.Source,
            "lever": ats_lever.Source,
            "ashby": ats_ashby.Source,
            "apple": ats_apple.Source,
            "remotive": remotive.Source,
            "linkedin": linkedin.Source,
        }
    return _REGISTRY


def build(name, settings=None):
    """One configured source by name. None when the name is not registered."""
    factory = registry().get(name)
    if factory is None:
        return None
    return factory(settings or {})
