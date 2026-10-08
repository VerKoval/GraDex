"""Offline self-test: `python selftest.py`.

Exercises the parts that do not need the network or a model: the grade rubric,
the submission economics, the tool JSON schemas, and the /chat response
contract. The model call is stubbed, so this passes with no credentials and no
API key, which makes it the fastest way to check a change did not break
anything.
"""

from pathlib import Path
import json
import sys
from types import SimpleNamespace

sys.path.insert(0, ".")

import economics
import grading

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  pass  {label}")
    else:
        print(f"  FAIL  {label}{(' -- ' + detail) if detail else ''}")
        FAILURES.append(label)


FLAWLESS = {
    "centering_front": "50/50",
    "centering_back": "50/50",
    "corners": "all_sharp",
    "edges": "clean",
    "surface_front": "flawless",
    "surface_back": "flawless",
    "era": "modern_2023_plus",
    "handling": "pack_fresh_sleeved",
}


def test_rubric() -> None:
    print("\nGrade rubric")

    # Calibration: a flawless card should reproduce published gem rates.
    modern = grading.score_condition(**FLAWLESS)
    check("flawless modern 2023+ lands near the published 61% gem rate",
          0.55 <= modern["p_psa10"] <= 0.67, f"got {modern['p_psa10']}")

    vintage = grading.score_condition(**{**FLAWLESS, "era": "vintage_pre_2003"})
    check("flawless pre-2003 lands near the published 14% gem rate",
          0.10 <= vintage["p_psa10"] <= 0.19, f"got {vintage['p_psa10']}")

    # Centering is a hard ceiling, not a penalty.
    back_off = grading.score_condition(**{**FLAWLESS, "centering_back": "85/15"})
    check("back centering worse than 75/25 rules out a 10", back_off["p_psa10"] == 0.0)
    check("back centering ceiling is reported as the limiting factor",
          "back centering" in back_off["limiting_factor"])

    front_off = grading.score_condition(**{**FLAWLESS, "centering_front": "68/32"})
    check("front centering 68/32 caps at an 8", front_off["best_reachable_grade"] == 8)

    # Unknowns widen the estimate instead of condemning the card.
    unknown = grading.score_condition(
        centering_front="unsure", centering_back="unsure", corners="unsure", edges="unsure",
        surface_front="unsure", surface_back="unsure", era="unsure", handling="unsure",
    )
    check("all-unknown answers report low confidence", unknown["confidence"] == "low")
    check("all-unknown answers stay centred high rather than assuming damage",
          unknown["most_likely_grade"] >= 8, f"got {unknown['most_likely_grade']}")
    check("all-unknown answers spread mass over at least four grades",
          sum(1 for p in unknown["grade_distribution"].values() if p > 0.05) >= 4)

    # Wear outranks the ceiling when it costs more grade points.
    creased = grading.score_condition(
        centering_front="55/45", centering_back="80/20", corners="soft_or_dinged",
        edges="chipping", surface_front="scuffed_or_creased", surface_back="several_scratches",
        era="vintage_pre_2003", handling="loose_or_played",
    )
    check("a creased card blames the crease, not its centering",
          "centering" not in creased["limiting_factor"].split(";")[0])

    # Free-text centering.
    for text, expected in (("60/40", 60.0), ("dead centered", 50.0), ("55-45", 55.0),
                           ("unsure", None), ("gibberish", None)):
        ratio, _ = grading.parse_centering(text)
        check(f"centering input {text!r} reads as {expected}", ratio == expected,
              f"got {ratio}")

    # Every distribution must be a distribution.
    for label, result in (("modern", modern), ("vintage", vintage), ("unknown", unknown),
                          ("creased", creased), ("back_off", back_off)):
        total = sum(result["grade_distribution"].values())
        check(f"{label} distribution sums to 1.0", abs(total - 1.0) < 1e-6, f"got {total}")


