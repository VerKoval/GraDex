"""Terminal harness for testing the agent without the frontend.

    uv run cli.py --check-api      one real JustTCG call; no model needed
    uv run cli.py                  interactive conversation with the agent
    uv run cli.py --script basic   a scripted multi-turn run, no typing
    uv run cli.py --http           drive the running FastAPI server instead

It talks to the same Agent and the same SQLiteSession that app.py serves, so
what you see here is the conversation logic the web UI would get.

In the conversation, lines starting with / are commands, not messages:

    /dossier   print the running dossier
    /calls     print the last turn's tool calls in full
    /raw       toggle full JSON for every tool result
    /new       start a fresh session
    /quit      leave
"""

import argparse
import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) or ".")

# LiteLLM is chatty and folds a full traceback into its exception messages,
# which buries the one line that says what to fix. Quiet it before the agent
# imports it.
os.environ.setdefault("LITELLM_LOG", "ERROR")
import logging  # noqa: E402

logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)
try:
    import litellm

    litellm.suppress_debug_info = True
except Exception:  # noqa: BLE001 - quieting logs is never worth a crash
    pass

DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
OFF = "\033[0m"

if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    DIM = BOLD = RED = GREEN = YELLOW = CYAN = OFF = ""


def model_error_hints(detail, model):
    """What to try, chosen from what the error actually says."""
    lowered = detail.lower()
    free_route = (
        "Free option with no billing: get a key at aistudio.google.com, then "
        "$env:GEMINI_API_KEY = \"your-key\" and run again."
    )
    if "billing" in lowered:
        return ["The Google Cloud project has no billing account linked.", free_route]
    if "default credentials" in lowered:
        return ["Run: gcloud auth application-default login", free_route]
    if "api key" in lowered or "api_key" in lowered or "permission_denied" in lowered:
        return ["The Gemini key was rejected. Check it was copied in full.", free_route]
    if "not found" in lowered or "404" in lowered:
        return [
            f"The model name '{model}' was not found for this backend.",
            "Try another: $env:MODEL = \"gemini/gemini-2.5-flash\" and run again.",
        ]
    if "429" in lowered or "quota" in lowered or "rate" in lowered:
        return ["Rate limit hit. The free tier has per-minute caps; wait a minute and retry."]
    if "connection" in lowered and model.startswith("ollama"):
        return ["Ollama is not running. Start it, or run: ollama serve"]
    return []


def shorten(value, width=88):
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    return text if len(text) <= width else text[: width - 1] + "…"


def print_tool_calls(calls, raw=False):
    for call in calls:
        name = call.get("name", "?")
        args = call.get("args", {})
        result = call.get("result")

        is_error = isinstance(result, dict) and result.get("error")
        tint = RED if is_error else CYAN
        print(f"  {tint}{name}{OFF}{DIM}({shorten(args, 70)}){OFF}")

        if raw:
            for line in json.dumps(result, indent=2, default=str).splitlines():
                print(f"    {DIM}{line}{OFF}")
            continue

        if is_error:
            print(f"    {RED}{result['error']}{OFF}")
        elif isinstance(result, dict):
            for line in summarise(name, result):
                print(f"    {DIM}{line}{OFF}")


def summarise(name, r):
    """One or two lines per tool result, enough to see if it did the right thing."""
    if name == "identify_card":
        lines = [f"{r.get('match_count', 0)} candidate(s)"]
        for c in (r.get("candidates") or [])[:6]:
            lines.append(
                f"  {c.get('name')} | {c.get('set')} | #{c.get('number')} | "
                f"{c.get('rarity')} | raw {c.get('raw_market_price')} | id={c.get('card_id')}"
            )
        if r.get("next_question"):
            lines.append(f"ask: {r['next_question'][:100]}")
        return lines
    if name == "set_card_theme":
        return [f"theme -> {r.get('card_type')}" + ("" if r.get("theme_set") else " (stays neutral)")]
    if name == "get_graded_market_prices":
        card = r.get("card") or {}
        lines = [f"{card.get('name')} | raw {r.get('raw_market_price')} "
                 f"({r.get('raw_condition_priced')})"]
        graded = r.get("graded_prices") or {}
        lines.append("graded: " + (", ".join(
            f"{k}={v.get('price')}({v.get('price_status') or '?'})" for k, v in graded.items()
        ) or "none recorded"))
        if r.get("caveat"):
            lines.append(f"caveat: {r['caveat'][:110]}")
        return lines
    if name == "score_card_condition":
        dist = r.get("grade_distribution") or {}
        return [
            " ".join(f"{k.replace('psa', '')}={v:.0%}" for k, v in dist.items()),
            f"most likely {r.get('most_likely_grade')} | ceiling {r.get('best_reachable_grade')} "
            f"| confidence {r.get('confidence')}",
            f"limiting: {str(r.get('limiting_factor'))[:110]}",
        ]
    if name == "grading_verdict":
        ev = r.get("expected_value") or {}
        sub = r.get("submission") or {}
        return [
            f"{r.get('verdict')} | break-even PSA {r.get('break_even_grade')} | "
            f"P(beat raw) {r.get('probability_of_beating_raw')}",
            f"graded net {ev.get('net_after_grading_costs')} vs raw "
            f"{ev.get('net_if_sold_raw_today')} | edge {ev.get('edge_from_grading')}",
            f"{sub.get('service_level')} ${sub.get('grading_fee_per_card')} | all-in "
            f"${sub.get('all_in_cost_per_card')}",
        ]
    return [shorten(r)]


