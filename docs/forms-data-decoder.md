# The forms decoder

How a participant's raw Scoutnet answers become the `forms_data` the GUI reads.
The quirks of the source data are in [scoutnet-data-quirks.md](scoutnet-data-quirks.md);
this is about our side.

Code: [`src/app/scoutnet_forms.py`](../src/app/scoutnet_forms.py).
Template: [`src/app/forms_template.json`](../src/app/forms_template.json).

## The template is the source of truth

`forms_template.json` is **hand-maintained** and decides everything the decoder
emits: which questions appear, under which headings, in which order, and what
each is called in the API. The decoder walks the template — never the answers —
so a change to the output is an edit to that file, not to Python.

```json
{
  "avdelningsledare_kontingentledning": {
    "Hälsoinformation": {
      "Diet och födoämnesallergier": {
        "93221": {"key": "specialDiet", "label": "Har du behov av specialkost?"}
      }
    }
  }
}
```

Four levels: **form → tab → section → question id**. It is keyed by question id
because that is how answers arrive; the entry says what to call it (`key`) and
what it asked (`label`).

Output mirrors it exactly, with answers in place of labels:

```json
"forms_data": {
  "deltagare_ist": {
    "Hälsoinformation": {
      "Diet och födoämnesallergier": {"specialDiet": "Ingen specialkost", "hasFoodAllergy": "Nej"}
    }
  }
}
```

### Rules the template must keep

- **One key, one question, one label.** Ids may share a key only when they ask
  the *identical* question — which they do across forms: `specialDiet` is
  `93221` for leaders and `86889` for participants, same wording, one key, so
  the GUI learns one vocabulary. `_load_templates()` refuses a key whose ids
  carry different wording and tells you to split it.
- When wording genuinely differs, use separate keys. `additionalInfo` and
  `additionalInfoUnitLeader` exist because participants are asked about
  "avdelningsledare eller kontingentledningen" while IST and leaders are only
  asked about "kontingentledningen". A member gets one or the other, never both.
- Keys are camelCase, English and semantic (`sensitivityToUnpredictability`,
  not the Swedish sentence). The GUI's label table supplies the display text.
- A key must live in one section only. Splitting it across sections makes the
  answer's placement arbitrary.

## What is deliberately not in `forms_data`

| left out | why |
|---|---|
| `Intern information` tab | staff-only fields the applicant never sees |
| `WSJ-relaterad information` tab | consumed elsewhere in the system |
| `applicationType`, travel types | already the basic block's `member_type` etc. |
| language skills | sent to the Jamboree host organisation, not used here |

These are simply absent from the template. `tools/question_keys.py` still
documents every question id in both forms, including the omitted ones.

## `contact_info`: same template, different access level

`forms_data` is health and food information, and [`participants.py`](../src/app/participants.py)
drops it **wholesale** for a caller without the role for it. Contact details are
available with basic access, so they cannot live inside it — the gate is
all-or-nothing.

So the `Grundläggande information` tab is described in the template like every
other question, keeping its keys, labels and order, and `_split_contact()` sends
it to `contact_info` instead. `CONTACT_TABS` names the tabs that go that way;
the split is about access, not about display.

```json
"contact_info": {
  "Information redan i Scoutnet":  {"email": "…", "mobilePhone": "…", "alternateEmail": "…"},
  "Kontaktuppgifter närstående 1": {"nextOfKin1Name": "…", "nextOfKin1Relation": "Moder", …},
  "Kontaktuppgifter närstående 2": {…}
}
```

The form and tab levels are dropped — constant once the health tabs are gone —
and the section grouping kept, so the GUI renders it with the same code as a
`forms_data` section. What to *display* stays the GUI's decision: everything
published reaches it.

These fields are a **snapshot from application time**, not the live profile: the
form email differs from `primary_email` for 258 of 2324 members and the form
mobile from `contact_info["1"]` for 112 of 2293. The basic block carries the
live values, so both are available and visibly distinct — as in ScoutView.

### The leaders' contact questions (published 2026-09-01)