def test_economics() -> None:
    print("\nSubmission economics")

    dist = grading.score_condition(**{**FLAWLESS, "era": "modern_2019_2022"})
    d = dist["grade_distribution"]
    probs = {
        "p10": d["psa10"], "p9": d["psa9"], "p8": d["psa8"],
        "p7": d["psa7"], "p6_or_lower": d["psa6_or_lower"],
    }

    cheap = economics.grading_economics(raw_market_price=12, psa10_price=60, psa9_price=25, **probs)
    check("a $12 card is not worth grading", cheap["verdict"] == "DON'T GRADE")
    check("a $12 card has no break-even grade", cheap["break_even_grade"] is None)

    solo = economics.grading_economics(raw_market_price=140, psa10_price=400,
                                       psa9_price=190, card_count=1, **probs)
    bulk = economics.grading_economics(raw_market_price=140, psa10_price=400,
                                       psa9_price=190, card_count=10, **probs)
    check("bundling lowers the all-in cost per card",
          bulk["submission"]["all_in_cost_per_card"] < solo["submission"]["all_in_cost_per_card"])
    check("the same card does better in a 10-card submission",
          bulk["expected_value"]["edge_from_grading"] > solo["expected_value"]["edge_from_grading"])
    check("a lone card gets bundling advice", "bundling_advice" in solo)

    # Declared value follows expected value, not the long-shot PSA 10 price.
    chase = economics.grading_economics(raw_market_price=3000, psa10_price=17500,
                                        psa9_price=2500, p10=0.01, p9=0.15, p8=0.55,
                                        p7=0.25, p6_or_lower=0.04)
    check("a long-shot PSA 10 price does not push the card into a premium tier",
          chase["submission"]["grading_fee_per_card"] < 999.0,
          chase["submission"]["service_level"])
    check("the insurance trade-off is surfaced when the 10 is worth far more",
          "insurance_tradeoff" in chase["submission"])

    # Missing data must refuse rather than invent.
    no_comps = economics.grading_economics(raw_market_price=30, psa10_price=0,
                                           psa9_price=0, **probs)
    check("no graded comps returns an error instead of a verdict", "error" in no_comps)

    no_probs = economics.grading_economics(raw_market_price=100, psa10_price=500,
                                           psa9_price=200, p10=0, p9=0, p8=0, p7=0,
                                           p6_or_lower=0)
    check("no grade probabilities returns an error", "error" in no_probs)

    partial = economics.grading_economics(raw_market_price=140, psa10_price=0,
                                          psa9_price=190, **probs)
    check("a missing PSA 10 comp is flagged as a conservative lower bound",
          any("lower bound" in a for a in partial["assumptions"]))

    check("the suspended Value tier is explained rather than silently skipped",
          "suspended" in solo["submission"].get("note", ""))
    check("fee data carries an as-of date", bool(solo["fees_as_of"]))


def test_server_contract() -> None:
    print("\nServer contract")
    from fastapi.testclient import TestClient

    import app as server

    for tool in server.TOOLS:
        schema = tool.params_json_schema
        check(f"tool {tool.name} exposes a schema", bool(schema.get("properties")))
        check(f"tool {tool.name} has a substantive description",
              bool(tool.description) and len(tool.description) > 80)

    enums = next(t for t in server.TOOLS if t.name == "score_card_condition").params_json_schema["properties"]["corners"].get("enum")
    check("condition answers are a closed enum, not free text", bool(enums))

    # Stub the model: run the real tools, skip the LLM.
    async def fake_run(agent, message, session=None, hooks=None, max_turns=12):
        # Round-trips through JSON the way the real tool wrapper does.
        scored = json.loads(json.dumps(
            grading.score_condition(**{**FLAWLESS, "era": "modern_2019_2022"}),
            default=str,
        ))
        await hooks.on_tool_end(
            SimpleNamespace(tool_arguments=json.dumps(FLAWLESS)),
            agent, SimpleNamespace(name="score_card_condition"), json.dumps(scored),
        )
        d = scored["grade_distribution"]
        verdict = economics.grading_economics(
            raw_market_price=140, psa10_price=400, psa9_price=190, p10=d["psa10"],
            p9=d["psa9"], p8=d["psa8"], p7=d["psa7"], p6_or_lower=d["psa6_or_lower"],
        )
        await hooks.on_tool_end(
            SimpleNamespace(tool_arguments=json.dumps({"raw_market_price": 140})),
            agent, SimpleNamespace(name="grading_verdict"), json.dumps(verdict),
        )
        return SimpleNamespace(final_output="Stubbed reply.")

    server.Runner.run = staticmethod(fake_run)
    client = TestClient(server.app)

    body = client.post("/chat", json={"message": "Charizard VMAX"}).json()
    for field in ("response", "session_id", "tool_calls"):
        check(f"/chat returns {field}", field in body)
    check("tool_calls records name, args and result",
          all({"name", "args", "result"} <= set(c) for c in body["tool_calls"]))
    check("two tool calls were recorded", len(body["tool_calls"]) == 2)
    check("the dossier picked up the grade estimate", body["dossier"]["grade"] is not None)
    check("the dossier picked up the verdict", body["dossier"]["verdict"] is not None)

    # Session continuity and isolation.
    sid = body["session_id"]
    again = client.post("/chat", json={"message": "and the price?", "session_id": sid}).json()
    check("a returning session keeps its id", again["session_id"] == sid)

    other = client.post("/chat", json={"message": "different card"}).json()
    check("a new conversation gets a different session id", other["session_id"] != sid)
    check("sessions are stored separately", len(server.sessions) >= 2)

    client.post("/clear", params={"session_id": sid})
    check("clearing removes only that session", sid not in server.sessions)

    health = client.get("/health").json()
    check("/health reports api usage", "api_usage" in health)


