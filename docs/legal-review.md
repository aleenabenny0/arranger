# Questions for the owner and a lawyer

Arranger ships with draft legal pages (`frontend/privacy.html`, `terms.html`,
`cookies.html`, `copyright.html`, `support.html`). They describe what the
software does, taken from the code. They deliberately say nothing about who
operates the service, under which law, or what is promised, because none of
that is known to the codebase and none of it has been invented.

Until the items below are settled, every legal page shows a "Draft" banner and
prints "not provided by the operator" wherever a detail is missing. Setting
`PUBLIC_LAUNCH=true` removes the banner; startup refuses that setting while the
operator details are blank.

Nothing in this file is legal advice. It is a list of decisions that are not an
engineer's to make.

## 1. Who operates the service

| Needed | Goes in | Why it matters |
|---|---|---|
| Legal name of the operator (person or company) | `OPERATOR_NAME` | A privacy notice with no named controller is not a privacy notice. |
| Contact email | `OPERATOR_CONTACT_EMAIL` | Shown on every legal page and on Support. |
| Postal address | `OPERATOR_POSTAL_ADDRESS` | Required to be published in some countries (for example by German and Austrian imprint rules, and for a registered DMCA agent). Optional in others. Startup does not force it; decide it here. |
| Governing law and venue | `OPERATOR_JURISDICTION` | Printed in the terms. |
| Privacy contact | `PRIVACY_CONTACT_EMAIL` | Where access, correction and deletion requests go when self-service is not enough. |
| Copyright contact | `DMCA_CONTACT_EMAIL` | Where infringement notices go. |
| Where data is stored | `DATA_REGION` | Printed in the privacy notice. Must match where the database and files really are. |

**Open questions**

- Is the operator an individual or a registered entity? It changes liability and
  what must be published.
- Is a registered DMCA agent wanted (United States)? Registration is a filing
  with the Copyright Office and has a fee. Without it, the safe harbour for
  user uploads is not available there.
- Is an imprint page needed (Germany, Austria, Switzerland)?

## 2. Privacy

The notice already states, from the code: what is stored, that there is no
analytics or advertising, who receives data, retention periods read from
configuration, and how to export and delete. What it cannot state:

- **Legal basis** for each kind of processing (GDPR Article 6), if users in the
  EU or UK are in scope. Likely contract for the service itself; confirm.
- **Hand measurements.** The profile stores reach, finger availability and
  speed. Missing fingers or a restricted reach may reveal a disability, which
  is health data under GDPR Article 9 and similar laws. Is explicit consent
  needed at the point where the user enters it? The software does not currently
  ask for it. **This is the question most likely to need a product change.**
- **Minimum age.** There is no age gate. Under-13 (US, COPPA) and under-16 (EU
  default) users need parental consent in some cases. Decide the minimum age;
  the terms and the registration form then need a line each.
- **International transfers.** If the database or email provider is outside the
  user's region, which mechanism covers it? The processors are listed in the
  notice; their contracts are the operator's to sign.
- **Processor agreements** with the hosting provider, Resend (email), Anthropic
  (only if `MODEL_REPAIR_ENABLED=true`) and the object store (only if `s3`).
- **Backups.** The application deletes data at once on request, but it does not
  control backups. How long do backups live? The notice must say so.
- **Breach notification.** Who decides, who is told, within what time. The
  runbook stops at "this is a legal question".
- **Supervisory authority and representative.** Which regulator do users
  complain to? Is an EU or UK representative needed?
- **Do-not-sell / CCPA wording**, if California users are in scope. Nothing is
  sold or shared for advertising; a lawyer should say whether a statement is
  still required.
- **Log retention.** Request logs contain network addresses. The application
  writes them to stdout; how long the host keeps them is a host setting that
  the notice should reflect.

## 3. Terms of use

