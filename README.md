[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]
[![LinkedIn][linkedin-shield]][linkedin-url]

# GraDex
Author: Veronica Koval

Try it: **https://gradex-veronicak.cloud.run/** (Use your columbia.edu account)

## Showcase
The same consultation in two of the twelve type themes. The interface recolours itself to the card's Pokémon type once the printing is pinned down: Fire on the left, Water on the right.

![Fire theme](images/fire-theme.png) ![Water theme](images/water-theme.png)

All twelve themes: [sheet 1](images/themes-1.png), [sheet 2](images/themes-2.png).

## Goal
Grading a Pokémon card with PSA currently starts at **$59.99**, and shipping both ways adds roughly $50 more to a small submission. Most cards do not clear that bar, yet collectors send them anyway because the PSA 10 price looks big next to the fee. The comparison that actually matters is against *selling the card raw today*, which almost nobody makes. Our goal is to make that comparison for any card, and to show the working.

## Description
GraDex is a tool-calling chat agent that decides whether a Pokémon card is worth sending to PSA. It pins down the exact printing by asking questions (the same Pokémon is printed across many sets, years and variants, and values differ by orders of magnitude), looks up raw and PSA-graded market prices, asks plain-language condition questions, estimates the grade as a **probability distribution**, and compares the expected value of grading, after fees, shipping and selling costs, against selling the card as it is.

**Why a distribution instead of a predicted grade.** The decision is driven almost entirely by P(PSA 10), because the 9 → 10 price cliff is routinely 3–5×. "Probably an 8 or 9" and "probably a 9 or 10" are the same ±1 window and opposite answers, so a point estimate throws away the only thing that matters.

**What the money math gets right**
- The comparison is against selling raw, not against zero, with selling fees (13.25%) on both sides.
- Declared value follows the expected graded value, not the long-shot PSA 10 price, because it decides the PSA service tier.
- Shipping is per submission, not per card. The agent recomputes at ten cards and says when bundling flips the verdict.
- PSA's Value tiers are suspended as of 2026-10-07, so the floor is Standard at $59.99. The fee table carries an `as_of` date, because these numbers move.
- Missing data refuses rather than invents: no graded comps means no verdict.

**Type themes.** The UI is dark by default. Fire turns the page ember, with your messages in bright orange and the agent's in dark orange, behind a large faint flame; the other ten types do the same with their own palette and symbol (original drawings, not the official energy icons). `selftest.py` re-checks every theme's text contrast from the CSS.

## Built With
[![Python][Python]][Python-url]
[![FastAPI][FastAPI]][FastAPI-url]
[![OpenAI Agents SDK][Agents]][Agents-url]
[![Gemini][Gemini]][Gemini-url]
[![Docker][Docker]][Docker-url]
[![Google Cloud Run][CloudRun]][CloudRun-url]

## How to Use
Run it locally:

```bash
uv run app.py          # http://localhost:8000
uv run selftest.py     # offline checks, no network or credentials needed
```

Two things are needed for the full experience.

**A model.** The app picks a backend from the environment variables you set; nothing in the code changes.