# One card in the shape the JustTCG v2 docs describe, used to test parsing
# offline. If the live API ever disagrees with this, the parser is what to fix.
JUSTTCG_V2_SAMPLE = {
    "data": [{
        "id": "9b2e4d1a-1111-4a5b-aaaa-000000000000",
        "slug": "pokemon-base-set-charizard-holo-rare",
        "name": "Charizard",
        "game": {"id": "pokemon", "name": "Pokemon"},
        "set": {"id": "base-set-pokemon", "name": "Base Set"},
        "number": "4",
        "rarity": "Rare Holo",
        "external_ids": {"tcgplayer": "42445", "scryfall": None},
        "variants": [
            {"id": "v1", "type": "raw", "condition": "Near Mint", "printing": "Holofoil",
             "grading": None,
             "markets": [{"region": "NA", "currency": "USD", "price": 2950.0,
                          "price_status": "current", "updated_at": 1791331200}]},
            {"id": "v2", "type": "raw", "condition": "Lightly Played", "printing": "Holofoil",
             "grading": None,
             "markets": [{"region": "NA", "currency": "USD", "price": 1800.0,
                          "price_status": "current", "updated_at": 1791331200}]},
            {"id": "v3", "type": "graded", "condition": None, "printing": "Holofoil",
             "grading": {"company": "PSA", "grade": 10, "canonical": "PSA 10"},
             "markets": [
                 {"region": "EU", "currency": "EUR", "price": None, "price_status": "unpriced"},
                 {"region": "NA", "currency": "USD", "price": 17500.0,
                  "price_status": "current", "updated_at": 1791331200}]},
            {"id": "v4", "type": "graded", "condition": None, "printing": "Holofoil",
             "grading": {"company": "PSA", "grade": 9, "canonical": "PSA 9"},
             "markets": [{"region": "NA", "currency": "USD", "price": 2487.5,
                          "price_status": "current", "updated_at": 1791331200}]},
            {"id": "v5", "type": "graded", "condition": None, "printing": "Holofoil",
             "grading": {"company": "BGS", "grade": 9.5, "canonical": "BGS 9.5"},
             "markets": [{"region": "NA", "currency": "USD", "price": 6000.0,
                          "price_status": "current", "updated_at": 1791331200}]},
            {"id": "v6", "type": "graded", "condition": None, "printing": "Holofoil",
             "grading": {"company": "PSA", "grade": 8, "canonical": "PSA 8"},
             "markets": [{"region": "NA", "currency": "USD", "price": None,
                          "price_status": "unpriced"}]},
        ],
    }],
}


