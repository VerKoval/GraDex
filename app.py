"""GraDex: should this Pokemon card be sent to PSA, or stay in its sleeve?

A FastAPI server around an OpenAI Agents SDK agent running on Gemini through
LiteLLM. The shape follows the course starters: an in-memory session store, a
`/chat` endpoint returning `response`, `session_id` and `tool_calls`, and a
`RunHooks` subclass that records every tool call so the UI can show the agent's
work.

The agent walks one path:

    identify the exact printing -> price its graded outcomes -> grade the card
    from described condition -> compare expected value against selling it raw

Each step is a tool, and the interesting one is the third: it turns plain-English
condition answers into a probability distribution over PSA grades using PSA's
published centering tolerances as a hard ceiling.
"""

import json
import os
import uuid
from pathlib import Path
from typing import Literal

import uvicorn
from agents import (
    Agent,
    MaxTurnsExceeded,
    ModelSettings,
    RunHooks,
    Runner,
    SQLiteSession,
    function_tool,
    set_tracing_disabled,
)
from agents.extensions.models.litellm_model import LitellmModel
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

import cards
import economics
import grading

# Tracing uploads to OpenAI by default, which would need an OpenAI key. We're on Gemini.
set_tracing_disabled(True)

# Which model runs the conversation. Pick one by setting an environment variable;
# nothing else in the code changes.
#
#   MODEL=...            use exactly this LiteLLM model string
#   GEMINI_API_KEY=...   free Gemini API key from aistudio.google.com: no Google
#                        Cloud project and no billing
#   (neither)            Gemini on Vertex AI, which is the course starter's setup
#                        and needs a Google Cloud project with billing
#
# Local, free, no account at all: install Ollama, `ollama pull qwen2.5:7b`, then
# MODEL=ollama_chat/qwen2.5:7b. Small models call tools much less reliably.
GEMINI_API_KEY = (
    os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
).strip()
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "global")


def _pick_model() -> str:
    explicit = os.environ.get("MODEL", "").strip()
    if explicit:
        return explicit
    if GEMINI_API_KEY:
        return "gemini/gemini-3.5-flash-lite"
    return "vertex_ai/gemini-3.5-flash-lite"


MODEL = _pick_model()


def backend_name(model: str = MODEL) -> str:
    """A short human label for the CLI and /health: where the model is running."""
    if model.startswith("vertex_ai/"):
        return "Vertex AI (needs a billed Google Cloud project)"
    if model.startswith(("gemini/", "google/")):
        return "Gemini API key (free tier)"
    if model.startswith("ollama"):
        return "Ollama (local)"
    return "custom"


def build_model_and_settings(model: str = MODEL):
    """The LiteLLM model plus the settings that only apply to some backends.

    `vertex_location` is a Vertex-only argument, so it must not be sent to any
    other provider, which would reject it.
    """
    if model.startswith("vertex_ai/"):
        # Vertex authenticates with Google credentials, so api_key is unused.
        return (
            LitellmModel(model=model, api_key="unused"),
            ModelSettings(extra_args={"vertex_location": VERTEX_LOCATION}),
        )
    if model.startswith(("gemini/", "google/")):
        return LitellmModel(model=model, api_key=GEMINI_API_KEY or "unused"), ModelSettings()
    # Ollama and anything else: LiteLLM's defaults, no key.
    return LitellmModel(model=model, api_key="unused"), ModelSettings()

# --- Tools ---------------------------------------------------------------
#
# @function_tool builds each JSON schema from the signature and docstring, so
# the argument descriptions below are what the model actually reads. Literal
# types become enums, which is what keeps the condition answers structured
# instead of free text.