def print_dossier(dossier):
    print(f"\n{BOLD}dossier{OFF}")
    for key in ("card", "prices", "grade", "verdict"):
        value = dossier.get(key)
        if not value:
            print(f"  {DIM}{key}: empty{OFF}")
            continue
        print(f"  {key}:")
        for line in json.dumps(value, indent=2, default=str).splitlines():
            print(f"    {DIM}{line}{OFF}")


# --- API check -------------------------------------------------------------

def check_api(query):
    """One real call against JustTCG. Shows the parse and the raw payload."""
    import cards

    if not os.environ.get("JUSTTCG_API_KEY", "").strip():
        print(f"{RED}JUSTTCG_API_KEY is not set.{OFF}")
        print("  export JUSTTCG_API_KEY=tcg_your_key_here")
        return 1

    print(f"{BOLD}1. identify({query!r}){OFF}")
    found = cards.identify(query, limit=5)
    if found.get("error"):
        print(f"{RED}{found['error']}{OFF}")
        return 1
    for line in summarise("identify_card", found):
        print(f"  {line}")

    candidates = found.get("candidates") or []
    if not candidates:
        print(f"{YELLOW}No candidates, so no price lookup to try.{OFF}")
        return 1

    card_id = candidates[0]["card_id"]
    print(f"\n{BOLD}2. graded_prices(card_id={card_id!r}){OFF}")
    priced = cards.graded_prices(card_id=card_id)
    if priced.get("error"):
        print(f"{RED}{priced['error']}{OFF}")
        return 1
    for line in summarise("get_graded_market_prices", priced):
        print(f"  {line}")

    print(f"\n{BOLD}3. parsed price result{OFF}")
    print(json.dumps(priced, indent=2, default=str))

    print(f"\n{BOLD}4. raw payload of the last call{OFF}")
    print(f"{DIM}If the parse above looks wrong, this is what to check.{OFF}")
    if cards._cache:
        newest = max(cards._cache.values(), key=lambda entry: entry[0])
        print(json.dumps(newest[1], indent=2, default=str)[:4000])

    print(f"\n{GREEN}API reachable.{OFF} {cards.usage()}")
    if not priced.get("graded_prices"):
        print(f"{YELLOW}Note: no PSA prices came back for this card. Try a more valuable "
              f"card (a Base Set Charizard) before concluding graded data is missing.{OFF}")
    return 0


# --- Conversation ----------------------------------------------------------

SCRIPTS = {
    "basic": [
        "I pulled a Charizard VMAX from Champion's Path and it looks mint. Worth grading?",
        "074/073",
        "Borders look even on all sides. Corners are sharp. There is one tiny scratch on "
        "the holo under light. It has been in a sleeve since I pulled it in 2020.",
        "Just the one card.",
    ],
    "vintage": [
        "I have a 1999 Base Set Blastoise, holo, kept in a binder since I was a kid.",
        "The left border is noticeably thicker than the right. Corners have a bit of white "
        "fuzz. There are a few scratches on the front.",
        "I have about 12 other cards I could send with it.",
    ],
    "memory": [
        "I have a Base Set Charizard.",
        "What card were we just talking about?",
        "What did I say about its condition?",
    ],
    "offtopic": [
        "What is the capital of France?",
        "Okay, I have a Moonbreon from Evolving Skies. Alt art.",
    ],
}