def test_justtcg_parsing() -> None:
    """Parse the documented JustTCG v2 shape without touching the network."""
    print("\nJustTCG response parsing")
    import os

    import cards

    os.environ["JUSTTCG_API_KEY"] = "tcg_offline_test_key"
    cards._cache.clear()
    captured = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return JUSTTCG_V2_SAMPLE

    def fake_request(url, params=None, headers=None, timeout=None):
        # Stubbed at the transport, so the real _get builds the request.
        captured.clear()
        captured.update(params or {})
        captured["__url"] = url
        captured["__auth_header"] = (headers or {}).get("x-api-key")
        return FakeResponse()

    real_request = cards.requests.get
    cards.requests.get = fake_request
    try:
        found = cards.identify("charizard", number="4")
        check("the search sends q, game and number",
              captured.get("q") == "charizard" and captured.get("game") == "pokemon"
              and captured.get("number") == "4", str(captured))
        check("the request goes to the v2 cards endpoint",
              captured.get("__url", "").endswith("/v2/cards"), captured.get("__url"))
        check("the key is sent as the x-api-key header",
              captured.get("__auth_header") == "tcg_offline_test_key")
        candidate = (found.get("candidates") or [{}])[0]
        check("the card id comes from the v2 uuid", candidate.get("card_id", "").startswith("9b2e"))
        check("the nested set object reads as a name", candidate.get("set") == "Base Set")
        check("the Near Mint variant sets the raw price",
              candidate.get("raw_market_price") == 2950.0, str(candidate.get("raw_market_price")))

        cards._cache.clear()
        priced = cards.graded_prices(card_id="9b2e4d1a-1111-4a5b-aaaa-000000000000")
        check("the price request asks for PSA graded variants",
              captured.get("graded") == "include" and captured.get("grading_company") == "PSA")
        graded = priced.get("graded_prices") or {}
        check("the PSA 10 price is read from the NA market",
              graded.get("psa10", {}).get("price") == 17500.0, str(graded.get("psa10")))
        check("the PSA 9 price is read", graded.get("psa9", {}).get("price") == 2487.5)
        check("a BGS variant is not counted as a PSA price", "psa9_5" not in graded
              and all(abs(v.get("price", 0) - 6000.0) > 0.01 for v in graded.values()))
        check("an unpriced PSA 8 is omitted rather than reported as zero",
              "psa8" not in graded)
        check("the unpriced PSA 8 is explained in the caveat",
              "PSA 8" in (priced.get("caveat") or ""), priced.get("caveat"))
        check("the result says these are market prices, not sales",
              "not medians of completed sales" in priced.get("data_source", ""))
    finally:
        cards.requests.get = real_request
        cards._cache.clear()


def test_model_backends() -> None:
    print("\nModel backend selection")
    import app as server

    vertex_model, vertex_settings = server.build_model_and_settings("vertex_ai/gemini-3.5-flash-lite")
    check("Vertex gets its location argument",
          (vertex_settings.extra_args or {}).get("vertex_location") is not None)

    for name in ("gemini/gemini-3.5-flash-lite", "ollama_chat/qwen2.5:7b"):
        _, settings = server.build_model_and_settings(name)
        check(f"{name} does not receive the Vertex-only location argument",
              not (settings.extra_args or {}).get("vertex_location"))

    check("a gemini/ model is labelled as the free API-key route",
          "free" in server.backend_name("gemini/gemini-3.5-flash-lite").lower())
    check("a vertex model is labelled as needing billing",
          "billed" in server.backend_name("vertex_ai/x").lower())
    check("an ollama model is labelled local",
          "local" in server.backend_name("ollama_chat/qwen2.5:7b").lower())