| Backend | How | Cost |
| --- | --- | --- |
| **Gemini API key** | Get a key at [aistudio.google.com](https://aistudio.google.com), then `$env:GEMINI_API_KEY = "..."` | Free tier, no Google Cloud project, no billing |
| **Vertex AI** (course starter) | `gcloud auth application-default login` and `VERTEXAI_PROJECT=your-project` | Needs a project with billing. On Cloud Run the service account handles auth |
| **Ollama** (local) | `ollama pull qwen2.5:7b`, then `MODEL=ollama_chat/qwen2.5:7b` | Free and offline, but small models call tools unreliably |

Set `MODEL` to override the model name. If both a key and `VERTEXAI_PROJECT` are present, the key wins.

**`JUSTTCG_API_KEY`** for card data, from [justtcg.com](https://justtcg.com) (free tier: 100 calls a day, 1,000 a month). Without it the condition rubric still works and the agent says plainly that card data is unavailable. Lookups are cached for six hours and the call count sits in the corner of the UI.

Then chat:
- Describe a card and answer the agent's questions. Open a tool-call chip under any reply to see its arguments and result.
- Press **Start over** to begin a fresh session.

### Three sample queries
1. **`I pulled a Charizard VMAX from Champion's Path and it looks mint. Worth grading?`** Exercises the full path: the agent asks for the collector number, prices the secret rare, asks condition questions, and usually lands on *don't send this one on its own*.
2. **`I have a 1999 Base Set Blastoise, holo, kept in a binder since I was a kid.`** Exercises the vintage branch: binder storage and a pre-2003 era drive the gem rate down hard.
3. **`Is it worth grading a Moonbreon? I have the Evolving Skies alt art.`** A high-value modern card where grading usually *does* pay, to check the agent flips to **GRADE IT** when the numbers justify it.

Ask it to estimate the grade on 20 cards at once to watch shipping get split and the verdict change.

### Testing without the frontend
```bash
uv run cli.py --check-api          # one real JustTCG call; no model needed
uv run cli.py                      # interactive conversation
uv run cli.py --script basic       # a canned multi-turn run
uv run cli.py --http               # drive a running server's /chat route
```
In the conversation, `/dossier` prints findings so far, `/calls` dumps the last turn's tool calls, `/new` starts a fresh session.

## Tools
`/chat` returns `response`, `session_id` and `tool_calls` (name, args and result of every call), plus a `dossier` the side panel renders. Conversations live in a `SQLiteSession` per session id, held in memory.

| Tool | What it does |
| --- | --- |
| `identify_card` | Narrows a described card to one exact printing. With several candidates it returns the single most discriminating question to ask. |
| `get_graded_market_prices` | **The external data call.** One JustTCG request returns the raw market price and each PSA grade's price, with a freshness status so stale or unpriced grades are flagged rather than quoted as fact. |
| `set_card_theme` | Reports the card's energy type so the interface can switch theme. Says `unknown` rather than guessing; the screen stays neutral. |
| `score_card_condition` | Turns plain-English condition answers into a probability distribution over PSA grades. |
| `grading_verdict` | Picks the PSA service level, adds shipping and supplies, applies selling fees, and compares expected value of grading against selling raw. |

### The grade model
**Hard ceilings.** PSA's centering tolerances cap the grade: a front worse than 60/40 or a back worse than 75/25 is not getting a 10 however clean the surface is. Back centering is the quiet PSA 10 killer.

| Grade | Front | Back |
| --- | --- | --- |
| 10 | 55/45 – 60/40 | 75/25 |
| 9 | 60/40 | 90/10 |
| 8 | 65/35 | 90/10 |
| 7 | 70/30 | 90/10 |

**Soft penalties.** Corners, edges, surface and storage demote the expected grade, combined sub-additively (worst flaw in full, the rest at 45%) because PSA grades holistically. The result is a truncated normal over the grade scale, calibrated so a flawless card reproduces the published gem rate for its era: 60.4% for 2023+ modern against a published 61.1%, 14.9% for pre-2003 against 13.9%. Those published rates only count cards somebody chose to submit, so they run optimistic for a random card, and the agent says so. An `unsure` answer costs almost nothing in grade points and instead widens the distribution.

## Layout
| File | |
| --- | --- |
| `app.py` | FastAPI server, the agent and its five tools, session store, `/chat` |
| `grading.py` | Grade rubric: centering ceilings, defect penalties, era priors |
| `economics.py` | PSA fee table, service level selection, expected-value math |
| `cards.py` | JustTCG client, caching, call accounting, error handling |
| `cli.py` | Terminal harness |
| `index.html` | Dark UI: dossier, chat, 12 type themes |
| `selftest.py` | Offline checks for all of the above |

## Limitations
- **An estimate from a description, not a grade.** PSA's graders are the only ones who decide.
- Plain-English centering answers ("slightly off") map to conservative ratios; a real measurement is far better.
- **Market prices, not completed sales.** The agent says "trading around", never "sold for", and never claims a sale count.
- Prices below PSA 8 are estimated from the raw price.
- JustTCG's v2 graded endpoint is in beta, so the response shape may change.
- Penalty weights are tuned judgement, not a fitted model. Only the centering tolerances and era gem rates come from published data.

## Sources
- [**PSA grading service levels and fees**](https://www.psacard.com/services/tradingcardgrading)
- [**PSA centering standards by grade**](https://www.cardcenteringtool.com/centering-for/psa) and [**tolerances, grades 10 down to 6**](https://www.cardcenteringcalculator.com/articles/psa-centering-standards)
- [**PSA 10 gem rates by era**](https://www.pokeinvest.io/gem-rates) and [**odds by card type and handling**](https://pregradecards.com/blog/psa-10-odds-what-are-my-chances-2026)
- [**PSA hidden fees, shipping and Collectors Club**](https://phantomdisplay.com/blogs/blog/psa-grading-costs-2026-complete-pricing-guide-hidden-fees)
- [**JustTCG API**](https://justtcg.com/docs): [GET /v2/cards, including graded variants](https://justtcg.com/docs/api/cards-v2)

## Contributors
- Veronica Koval: [**LinkedIn**](https://www.linkedin.com/in/veronicakoval), [**GitHub**](https://github.com/VerKoval/)



[contributors-shield]: https://img.shields.io/github/contributors/VerKoval/GraDex.svg?style=for-the-badge
[contributors-url]: https://github.com/VerKoval/GraDex/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/VerKoval/GraDex.svg?style=for-the-badge
[forks-url]: https://github.com/VerKoval/GraDex/network/members
[stars-shield]: https://img.shields.io/github/stars/VerKoval/GraDex.svg?style=for-the-badge
[stars-url]: https://github.com/VerKoval/GraDex/stargazers
[issues-shield]: https://img.shields.io/github/issues/VerKoval/GraDex.svg?style=for-the-badge
[issues-url]: https://github.com/VerKoval/GraDex/issues
[license-shield]: https://img.shields.io/github/license/VerKoval/GraDex.svg?style=for-the-badge
[license-url]: https://github.com/VerKoval/GraDex/blob/main/LICENSE
[linkedin-shield]: https://img.shields.io/badge/-LinkedIn-black.svg?style=for-the-badge&logo=linkedin&colorB=0077B5
[linkedin-url]: https://www.linkedin.com/in/veronicakoval
[Python]: https://img.shields.io/badge/python-FFDE57?style=for-the-badge&logo=python&logoColor=4584B6
[Python-url]: https://www.python.org/
[FastAPI]: https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white
[FastAPI-url]: https://fastapi.tiangolo.com/
[Agents]: https://img.shields.io/badge/OpenAI_Agents_SDK-412991?style=for-the-badge&logo=openai&logoColor=white
[Agents-url]: https://openai.github.io/openai-agents-python/
[Gemini]: https://img.shields.io/badge/Gemini-8E75B2?style=for-the-badge&logo=googlegemini&logoColor=white
[Gemini-url]: https://ai.google.dev/
[Docker]: https://img.shields.io/badge/docker-2496ED?style=for-the-badge&logo=docker&logoColor=white
[Docker-url]: https://www.docker.com/
[CloudRun]: https://img.shields.io/badge/Cloud_Run-4285F4?style=for-the-badge&logo=googlecloud&logoColor=white
[CloudRun-url]: https://cloud.google.com/run