async def converse(args):
    import app as server

    if not os.environ.get("JUSTTCG_API_KEY", "").strip():
        print(f"{YELLOW}JUSTTCG_API_KEY is not set: price lookups will fail on purpose. "
              f"The grade rubric still works.{OFF}\n")

    session_id = args.session or str(uuid.uuid4())
    state = server.sessions.setdefault(session_id, server._new_session(session_id))
    raw = args.raw
    scripted = list(SCRIPTS.get(args.script, [])) if args.script else []

    print(f"{BOLD}GraDex{OFF} {DIM}session {session_id[:8]}{OFF}")
    print(f"{DIM}model   {server.MODEL}{OFF}")
    print(f"{DIM}backend {server.backend_name()}{OFF}")
    print(f"{DIM}/dossier /calls /raw /new /quit{OFF}\n")

    last_calls = []
    while True:
        if scripted:
            message = scripted.pop(0)
            print(f"{BOLD}you>{OFF} {message}")
        else:
            if args.script:
                break
            try:
                message = input(f"{BOLD}you>{OFF} ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

        if not message:
            continue
        if message in ("/quit", "/q"):
            break
        if message == "/raw":
            raw = not raw
            print(f"{DIM}raw tool results {'on' if raw else 'off'}{OFF}")
            continue
        if message == "/dossier":
            print_dossier(state["dossier"])
            continue
        if message == "/calls":
            print(json.dumps(last_calls, indent=2, default=str))
            continue
        if message == "/new":
            session_id = str(uuid.uuid4())
            state = server.sessions.setdefault(session_id, server._new_session(session_id))
            print(f"{DIM}new session {session_id[:8]}{OFF}")
            continue

        hooks = server.RecordToolCalls()
        try:
            result = await server.Runner.run(
                server.agent, message, session=state["session"], hooks=hooks, max_turns=12
            )
            reply = (result.final_output or "").strip()
        except Exception as exc:  # noqa: BLE001
            # LiteLLM embeds a traceback in the message; the first line is the point.
            detail = str(exc).split("Traceback (most recent call last)")[0].strip()
            print(f"{RED}model call failed: {exc.__class__.__name__}{OFF}")
            print(f"{RED}{detail[:400]}{OFF}")
            for hint in model_error_hints(detail, server.MODEL):
                print(f"{YELLOW}  {hint}{OFF}")
            continue

        last_calls = hooks.calls
        server._update_dossier(state["dossier"], hooks.calls)

        if hooks.calls:
            print(f"{DIM}  -- {len(hooks.calls)} tool call(s){OFF}")
            print_tool_calls(hooks.calls, raw=raw)
        print(f"\n{GREEN}agent>{OFF} {reply}\n")

    verdict = state["dossier"].get("verdict")
    if verdict:
        print(f"{BOLD}final verdict:{OFF} {verdict.get('verdict')} — {verdict.get('headline')}")
    print(f"{DIM}{server.cards.usage()}{OFF}")


def converse_http(args):
    """Same conversation, but through the running server's /chat route."""
    import requests

    base = args.http if args.http.startswith("http") else f"http://{args.http}"
    session_id = args.session
    scripted = list(SCRIPTS.get(args.script, [])) if args.script else []
    print(f"{BOLD}GraDex{OFF} {DIM}via {base}/chat{OFF}\n")

    while True:
        if scripted:
            message = scripted.pop(0)
            print(f"{BOLD}you>{OFF} {message}")
        else:
            if args.script:
                break
            try:
                message = input(f"{BOLD}you>{OFF} ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
        if not message or message in ("/quit", "/q"):
            break

        try:
            response = requests.post(
                f"{base}/chat", json={"message": message, "session_id": session_id}, timeout=120
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            print(f"{RED}request failed: {exc}{OFF}")
            continue

        session_id = body.get("session_id")
        missing = [f for f in ("response", "session_id", "tool_calls") if f not in body]
        if missing:
            print(f"{RED}/chat response is missing required field(s): {missing}{OFF}")

        calls = body.get("tool_calls") or []
        if calls:
            print(f"{DIM}  -- {len(calls)} tool call(s){OFF}")
            print_tool_calls(calls, raw=args.raw)
        print(f"\n{GREEN}agent>{OFF} {body.get('response')}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Test the GraDex agent from a terminal.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--check-api", action="store_true",
                        help="make one real JustTCG call and show the parse; no model needed")
    parser.add_argument("--query", default="charizard",
                        help="card to look up with --check-api (default: charizard)")
    parser.add_argument("--script", choices=sorted(SCRIPTS),
                        help="run a canned conversation instead of typing")
    parser.add_argument("--http", nargs="?", const="http://localhost:8000", default=None,
                        help="drive a running server's /chat route instead of the agent")
    parser.add_argument("--session", default=None, help="resume a session id")
    parser.add_argument("--raw", action="store_true", help="print full JSON tool results")
    args = parser.parse_args()

    if args.check_api:
        sys.exit(check_api(args.query))
    if args.http:
        converse_http(args)
    else:
        asyncio.run(converse(args))


if __name__ == "__main__":
    main()