def test_collector_number_matching() -> None:
    """A number typed as printed ('34/119') must find a catalog entry stored as '034'."""
    print("\nCollector number matching")
    import os

    import cards

    os.environ["JUSTTCG_API_KEY"] = "tcg_offline_test_key"
    catalog = [
        {"id": "u1", "name": "Gengar EX", "set": {"name": "Phantom Forces"}, "number": "034",
         "rarity": "Rare Holo EX", "variants": []},
        {"id": "u2", "name": "Gengar EX", "set": {"name": "Fates Collide"}, "number": "108",
         "rarity": "Rare Holo EX", "variants": []},
    ]
    calls: list[dict] = []

    class Response:
        status_code = 200

        def __init__(self, rows):
            self.rows = rows

        def json(self):
            return {"data": self.rows}

    def strict_catalog(url, params=None, headers=None, timeout=None):
        # Exact-match number filter, like a strict API would apply.
        calls.append(dict(params or {}))
        rows = catalog
        if (params or {}).get("number"):
            rows = [c for c in rows if c["number"] == params["number"]]
        return Response(rows)

    real = cards.requests.get
    cards.requests.get = strict_catalog
    try:
        for typed in ("34/119", "034/119", "34"):
            cards._cache.clear()
            calls.clear()
            found = cards.identify("gengar ex", number=typed)
            check(f"{typed!r} finds the Phantom Forces printing",
                  found.get("resolved") is True
                  and found["candidates"][0]["set"] == "Phantom Forces", str(found)[:140])
            check(f"{typed!r} takes at most two API calls", len(calls) <= 2, str(len(calls)))

        cards._cache.clear()
        missing = cards.identify("gengar ex", number="99/119")
        check("a number that does not exist is not an error", "error" not in missing)
        check("it still returns the name matches for the user to choose from",
              missing.get("match_count") == 2 and missing.get("resolved") is False)
        check("it tells the model not to search again", "Do not search again" in
              missing.get("number_note", ""))

        catalog.clear()
        cards._cache.clear()
        nothing = cards.identify("gengar ex", number="34/119")
        check("a search with no results is an error that forbids blind retries",
              "error" in nothing and "Do not search again" in nothing["error"])
    finally:
        cards.requests.get = real
        cards._cache.clear()

    check("the key reducer treats padding and totals as equal",
          cards._number_key("034/119") == cards._number_key("34") == "34")
    check("the key reducer keeps promo prefixes", cards._number_key("SWSH123") == "swsh123")


def test_api_errors() -> None:
    print("\nAPI error handling")
    import os

    import cards

    saved = os.environ.pop("JUSTTCG_API_KEY", None)
    try:
        missing = cards.identify("charizard")
        check("a missing API key returns an actionable error, not an exception",
              "error" in missing and "JUSTTCG_API_KEY" in missing["error"])
        check("the missing-key error tells the model what still works",
              "grade estimate" in missing["error"])
        blank = cards.identify("")
        check("an empty description is rejected before any request", "error" in blank)
        no_id = cards.graded_prices()
        check("prices without a card id are rejected", "error" in no_id)
    finally:
        if saved is not None:
            os.environ["JUSTTCG_API_KEY"] = saved


def test_themes() -> None:
    print("\nType themes")
    import re
    import app as server
    tool = next(t for t in server.TOOLS if t.name == "set_card_theme")
    enum = tool.params_json_schema["properties"]["card_type"].get("enum") or []
    check("set_card_theme offers 11 types plus unknown", len(enum) == 12 and "unknown" in enum)

    d = server._new_session("t")["dossier"]
    check("dossier starts neutral", d["theme"] == "neutral")
    server._update_dossier(d, [{"name": "set_card_theme", "args": {}, "result": {"card_type": "fire", "theme_set": True}}])
    check("dossier takes the type", d["theme"] == "fire")
    server._update_dossier(d, [{"name": "set_card_theme", "args": {}, "result": {"card_type": "unknown", "theme_set": False}}])
    check("unknown type goes back to neutral", d["theme"] == "neutral")

    html = (Path(__file__).parent / "index.html").read_text()
    for t in server.CARD_TYPES + ("neutral",):
        check(f"{t}: CSS block and glyph present",
              f'data-type="{t}"' in html and f'"{t}": "<' in html)

    def lum(h):
        ch = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        ch = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]

    def ratio(a, b):
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    for m in re.finditer(r':root(?:, :root)?\[data-type="(\w+)"\] \{(.*?)\}', html, re.S):
        name, body = m.group(1), m.group(2)
        v = dict(re.findall(r"--([\w-]+): (#[0-9a-f]{6});", body))
        pairs = [("ink", "panel", 7), ("ink", "agent", 7), ("ink-soft", "agent", 4.5),
                 ("accent", "panel", 4.5), ("accent-ink", "accent", 4.5), ("user-ink", "user", 4.5)]
        worst = min(ratio(v[a], v[b]) - need for a, b, need in pairs)
        check(f"{name}: text contrast meets thresholds", worst >= 0, f"short by {worst:.2f}")


if __name__ == "__main__":
    test_rubric()
    test_economics()
    test_server_contract()
    test_justtcg_parsing()
    test_model_backends()
    test_collector_number_matching()
    test_api_errors()
    test_themes()
    print("\n" + ("-" * 60))
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("All checks passed.")