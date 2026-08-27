# Chrome Web Store listing — Draft Copilot

Working name: **Draft Copilot** (internal: ffbot). Everything below is copy-paste
ready for the CWS developer dashboard. Placeholders (`YOUR_DOMAIN`, `JOEL_LINK`,
`CONTACT_EMAIL`, `EXTENSION_ID`) must be replaced before submission.

> Note: the Chrome Web Store takes the item name and short description from
> `manifest.json`, not from the dashboard. The manifest must be updated to match
> this listing before the zip is uploaded (see the manifest checklist in the
> repo/workstream notes).

---

## Name

Draft Copilot

## Summary (max 132 characters — this one is 130)

Live draft help for Sleeper (Yahoo beta): best-available advice, odds a player lasts to your pick, tunable strategy. Advises only.

## Category

**Entertainment** (fantasy sports). If the dashboard offers a Sports
subcategory, use it; **Workflow & Planning** is the fallback if Entertainment
feels wrong at submission time.

## Full description

Draft Copilot docks a live draft assistant beside your fantasy football draft
room. You draft; it keeps the math current.

What it does, during the draft:

- **Mirrors the draft board live.** Every pick shows up in the panel as it
  happens, so the advice is always about the board as it actually stands.
- **Best available, by position, ranked by your strategy** — not a static
  cheat sheet. Rankings re-sort as the draft develops.
- **"Will he last?" survival odds.** Monte Carlo simulation of the picks
  between you and your next turn, so "reach now or wait" is a probability,
  not a gut call.
- **A plain-English chat assistant.** Ask "who do I take here?" or "can I
  wait on a QB?" and get a straight answer grounded in the live board.
- **Injury flags** on recommendations, so a "value" isn't a surprise.
- **Rookie draft-capital logic** — rookies are weighted by where real NFL
  teams drafted them, not just camp hype.
- **Strategy presets you can change mid-draft:** RB-heavy, zero-RB, hero-RB,
  pure BPA, superflex, dynasty. Your league zigged? Re-tune without leaving
  the room.

Platforms:

- **Sleeper** — full support today.
- **Yahoo Fantasy** — beta, via Yahoo's official OAuth sign-in (we never see
  your password).
- **ESPN and NFL.com** — not supported yet; coming later.

What it will never do: **make a pick for you.** Draft Copilot advises only.
The extension itself is just a viewer — it docks the Draft Copilot web app in
a sidebar on your draft page. It reads nothing from the page, scrapes
nothing, and clicks nothing. Draft data comes from your platform's API via
the hosted service, the same public data anyone with the draft link can see.

Rankings and strategy are built on the Joel Smyth 2026 Draft Guide
(JOEL_LINK) — if you like how this bot thinks, he's why. Joel is not
affiliated with and does not endorse Draft Copilot.

No accounts, no email address, no ads, no analytics, no trackers. Sessions
are anonymous and auto-delete after a period of inactivity. Full policy:
https://YOUR_DOMAIN/privacy

Not affiliated with Sleeper, Yahoo, ESPN, or the NFL.

Questions: CONTACT_EMAIL

## Single-purpose statement

Draft Copilot has one purpose: to display the Draft Copilot fantasy-football
draft assistant in a sidebar next to the user's live draft room. The
extension injects an iframe of the Draft Copilot web app on supported draft
pages and stores the user's sidebar settings. It does not read, modify, or
automate any page content.

## Permission justifications

**storage** — Saves the user's sidebar preferences (panel width, collapsed
state, chosen options) locally via `chrome.storage` so the sidebar comes back
the way they left it. Nothing else is stored; nothing is synced to us.

**Host access: `https://YOUR_DOMAIN/*`** — The sidebar is an iframe of the
Draft Copilot web app hosted at YOUR_DOMAIN. Host access is required to load
and communicate with that app (the extension's own UI). No data is read from
any other site.

**Content script on `sleeper.com` / `sleeper.app` and `yahoo.com` draft
pages** — Injects the sidebar iframe beside the draft room and nothing else.
The content script reads no page content: it does not scrape the draft board,
league, chat, or DOM. It only mounts/unmounts the sidebar (in a shadow root,
so page styles and extension styles cannot affect each other) and watches the
URL path to know when the user is in a draft room. Draft data shown in the
sidebar comes from the hosted service via the platform's public API, not from
the page.

## Data usage disclosures (dashboard privacy tab)

These must match the privacy policy at https://YOUR_DOMAIN/privacy
(site/privacy.html in the repo).

Collection checklist — check **none** of the categories. The extension
itself collects no user data:

- Personally identifiable information: **No**
- Health information: **No**
- Financial and payment information: **No**
- Authentication information: **No** (Yahoo OAuth happens on Yahoo's site
  within the web app, never in the extension)
- Personal communications: **No**
- Location: **No**
- Web history: **No**
- User activity (clicks, keystrokes, etc.): **No**
- Website content: **No** (the content script reads nothing from the page)

Settings live in `chrome.storage.local` on the user's machine and are not
transmitted to us as telemetry. The embedded web app is an ordinary visit to
YOUR_DOMAIN and is governed by the privacy policy: anonymous sessions, no
accounts, chat messages and connected-draft data only, auto-deleted after
inactivity.

Certifications (check all three):

- Data is not sold to third parties and is not used or transferred for
  purposes unrelated to the item's single purpose.
- Data is not used or transferred to determine creditworthiness or for
  lending purposes.
- Privacy policy URL: `https://YOUR_DOMAIN/privacy`

## Screenshots — shot list (5, capture at 1280x800)

To be captured from real sessions (mock drafts are fine — they are real app
output). No fabricated boards, no invented stats or user counts in overlay
text.

1. **The sidebar in place.** A live Sleeper draft room mid-draft with the
   Draft Copilot panel docked on the right: draft board mirrored in the
   panel, best-available list visible. Establishes the core promise in one
   frame.
2. **Best available, by position.** Close crop of the recommendation list
   grouped by position, with the active strategy preset visible (e.g.
   hero-RB) and an injury flag showing on one player. Caption the flag.
3. **Survival odds.** The "will he last to your next pick?" view: a queued
   player with his Monte Carlo survival percentage and the picks remaining
   until the user's turn. This is the feature no cheat sheet has — make the
   percentage the focal point.
4. **The chat assistant.** A real exchange: user asks a natural question
   ("Can I wait on QB until round 8?"), assistant answers in plain English
   referencing the live board. Show the answer actually citing board state.
5. **Strategy tuned mid-draft.** The preset/tuning controls open, mid-draft,
   showing the re-ranked list after switching presets (e.g. zero-RB to BPA).
   Demonstrates "tunable mid-draft" honestly — same board, new order.

Promo tile (440x280, optional but recommended): the lace icon and the
wordmark on the dark background, plus the one-liner "Advises only. Never
picks." No screenshots-in-tile, no badges, no invented ratings.