The draft says what the service does and does not promise: "passes modeled
constraints" is a statement about a model of the hand, not a guarantee of
comfort; difficulty is an estimate; transcription makes mistakes; play within
your own limits. It states that there is no billing, so there is no refund
process. It leaves these to a lawyer:

- Limitation of liability and disclaimer of warranties, in a form that is
  enforceable where the operator is.
- **Physical-injury wording.** The product tells people what their hands can
  play. The draft tells users to stop if anything hurts. Is that enough?
- Acceptable use, suspension and termination.
- How the terms change and how users are told.
- Dispute resolution.
- Consumer-law rights that cannot be waived (EU, UK, Australia).
- If billing is ever added: pricing, tax, renewal, cancellation, refunds, and
  the withdrawal right for digital services. None of this exists in the code.

## 4. Copyright

The upload page tells users to upload only public-domain music, their own
music, or music they have permission to arrange, and explains that an
arrangement is a derivative work and that an edition can carry its own
copyright. Uploads are private to the account and are never published or used
for training.

- Is that notice sufficient, or should the user tick a box at upload to confirm
  they have the right? That is a product change and is not built.
- Notice-and-takedown and counter-notice procedure, and a repeat-infringer
  policy, if the DMCA safe harbour is wanted.
- **Who owns the arrangement?** The draft says the user keeps all rights in what
  they upload and what is made from it. It grants the operator nothing beyond
  storing and processing the files to provide the service, and it does not say so
  in those words; a lawyer should add the licence wording.
  Confirm that is the intention.
- Whether output made with the optional AI model needs any disclosure. The model
  chooses arrangement decisions from a fixed menu; it never writes notes.

## 5. Third-party software and content

| Component | Licence | How it is used | Question |
|---|---|---|---|
| Verovio | LGPL-3.0-or-later | Unmodified, fetched at build time, loaded as a separate file in the browser | Attribution is in the page footer. Is a link to the source and licence text also wanted? |
| LilyPond | GPL-3.0-or-later | Run as a separate program; its output (the PDF) is not covered by the GPL | The image redistributes the Debian package. Distributing the image publicly carries the usual GPL source-offer duties for that package. |
| Basic Pitch model | Apache-2.0 | The ONNX model file only | Attribution and notice file in the image. |
| axe-core | MPL-2.0 | Tests only, never shipped | None. |
| Python dependencies | Mostly MIT, BSD, Apache-2.0 | See `requirements.lock` | A licence inventory has not been generated. `pip-licenses` would do it. |
| Evaluation corpus | Public domain and Creative Commons, from the Mutopia Project | Development only, never served | Per-file attribution is in `evals/corpus/manifest.json`. |

## 6. Accessibility

The interface is built to WCAG 2.2 AA and is checked on every change by
axe-core in a real browser, with keyboard-only and phone-width runs. Automated
checks find a minority of accessibility problems. No person who uses a screen
reader, switch, or magnifier has tested it, and no conformance statement has
been written.

- Is a formal accessibility statement required (EU public sector, or the
  European Accessibility Act for consumer services from June 2025)?
- Who handles accessibility feedback, and how quickly?

## 7. Claims the product makes

Reviewed for this handoff, and worth re-checking before any marketing is
written:

- No accuracy figure is claimed for transcription. Measured accuracy so far is
  on synthesised audio only and is documented as such in
  `docs/audio-transcription.md`.
- "Passes modeled constraints" is the strongest playability statement anywhere
  in the interface. It is never shortened to "playable".
- The fidelity figure is labelled as a score of what can still be heard, not a
  count of notes kept.
- There are no testimonials, user counts, certifications or security badges.

## Before setting `PUBLIC_LAUNCH=true`

1. Sections 1 to 4 answered and the legal pages edited to match.
2. The hand-measurement consent question (section 2) decided, and built if needed.
3. Minimum age decided, and built if needed.
4. Backup retention decided and written into the privacy notice.
5. A lawyer has read the five pages as they will appear.