@function_tool
def identify_card(
    card_description: str, set_name: str = "", collector_number: str = "", limit: int = 6
) -> str:
    """Find which exact Pokemon card printing the user is holding.

    Call this first, before any pricing. The same Pokemon is printed many times
    across sets, years, rarities and variants (V, VMAX, ex, full art, alternate
    art, secret rare), and those printings can differ in value by orders of
    magnitude, so the printing must be pinned down before anything is quoted.

    If more than one candidate comes back, the result includes a `next_question`
    naming the single most useful thing to ask. Ask exactly that, then call this
    tool again with the user's answer folded into card_description.

    Args:
        card_description: What the user said they have, as specifically as they
            put it, e.g. 'Charizard VMAX rainbow secret rare' or 'Base Set
            Blastoise 009/102'. Include a collector number if they gave one.
        set_name: Optional set name to narrow the search, e.g. 'Champion's Path'.
            Leave empty unless the user named a set.
        collector_number: Optional number printed on the card. Pass it exactly as
            the user gave it, e.g. '34/119' or '074'; padding and the '/total' part
            are handled for you. This is the single most discriminating filter, so
            pass it as soon as the user gives it. Leave empty otherwise.
        limit: How many candidate printings to return, 1 to 20. Default 6.
    """
    return json.dumps(
        cards.identify(card_description, set_name, collector_number, limit), default=str
    )


@function_tool
def get_graded_market_prices(card_id: str = "", card_name_and_set: str = "") -> str:
    """Look up what one specific printing trades at raw and in PSA slabs.

    This is the external data call: one request returns the raw market price
    plus the market price of each PSA grade, with a freshness status on each so
    weak or stale numbers can be flagged to the user.

    These are observed market prices, not medians of completed sales, and there
    is no sale count. Describe them as what the card is trading at. Never call
    them recorded sales.

    Prefer the card_id from identify_card. Pass it only once the user has
    confirmed which printing is theirs.

    Args:
        card_id: The card_id of a candidate returned by identify_card. Strongly
            preferred, since it names one exact printing.
        card_name_and_set: Fallback when no card_id is available: the card name
            and set together, e.g. 'Charizard VMAX Champion's Path'.
    """
    return json.dumps(cards.graded_prices(card_id, card_name_and_set), default=str)


@function_tool
def score_card_condition(
    centering_front: str,
    centering_back: str,
    corners: Literal[
        "all_sharp",
        "one_slight_white",
        "multiple_slight_white",
        "visible_wear",
        "soft_or_dinged",
        "unsure",
    ],
    edges: Literal[
        "clean",
        "minor_whitening_one_edge",
        "whitening_several_edges",
        "chipping",
        "unsure",
    ],
    surface_front: Literal[
        "flawless",
        "one_light_scratch",
        "several_scratches",
        "print_line_or_dimple",
        "scuffed_or_creased",
        "unsure",
    ],
    surface_back: Literal[
        "flawless",
        "one_light_scratch",
        "several_scratches",
        "print_line_or_dimple",
        "scuffed_or_creased",
        "unsure",
    ],
    era: Literal[
        "japanese_modern",
        "modern_2023_plus",
        "modern_2019_2022",
        "late_2010s",
        "mid_2000s",
        "vintage_pre_2003",
        "unsure",
    ],
    handling: Literal["pack_fresh_sleeved", "sleeved_later", "loose_or_played", "unsure"],
) -> str:
    """Estimate the PSA grade as a probability distribution, not a single number.

    Call this only after the printing is confirmed. Gather the answers by asking
    the user a few short questions in plain language, one or two at a time, and
    pass 'unsure' for anything they genuinely cannot tell you: unknowns widen the
    estimate and lower its stated confidence rather than being treated as perfect.

    The result gives the probability of each grade, the best grade still
    reachable, and the single factor holding the grade down. Centering is a hard
    ceiling drawn from PSA's published tolerances, so a card that is off-centre
    cannot reach a 10 however clean it looks.

    Args:
        centering_front: The front border split as the user describes it. Accepts
            a ratio like '55/45' or '60/40', 'dead centered', a plain-English
            description like 'slightly off' or 'noticeably off', or 'unsure'.
            To get this, ask whether the left and right borders look equal and
            whether the top and bottom do; the worse of the two is what counts.
        centering_back: The back border split, in the same formats. The back is
            judged more loosely than the front but still caps a 10 at 75/25.
        corners: Sharpness of all four corners under good light.
        edges: Whitening or chipping along the card's edges.
        surface_front: Scratches, print lines, dimples or scuffs on the front,
            viewed at an angle under light. Holo cards show scratches readily.
        surface_back: The same inspection on the back.
        era: Roughly when the card was printed, which sets the base gem rate.
            Use 'japanese_modern' for recent Japanese cards, which gem highest.
        handling: How the card has been stored since it was pulled.
    """
    return json.dumps(
        grading.score_condition(
            centering_front=centering_front,
            centering_back=centering_back,
            corners=corners,
            edges=edges,
            surface_front=surface_front,
            surface_back=surface_back,
            era=era,
            handling=handling,
        ),
        default=str,
    )