Scoutnet originally did not publish the leaders' form's contact questions over
the API — form 47115 returned 54 questions with no e-post, mobil, Närstående or
Nödkontakt field, though the form rendered them, so all 268 leaders arrived with
no contact data at all. Publishing them raised the form to 74 questions. **This
is per-form question visibility and can be turned off again**; if leaders lose
their contact data, check that before suspecting the decoder.

Three things about those questions, all handled in the template:

- They use **different ids** from the participant form for the identical
  questions (`90957` vs `85101` for Närstående 1 - Namn, and so on). Shared keys
  carry both ids, which is what the one-key-one-question rule is for.
- They sit in a **tab with an empty title**, which `to_title_dict()` drops. The
  template files them under a `Grundläggande information` tab of its own naming,
  matching the participant form so `CONTACT_TABS` routes both the same way.
- Two labels are worded slightly differently — `Mobilnummer` for
  `Ansökandes mobiltelefon`, and `Närstående - Relation` missing its `2`. The
  template **normalises** these to the participant wording so the shared key
  keeps a single label. Ids `90961`-`90964` run in sequence, which is what
  identifies the unnumbered relation field as next of kin 2's.

The `Annan nödkontakt` section is leaders-only and names contacts to reach
*instead of* the next of kin. It has its own keys (`emergencyContact1*`,
`emergencyContact2*`, gated by `hasAlternateEmergencyContact`), so a GUI can
tell the two kinds apart — which matters in an emergency.

## Decoding rules

- **Choice** answers resolve through the question's `choices`; multi-select
  gives a list.
- **Linked profile fields** (`{"linked_id": N, "value": "..."}`) are unwrapped
  to their `value`.
- **Unanswered is omitted**, not emitted as `null` or `""`. `null`, `""`, `[]`
  and the untouched-radio `"1"` all count as unanswered, and a section or tab
  left with nothing is dropped rather than emitted bare. A member who filled in
  nothing gets `"forms_data": {}`.
- **Several ids on one key**: the first holding an actual answer wins, so a
  blank id can't mask a filled one. One member has exactly that — a blank
  `87312` beside a filled `93212`.
- **Every form is tried.** Question ids are disjoint across forms, so a form the
  member never filled yields nothing and is left out. No member-type mapping is
  needed, and a third form would work with no code change.

### Why unanswered questions are omitted

Scoutnet distinguishes answered / shown-but-blank / never-shown, and we keep
only the first. The case that would justify showing blanks — someone answering
"Ja" and leaving the description empty, which the health patrol would want to
chase — **does not occur**: zero cases across `medicationDetails`,
`medicalConditionDetails`, `otherAllergyDetails` and `foodAllergyDetails`.
The ~7500 blanks are `mobilityAids: []`, `cognitiveDiagnoses: []` and empty
free-text boxes; all mean "no", none is worth a row.

## Two deliberate differences from ScoutView

This replaces ScoutView, whose decoder is `decode_choice_questions()` in its
`application/main.py`. It matches ScoutView everywhere except:

1. **Untouched radios.** ScoutView falls back to the raw value
   (`choice_map[qid].get(str(answer), str(answer))`), so a `"1"` reaches the
   screen and reads as "mildly allergic" on a 1–5 scale. We treat it as
   unanswered — roughly 600 phantom mild-allergy readings disappear. Mention
   this at handover; it is the visible change for the health patrol.
2. **Blank answers** are omitted rather than rendered as an empty cell.

Both are counted in a single log line per refresh, e.g.
`Ignored 765 untouched-radio markers over 32 questions; most common: …`.

## Changing things

- **Add / remove / reorder a question**: edit the template. Nothing else.
- **Rename a key**: edit the template, rerun `tools/decode_participants.py`, and
  hand the GUI the regenerated `question_labels.json` / `questionKeys.ts`.
- **Fix a label typo** (Scoutnet's own wording contains `epelepsi`, `tilll`,
  `Bedömmare`): edit the template; the GUI's label table is generated from it.
- **After any change**, run `tools/decode_participants.py` and check its output
  against the app's — see [`tools/README.md`](../tools/README.md).

`_load_templates()` validates on every cache refresh and logs an error for an id
no form defines, an entry with no key, and two ids sharing a key with different
wording. It logs rather than raises: a bad template must not take the API down.
