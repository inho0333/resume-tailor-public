# What you are looking for

Stage 3, the half that is about you rather than about the posting. Every axis
here lives in `profile.yaml`'s `search` block, and every one is optional.

**Empty means that axis is not screened.** Not "screened loosely", not "screened
with a guess" — not screened. An empty `locations` list filters no posting on
where the job is; an empty `comp_floor` never compares pay. This is the rule the
whole file is built on, because the alternative is a filter nobody set that
quietly removes postings.

## Why there are axes at all

The first version of this tool judged fit with one question — "how much of what
this posting requires can this experience evidence?" — and one clamp: does a
`target_roles` string appear in the job title.

Two things went wrong with that, and both were invisible.

A posting in the wrong country, at the wrong level, for the wrong employment
type, or paying under half what you would accept arrived looking exactly like a
good one, because none of those facts was extracted and none of them was
compared. The digest did not even show the location.

And `high` was unreachable unless a target role appeared literally in the title.
That rule was added for a measured reason — small models hand out "high" freely,
and on one batch of 24 postings two operations-analyst internships both came
back "high" while every posting of that kind expired unanswered. But it means
anyone whose next job is not titled the way their last one was had every posting
flattened to "possible fit", and the digest's ordering carried no information at
all. Career-changers got no signal from the tool, which is who needs it most.

## Two kinds of judgment, kept apart

**Arithmetic is code.** Whether a posting is in a city you would move to, pays
above your floor, wants eight years when you have three, or is an internship
when you want a full-time role: `fit_gate.check_hard_axes` decides that, with no
model involved. Each mismatch is reported **by name** in the digest, and caps
the tier — one caps it at "possible fit", two or more force "weak fit".

**Evidence coverage is the model's.** "How much of this posting can this career
actually evidence" is a reading, a model does it well, and it is the only
question the fit prompt asks.

Mixing the two was the mistake. A model asked to weigh a location mismatch
alongside everything else quietly discounts it, and the result is a confident
sentence about a job in the wrong hemisphere.

## The axes

### `kind` — internship | new_grad | full_time | contract

What kind of posting you want. **State it.** The first version inferred it from
the substring `intern` appearing in a target role, which makes "internal audit"
and "international tax" internship searches.

Compared against the employment type the posting states, and only genuine
conflicts count: a new-graduate search is served by a full-time posting, and a
full-time search by one labelled "new grad".

### `term`

Internship searches only: `Summer 2027`. Shown to the model so it does not offer
you a posting for the wrong year. `doctor` warns if `kind: internship` has no
term.

### `target_roles`

The roles you want, lowercase, as they appear in job titles, one concept each.
Short fragments match more titles: `product manage` catches "Product Management
Intern" and "Product Manager, Payments".

These do more work than anything else in the file:

- a posting whose **title** matches one can never be tiered "weak fit" — missing
  a posting you wanted costs more than one noisy notification
- `high` needs the model and these to agree. The title is the strong signal; a
  target role found in the posting's **responsibilities or requirements** also
  counts, which is what unblocks a career-changer whose next title is new
- they are the first thing the filter in the tracker reads

`doctor` **fails** if this is still the example's list. That is not pedantry:
left alone, it applies a stranger's job search to every posting you see, and a
warning was not enough to make anyone change it.

### `avoid_roles`

Roles you keep declining. A title match is capped at "possible fit" and sinks in
the digest — but is still shown. Nothing is ever hidden from you outright.

Leave it empty on day one. Fill it in after a week of real notifications;
`run.py decisions` prints what you have actually been skipping.

### `seniority`

Levels as postings title them: `[senior, staff]`. Empty means any level. A
posting more than two steps away on `intern → new grad → junior → associate →
mid → senior → staff → principal → director` is reported as a mismatch.

### `years_experience` (in `background`, not `search`)

Half of the years check. A posting stating "8+ years required" against a profile
saying 3 is reported as a mismatch instead of being quietly tiered "possible
fit". Two years of tolerance, because postings round their own requirement up.

**A student or recent graduate screens as zero, not as unknown.** This field is
honestly empty for them, and that meant the years check did not run for them at
all — so the group most likely to be mismatched on experience was the only group
with no protection, and a posting asking for eight years came back with nothing
conflicting. Anyone else who leaves it empty is still not screened on years:
"I did not say" is different from "none".

**Self-contradiction is not believed.** A posting titled for an intern that asks
for eight years in its requirements block is boilerplate carried over from
another template — the requirement is noted as not believed and does not count
as a mismatch. The degree axis has had this rescue for a while, for the same
reason and after the same kind of measured failure; this is the version of it
that matters to an experienced-hire search.

### `locations`