@function_tool
def grading_verdict(
    raw_market_price: float,
    psa10_price: float,
    psa9_price: float,
    p10: float,
    p9: float,
    p8: float,
    p7: float,
    p6_or_lower: float,
    psa8_price: float = 0.0,
    card_count: int = 1,
) -> str:
    """Decide whether grading beats selling the card raw, after all costs.

    Call this last, with prices from get_graded_market_prices and probabilities
    from score_card_condition. It picks the PSA service level from the declared
    value, adds shipping and supplies, applies selling fees to both sides, and
    returns the expected value of grading against the value of selling it raw
    today, plus the lowest grade that breaks even.

    Args:
        raw_market_price: Current ungraded market price in USD, from
            get_graded_market_prices. This is the opportunity cost: what the user
            gives up by sending the card away.
        psa10_price: Recorded PSA 10 sale price in USD. Pass 0 if unknown.
        psa9_price: Recorded PSA 9 sale price in USD. Pass 0 if unknown.
        p10: Probability of a PSA 10, from score_card_condition's distribution.
        p9: Probability of a PSA 9.
        p8: Probability of a PSA 8.
        p7: Probability of a PSA 7.
        p6_or_lower: Probability of a PSA 6 or below.
        psa8_price: Recorded PSA 8 sale price in USD if available, else 0, in
            which case it is estimated from the raw and PSA 9 prices.
        card_count: How many cards the user plans to send in one submission.
            Shipping is charged per submission, so this materially changes the
            answer. Default 1; ask the user if they have not said.
    """
    return json.dumps(
        economics.grading_economics(
            raw_market_price=raw_market_price,
            psa10_price=psa10_price,
            psa9_price=psa9_price,
            p10=p10,
            p9=p9,
            p8=p8,
            p7=p7,
            p6_or_lower=p6_or_lower,
            psa8_price=psa8_price,
            card_count=card_count,
        ),
        default=str,
    )


CARD_TYPES = (
    "grass", "fire", "water", "lightning", "psychic", "fighting",
    "darkness", "metal", "dragon", "fairy", "colorless",
)


@function_tool
def set_card_theme(
    card_type: Literal[
        "grass", "fire", "water", "lightning", "psychic", "fighting",
        "darkness", "metal", "dragon", "fairy", "colorless", "unknown",
    ],
) -> str:
    """Tell the interface which Pokemon TCG energy type the confirmed card is, so
    the screen can switch to that type's colour theme.

    Call this once, right after the card's printing is confirmed (end of step 1),
    and only if you are sure of the type. Use the card's own type, not its name's
    association: Charizard VMAX is fire, Blastoise is water, Pikachu is lightning,
    Mewtwo is psychic, Umbreon is darkness, Gardevoir is psychic or fairy
    depending on the printing. Trainer and Energy cards, and anything you are not
    certain about, are "unknown" (the interface stays neutral). Pre-2008 Dark and
    Metal Pokemon use "darkness" and "metal"; Dragon is its own type.

    Args:
        card_type: The card's type, or "unknown" when not certain.
    """
    return json.dumps({"card_type": card_type, "theme_set": card_type != "unknown"})


TOOLS = [
    identify_card,
    get_graded_market_prices,
    set_card_theme,
    score_card_condition,
    grading_verdict,
]

# --- Agent ---------------------------------------------------------------

