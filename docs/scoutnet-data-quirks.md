# Scoutnet project API: data quirks

What the Scoutnet project API actually returns, as opposed to what it looks like
it returns. Written while building the WSJ27 forms decoder against the 2026
registration data, but none of it is WSJ27-specific — expect the same in any
project that reads participants and their form answers.

## Shape

`/questions?id=<project>` returns a **forms index**, not questions:

```json
{"forms": {"47115": {"title": "...", "id": 47115, "endpoint_url": "https://.../questions?form_id=47115"}}}
```

Each form's own endpoint returns `{form, questions, tabs, sections, messages}`.
A project can have several forms; `/participants` returns everyone across all of
them in one dict, so you have to work out which form each member filled in.

A question definition holds exactly:

```json
"88147": {"question": "Laktos", "description": "", "tab_id": 43945,
          "section_id": 20963, "type": "choice", "default_value": "60015",
          "choices": {"60016": {"value": 60016, "option": "1", "description": null}}}
```

`tabs` and `sections` are `id -> {id, title, description}` — and a title is often
the empty string, so tabs and sections are not reliably nameable.

## Answers

Answers arrive as `participant["questions"]`, keyed by question id. The value
encoding does **not** follow the declared `type`:

| what the member did | stored as |
|---|---|
| picked one option | the choice id, as a string: `"60016"` |
| picked several (multi-select) | a list of choice ids: `["63014", "63016"]` |
| typed text / a number | the string, `"3"` or `"många läger utomlands"` |
| a field linked to their Scoutnet profile | a JSON **string**: `'{"linked_id":1984390,"value":"karin@x.se"}'` |
| was shown a question and left it alone | `null`, `""`, `[]` — **or `"1"`, see below** |
| was never shown the question | the id is absent entirely |

The linked-profile encoding (next of kin, own email and phone) appears whatever
the declared type says — `text` and `other_unsupported_by_api` both do it. Parse
any string that looks like a JSON object and take `value`.

That last distinction is worth keeping: **key present but empty** means the
question was rendered and skipped; **key absent** means it was never asked
(a follow-up behind a "Ja", a question only one applicant type sees). Two
different facts, and only the storage tells them apart.

## `type` is lossy

The values seen are `choice`, `text`, `number` and `other_unsupported_by_api` —
that last one is Scoutnet admitting the widget has no API representation. There
is no `boolean`, and `type` does not tell you how a question was rendered: a
1–5 radio scale and a Ja/Nej radio are both plain `choice`. If you need to tell
them apart, inspect the option labels, not the type.

`default_value` is not usable as a default either — on several questions it
points at an id that isn't in that question's own `choices`.

## A bare `"1"` means "shown, not answered"

The one that cost the most to work out. Some choice answers are stored as `"1"`,
which matches no option in the form definition. It was the *only* unmappable
value in 2785 participants — 791 occurrences over 35 questions.

It means **the radio was displayed and left untouched**. Two independent
confirmations:

- The same UI is encoded differently by two forms in the same project. In the
  allergen grid (fifteen allergens, each a 1–5 scale, revealed by answering
  "Ja"), the leaders' form writes `null` for an untouched row and never a `"1"`;
  the participant form writes `"1"`. Both then show the identical human pattern —
  one real severity beside fourteen untouched rows: 53 members as
  "1 real + 14 null", 25 as "14 ones + 1 real".
- Tested on a live form: answer "Ja", save without picking any severity, and all
  fifteen rows come back `null`.

It is **not** a checkbox tick and **not** severity 1. Corroborating evidence:
on Ja/Nej questions, members whose answer is `"1"` never fill the follow-up
detail field (0 of 11), where a genuine "Ja" fills it 100% of the time
(143/143, 327/327, 496/496, 64/64); and members switching a gate question from
Ja back to Nej leave all fifteen rows behind as `"1"`.

The danger is that on a scale whose lowest rung is *labelled* `"1"`, passing the
raw value through renders as "mildly allergic" for someone who answered nothing.
Treat it as unanswered.

## Question ids are unique across a project's forms

The two WSJ27 forms share **zero** question ids. That is what lets a member be
matched to their form by looking at which ids they answered, with no mapping
table — decode against every form and the ones they didn't fill yield nothing.
Verified across all confirmed members: every one matched exactly one form.

Don't assume it holds forever; assert it rather than trusting it.

## The data drifts under you

Between two cache refreshes a week apart, all of this changed:

- **Questions disappeared.** Four fields (`87660`, `87662`, `87663`, `87665`)
  were removed from a form. Nothing announces this; validate your id references
  on load.
- **Absent keys became null keys.** `110268` went from "not present for most
  members" to "present with `null`". Code written as
  `access_map[q.get("110268", "73658")]` had worked for months and started
  raising `KeyError: None`, because a `.get()` default only covers an absent
  key. Use `q.get(x) or default`.
- Participant counts move constantly as people register and cancel.

## Fields that lie

- `district_name`, `group_name`, `patrol_name`, `org_id` and friends at the top
  level of a participant are the literal string
  `"Deprecated - use group_registration_info"`. Use `primary_membership_info`.
- `member_no` is sometimes a number and sometimes a string, and a JSON disk
  cache turns dict keys into strings. Pin the type at the one place you build
  your own structure.
- Contact details asked *in the form* are a snapshot taken when the member
  applied, not their live profile: form email differed from `primary_email` for
  258 of 2324 members, and form mobile from `contact_info["1"]` for 112 of 2293.
  Prefer the profile fields; the form copies go stale.