Places you would take a job, written as a person would: `San Francisco Bay
Area`, `London`. Matched loosely in both directions, so "London" matches
"London, GB" and "San Francisco Bay Area" is matched by a posting in San
Francisco.

A **remote** posting counts as being in every location you named.

Empty means anywhere.

### `relocate`

Only consulted when `locations` is non-empty. `false` means a posting elsewhere
is reported as a location mismatch; `true` means it is not.

### `work_arrangement`

Which of `remote`, `hybrid`, `onsite` you would accept. Empty means all three.

Only compared when the posting says. Note that schema.org has no way to express
"hybrid", so that value comes from the model reading the prose rather than from
the page's structured data.

### `comp_floor`

`{amount: 180000, currency: USD, period: year}`. Compared against the **top** of
a posting's stated range, and only when the currencies match — a cross-currency
comparison would need rates nobody here should be guessing. Periods are
normalised, so an hourly range is annualised before it is compared.

Empty means pay is never considered, which is the right setting for most
internship searches: those postings rarely state a range.

A range that comes from the page's own schema.org block is a published number. A
range read out of prose by a model is not, and is shown to you rather than
compared.

### `dealbreakers`

Conditions you will not take, in your own words: `shift work`, `field
operations`, `five days on site`, `on-call rotation`. They go into the fit prompt
as what "not what the applicant is looking for" means.

This field replaced a hardcoded list. The fit prompt used to name shift work,
field operations and pure administration as examples of a bad posting — one
person's taste, shipped to everyone, including people searching for exactly the
operations roles it quietly down-ranked.

### `market.country`

Two letters: `US`, `GB`, `DE`, `CA`, `SG`. Three effects:

1. It adds that country's own immigration wording to the sponsorship scan. An
   employer in the UK writes "we cannot offer a Certificate of Sponsorship", not
   "H-1B", and a scan carrying only US phrasings reports that posting as clean.
   Covered today: `US`, `GB`, `CA`, `SG`, `AU`, and the EU/EEA permit wording
   shared by `DE`, `NL`, `IE`, `FR`, `ES`, `IT`, `SE`, `DK`, `FI`, `PL`, `AT`,
   `BE`, `PT`. Adding one is a few patterns, their trigger words, and one
   sentence in `tests/test_search_axes.py` proving it fires — the test exists
   because a market's patterns and its triggers have to agree, and when they
   did not, the dead patterns looked exactly like postings with nothing to find.
2. It sets the locale company news is read in, which is how a regional
   employer's coverage gets found at all.
3. It marks that country's city names as noise rather than ATS keywords.

Leaving it empty costs you only the country-specific phrasings. The
country-neutral scan — "we do not sponsor", "no visa sponsorship" — always runs,
because a posting whose real disqualifier is that one sentence is too expensive
to miss.

Whether a match is **shown** to you is a separate question, answered by
`authorization`: with a stated status and `sponsorship_needed_later: false`, that
sentence warns about nothing and is noted rather than flagged. It appears in a
large share of postings, and a ⚠️ on all of them for a citizen is the noise the
batched digest exists to remove. That is not a hypothetical either: a local model read straight past
*"must be authorized to work without the need for current or future company
sponsorship"* and reported no concern, and that sentence was the whole reason
the application could go nowhere.

## What is deliberately not here

**Weights.** There is one tier out — strong / possible / weak — and no per-axis
scoring. A weighted model of your own preferences sounds better than it reads:
the useful output of this stage is "this posting is in Berlin and you said
London", not "0.62".

**Industry and company stage.** No axis, by choice. Both are better served by
`target_roles` and `avoid_roles`, which are matched against the posting's own
words, and by `dealbreakers`, which is matched against your own.

**Anything that hides a posting.** Every mismatch sorts a posting lower and
names its reason. The only thing in the whole pipeline that drops a posting
without telling you is `tracker.block_titles` / `block_companies`, which ships
empty and is yours to fill.

## Requirements no résumé edit can satisfy

Separate from the axes above, and reported rather than screened on: a security
clearance, a professional or driving licence, a certification, and a citizenship
or export-control residency bar (`U.S. Person`, ITAR, "citizens only").

These are counted by regex for the same reason sponsorship is. They read as
ordinary boilerplate, so a model weighs them as one more preferred
qualification — and a posting that merely *prefers* a clearance is worded the
same as one that *demands* it. You are the one who knows whether you hold it, so
the scan tells you it is there and leaves the decision alone.

A citizenship bar and a sponsorship exclusion are deliberately not the same
thing. A sponsorship line can close the long-term path while the role itself
stays open to you; "U.S. Person as defined by ITAR" closes the role outright.