INSTRUCTIONS = """
You are GraDex, an agent that answers one question: is this Pokemon card
worth sending to PSA for grading, or should it stay in its sleeve?

You work in four steps and you do not skip or reorder them. You also have a small
interface tool, set_card_theme, used once at the end of step 1.

STEP 1 - PIN DOWN THE PRINTING.
Call identify_card on whatever the user describes. Never assume which printing
they mean: the same Pokemon exists across many sets, years and variants, and the
values are nothing alike. If the tool returns several candidates it will also
return a next_question; ask exactly that, one question, then call identify_card
again with the answer included. Once a single printing is confirmed, say which
card you have settled on, with its set and collector number, so the user can
correct you. Then call set_card_theme with the card's energy type so the screen
can match it. If you are not certain of the type, pass "unknown"; never guess.

STEP 2 - PRICE THE OUTCOMES.
Call get_graded_market_prices with the confirmed card_id. Report the raw price
and the PSA prices. These are market prices, not completed-sale medians, so say
"trading around" rather than "sold for", and never claim a number of sales. If
the tool flags a stale or unpriced grade, say so plainly: a PSA 10 price that
has not moved in six months is a guess, not a market.

STEP 3 - GRADE THE CARD FROM ITS CONDITION.
Ask the user short, concrete questions, at most two per message, then call
score_card_condition once. Ask in plain language, never in jargon:

- Centering: "Hold the card at arm's length. Do the left and right borders look
  the same width? What about top and bottom?" If one side looks thicker, ask
  roughly how much. Pass what they say straight through: the tool accepts
  'slightly off' and 'way off' as well as ratios like 60/40.
- Corners: "Under a lamp, do all four corners come to a clean point, or is there
  any white fuzz on them?"
- Edges: "Run your eye along the edges. Any white flecks or roughness?"
- Surface: "Tilt it under the light. Any scratches, scuffs, or a line across the
  holo?" Ask about the back too.
- Era and storage: when the card is from, and whether it has been sleeved since
  it was pulled.

Pass 'unsure' for anything the user cannot answer. Do not push them to guess.
Then report the distribution in words: the probability of a 10, the most likely
grade, and the limiting factor the tool names. Always say this is an estimate
from a description, not a grade, and that PSA's graders are the only ones who
decide.

STEP 4 - GIVE THE VERDICT.
Call grading_verdict with the prices from step 2 and the probabilities from step
3. Ask how many cards they plan to send if they have not said, because shipping
is charged per submission and it changes the answer. Then give the verdict in
two or three sentences: the call, the expected value against selling it raw, the
break-even grade, and any bundling advice the tool returns.

RULES.
- Never retry a failed or empty lookup with small variations of the same input. If
  identify_card finds nothing, or says to ask the user something, stop calling
  tools and ask the user that one question. Call any single tool at most twice in
  a row unless the user has given you new information.
- Never invent a price, a population figure, or a grade. Every number you state
  comes from a tool result. If a tool returns an error, tell the user what went
  wrong in one sentence and what still works.
- You are not appraising an investment and you do not give financial advice. You
  are comparing two numbers and showing your working.
- Be brief and concrete. No bullet-point walls, no hedging paragraphs. Short
  paragraphs, plain words, one question at a time when you need something.
- If the user asks something off-topic, answer briefly and steer back.
- If the user just wants a grade estimate and not the money question, do step 3
  alone and skip the rest.
""".strip()

_MODEL_OBJECT, _MODEL_SETTINGS = build_model_and_settings()

agent = Agent(
    name="GraDex",
    instructions=INSTRUCTIONS,
    # LiteLLM routes to Gemini on Vertex AI. api_key is unused: Vertex uses the
    # service account credentials, so nothing secret lives in this repo.
    model=_MODEL_OBJECT,
    model_settings=_MODEL_SETTINGS,
    tools=TOOLS,
)


class RecordToolCalls(RunHooks):
    """Records every tool call of one run, for the /chat response and the UI."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def on_tool_end(self, context, agent, tool, result):
        # For a function tool, context is a ToolContext carrying the call's arguments.
        try:
            args = json.loads(context.tool_arguments) if context.tool_arguments else {}
        except (TypeError, ValueError):
            args = {"raw": str(context.tool_arguments)}
        try:
            parsed = json.loads(result) if isinstance(result, str) else result
        except ValueError:
            parsed = result
        self.calls.append({"name": tool.name, "args": args, "result": parsed})


# --- Session store -------------------------------------------------------
#
# session_id -> {"session": SQLiteSession, "dossier": {...}}. In memory and
# single process, like the starter. The dossier is what the side panel renders:
# the agent's findings so far, accumulated from tool results.

sessions: dict[str, dict] = {}


def _new_session(session_id: str) -> dict:
    return {
        # ":memory:" keeps each conversation in this process and nothing on disk.
        "session": SQLiteSession(session_id),
        "dossier": {
            "card": None,
            "candidates": [],
            "prices": None,
            "grade": None,
            "verdict": None,
            "theme": "neutral",
        },
    }


def _update_dossier(dossier: dict, calls: list[dict]) -> None:
    """Fold this turn's tool results into the running dossier."""
    for call in calls:
        name, result = call["name"], call["result"]
        if not isinstance(result, dict) or result.get("error"):
            continue

        if name == "set_card_theme":
            card_type = result.get("card_type")
            dossier["theme"] = card_type if card_type in CARD_TYPES else "neutral"
        elif name == "identify_card":
            candidates = result.get("candidates") or []
            dossier["candidates"] = candidates
            if len(candidates) != 1:
                dossier["theme"] = "neutral"
            if len(candidates) == 1:
                dossier["card"] = candidates[0]
        elif name == "get_graded_market_prices":
            dossier["prices"] = {
                "raw_market_price": result.get("raw_market_price"),
                "raw_condition_priced": result.get("raw_condition_priced"),
                "graded_prices": result.get("graded_prices") or {},
                "caveat": result.get("caveat"),
            }
            if result.get("card"):
                dossier["card"] = result["card"]
        elif name == "score_card_condition":
            dossier["grade"] = {
                "grade_distribution": result.get("grade_distribution"),
                "most_likely_grade": result.get("most_likely_grade"),
                "p_psa10": result.get("p_psa10"),
                "best_reachable_grade": result.get("best_reachable_grade"),
                "limiting_factor": result.get("limiting_factor"),
                "confidence": result.get("confidence"),
            }
        elif name == "grading_verdict":
            dossier["verdict"] = {
                "verdict": result.get("verdict"),
                "headline": result.get("headline"),
                "break_even_grade": result.get("break_even_grade"),
                "probability_of_beating_raw": result.get("probability_of_beating_raw"),
                "expected_value": result.get("expected_value"),
                "submission": result.get("submission"),
                "bundling_advice": result.get("bundling_advice"),
            }


# --- FastAPI -------------------------------------------------------------

app = FastAPI(title="GraDex")


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": MODEL,
        "backend": backend_name(),
        "api_usage": cards.usage(),
    }


@app.post("/chat")
async def chat(request: ChatRequest):
    session_id = request.session_id or str(uuid.uuid4())
    if session_id not in sessions:
        sessions[session_id] = _new_session(session_id)
    state = sessions[session_id]

    hooks = RecordToolCalls()
    try:
        # The Runner is the tool loop. The session carries the conversation, so
        # only the new message is passed in.
        result = await Runner.run(
            agent,
            request.message,
            session=state["session"],
            hooks=hooks,
            max_turns=12,
        )
        reply = (result.final_output or "").strip()
    except MaxTurnsExceeded:
        # The model kept calling tools without getting anywhere. The calls it made
        # are still recorded in `hooks`, so the UI shows what it tried.
        reply = (
            "I got stuck repeating a lookup and stopped so it would not keep spending "
            "your free price calls. Tell me the card's name and the set, plus the number "
            "printed in its bottom corner, and I will try once more."
        )
    except Exception as exc:  # noqa: BLE001 - surface the failure to the user
        reply = (
            "Something went wrong reaching the model: "
            f"{exc.__class__.__name__}. Try sending that again."
        )

    _update_dossier(state["dossier"], hooks.calls)

    # The three fields the assignment asks for, plus the dossier the UI renders.
    return {
        "response": reply,
        "session_id": session_id,
        "tool_calls": hooks.calls,
        "dossier": state["dossier"],
        "api_usage": cards.usage(),
    }


@app.post("/clear")
def clear(session_id: str | None = None):
    if session_id and session_id in sessions:
        del sessions[session_id]
    return {"status": "ok"}


if __name__ == "__main__":
    # Cloud Run supplies PORT; default to 8000 for local runs.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))