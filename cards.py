"""JustTCG client: card identification and raw plus graded market prices.

One endpoint does both jobs. GET /v2/cards returns a card with a `variants`
array in which each variant is either `type: "raw"` (a condition such as Near
Mint) or `type: "graded"` (with a `grading` object naming the company and the
grade). Asking for `graded=include` therefore gets the raw price and the PSA
prices in a single request, which matters on a free tier of 100 calls a day.

What this data is, and is not: JustTCG reports observed market prices per
variant, not a median of completed eBay sales. There is no sale count. The
honest signal it does give is `price_status` ("current", "expired",
"unpriced") and an `updated_at` timestamp, and both are passed through so the
agent can say how fresh a number is instead of implying it watched sales.

The v2 endpoint is in beta, so every field is read defensively and a missing
one degrades the result rather than raising.
"""

import os
import time

import requests

API_BASE = "https://api.justtcg.com/v2"
GAME = "pokemon"
CACHE_TTL_SECONDS = 6 * 60 * 60
REQUEST_TIMEOUT = 20

# Free tier allows 20 results per request; higher plans allow more.
MAX_LIMIT = 20

# Grades worth pricing. Below 7 a slab rarely beats the raw card.
GRADES_OF_INTEREST = (10, 9, 8, 7)

_cache: dict[tuple, tuple[float, dict]] = {}
_calls_made = 0
_cache_hits = 0


def usage() -> dict:
    """Call accounting for the UI. Resets when the process restarts."""
    return {
        "api_calls_made": _calls_made,
        "cache_hits": _cache_hits,
        "free_tier_daily_calls": 100,
        "cached_entries": len(_cache),
    }


def _api_key() -> str | None:
    key = os.environ.get("JUSTTCG_API_KEY", "").strip()
    return key or None


def _get(params: dict) -> dict:
    """GET /v2/cards with caching and errors a model can act on.

    Returns {"ok": True, "payload": ...} or {"ok": False, "error": ...} where
    the error is a sentence the agent can relay to the user as-is.
    """
    global _calls_made, _cache_hits

    key = _api_key()
    if not key:
        return {
            "ok": False,
            "error": (
                "The card price service is not configured: the JUSTTCG_API_KEY "
                "environment variable is unset. Card identification and the grade "
                "estimate both need it, so only the condition rubric will work. Tell "
                "the user the deployment is missing its API key."
            ),
        }

    query = {"game": GAME, **params}
    cache_key = tuple(sorted((k, str(v)) for k, v in query.items()))
    hit = _cache.get(cache_key)
    if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
        _cache_hits += 1
        return {"ok": True, "payload": hit[1], "from_cache": True}

    try:
        response = requests.get(
            f"{API_BASE}/cards",
            params=query,
            headers={"x-api-key": key, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.Timeout:
        return {
            "ok": False,
            "error": (
                "The card price service did not respond within 20 seconds. Suggest "
                "trying again in a moment; do not invent prices."
            ),
        }
    except requests.RequestException as exc:
        return {
            "ok": False,
            "error": (
                f"Could not reach the card price service ({exc.__class__.__name__}). "
                "Suggest trying again shortly; do not invent prices."
            ),
        }

    _calls_made += 1

    if response.status_code in (401, 403):
        return {
            "ok": False,
            "error": (
                f"The card price service rejected the API key (HTTP {response.status_code}). "
                "It is missing, mistyped, expired, or lacks access to this endpoint. Tell "
                "the user the deployment needs a valid key; do not invent prices."
            ),
        }
    if response.status_code == 404:
        return {
            "ok": False,
            "error": (
                "The card price service has no record of that card or variant. If a "
                "card_id was used, it may be wrong or the variant may never have been "
                "priced. Try identifying the card again by name."
            ),
        }
    if response.status_code == 429:
        return {
            "ok": False,
            "error": (
                "The card price service rate limit is used up (HTTP 429). The free tier "
                "allows 100 calls a day. The grade estimate still works without prices. "
                "Tell the user prices will be available again later; do not invent them."
            ),
        }
    if response.status_code >= 500:
        return {
            "ok": False,
            "error": (
                f"The card price service is having problems (HTTP {response.status_code}). "
                "Suggest trying again shortly; do not invent prices."
            ),
        }
    if response.status_code != 200:
        detail = ""
        try:  # Errors use application/problem+json.
            body = response.json()
            detail = str(body.get("detail") or body.get("title") or "")[:160]
        except ValueError:
            pass
        return {
            "ok": False,
            "error": (
                f"The card price service returned HTTP {response.status_code}"
                f"{': ' + detail if detail else ''}. Do not invent prices."
            ),
        }

    try:
        payload = response.json()
    except ValueError:
        return {
            "ok": False,
            "error": "The card price service returned a response that was not JSON.",
        }

    _cache[cache_key] = (time.time(), payload)
    return {"ok": True, "payload": payload, "from_cache": False}


def _rows(payload: dict) -> list[dict]:
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _text(value) -> str | None:
    """v2 nests set and game as {id, name}; v1 returned plain strings."""
    if isinstance(value, dict):
        return value.get("name") or value.get("id")
    if isinstance(value, str):
        return value
    return None


def _primary_market(variant: dict) -> dict:
    """The first market entry is the primary one; prefer NA when present."""
    markets = variant.get("markets")
    if not isinstance(markets, list) or not markets:
        return {}
    for market in markets:
        if isinstance(market, dict) and str(market.get("region", "")).upper() == "NA":
            return market
    first = markets[0]
    return first if isinstance(first, dict) else {}


def _price_of(variant: dict) -> tuple[float | None, str | None, int | None]:
    """Return (price, status, updated_at) for a variant, across v1 and v2 shapes."""
    market = _primary_market(variant)
    if market:
        price = market.get("price")
        if price is None:
            price = market.get("last_price")
        status = market.get("price_status")
        updated = market.get("updated_at")
    else:  # v1 flat shape, kept so a fallback response still parses.
        price = variant.get("price")
        if price is None:
            price = variant.get("lastKnownPrice")
        status = variant.get("priceStatus")
        updated = variant.get("lastUpdated")

    if not isinstance(price, (int, float)) or price <= 0:
        price = None
    if not isinstance(updated, (int, float)):
        updated = None
    return (float(price) if price is not None else None,
            str(status) if status else None,
            int(updated) if updated is not None else None)


def _is_graded(variant: dict) -> bool:
    if str(variant.get("type", "")).lower() == "graded":
        return True
    return isinstance(variant.get("grading"), dict)


def _raw_price(card: dict) -> tuple[float | None, str | None]:
    """Near Mint price if there is one, else the best-priced raw variant."""
    variants = card.get("variants")
    if not isinstance(variants, list):
        return None, None

    best: tuple[float, str] | None = None
    for variant in variants:
        if not isinstance(variant, dict) or _is_graded(variant):
            continue
        price, status, _ = _price_of(variant)
        if price is None:
            continue
        condition = variant.get("condition") or "Ungraded"
        if str(condition).lower().startswith("near mint"):
            return price, condition
        if best is None or price > best[0]:
            best = (price, str(condition))
    return (best[0], best[1]) if best else (None, None)


def _summarise(card: dict) -> dict:
    raw_price, raw_condition = _raw_price(card)
    externals = card.get("external_ids") if isinstance(card.get("external_ids"), dict) else {}
    return {
        # v2 ids are UUIDs; the legacy slug also resolves, so prefer whichever exists.
        "card_id": str(card.get("id") or card.get("slug") or card.get("uuid") or ""),
        "name": card.get("name"),
        "set": _text(card.get("set")) or card.get("set_name"),
        "number": card.get("number"),
        "rarity": card.get("rarity"),
        "raw_market_price": round(raw_price, 2) if raw_price is not None else None,
        "raw_condition_priced": raw_condition,
        "tcgplayer_id": externals.get("tcgplayer") or card.get("tcgplayerId"),
        "image": card.get("image") or card.get("image_url"),
    }


def _disambiguation_hint(candidates: list[dict]) -> str | None:
    """Name the single most discriminating question for this candidate set."""
    if len(candidates) <= 1:
        return None
    sets = {c.get("set") for c in candidates if c.get("set")}
    numbers = {c.get("number") for c in candidates if c.get("number")}
    rarities = {c.get("rarity") for c in candidates if c.get("rarity")}

    if len(numbers) > 1:
        return (
            "Ask the user for the collector number printed in the bottom corner of the "
            "card, for example 074/073. That one answer separates these printings faster "
            "than anything else."
        )
    if len(sets) > 1:
        return (
            "Ask the user which set the card is from, or what the small set symbol next "
            "to the collector number looks like."
        )
    if len(rarities) > 1:
        return (
            "Ask the user whether the card is textured or flat, and whether the artwork "
            "fills the whole card (alternate or full art) or sits in a window."
        )
    return "Ask the user for the collector number to confirm which printing this is."


def _number_key(value) -> str:
    """Reduce a printed collector number to a comparable key.

    '034/119', '34/119', ' 34 ' and '034' all become '34', so a number typed the
    way it is printed on the card matches however the catalog stores it.
    """
    text = str(value or "").strip().lower().split("/")[0].strip()
    if not text:
        return ""
    return text.lstrip("0") or "0"


def _candidates_from(outcome: dict) -> list[dict]:
    return [_summarise(card) for card in _rows(outcome["payload"])]


def identify(query: str, set_name: str = "", number: str = "", limit: int = 8) -> dict:
    """Find candidate printings matching a described card.

    A collector number is matched forgivingly, because the user types it the way
    it is printed ('34/119') and the catalog may store '34' or '034'. That takes
    at most two API calls inside this one tool call, never a retry loop driven
    by the model.
    """
    text = (query or "").strip()
    if not text:
        return {
            "error": (
                "No card description was given. Ask the user for the Pokemon's name, and "
                "the set or collector number if they have it."
            )
        }

    base: dict = {"q": text, "limit": max(1, min(int(limit or 8), MAX_LIMIT))}
    if set_name and set_name.strip():
        base["set"] = set_name.strip()

    want = _number_key(number)
    candidates: list[dict] = []
    number_matched = False
    from_cache = False

    if want:
        # First try the server-side filter with the cleaned number.
        outcome = _get({**base, "number": want})
        if not outcome["ok"]:
            return {"error": outcome["error"], "query": text}
        candidates = _candidates_from(outcome)
        from_cache = outcome.get("from_cache", False)
        number_matched = bool(candidates)

        if not candidates:
            # The catalog may store the number padded or in another form, so look
            # again without the filter and compare the numbers here instead.
            outcome = _get(base)
            if not outcome["ok"]:
                return {"error": outcome["error"], "query": text}
            everything = _candidates_from(outcome)
            from_cache = from_cache and outcome.get("from_cache", False)
            candidates = [c for c in everything if _number_key(c.get("number")) == want]
            number_matched = bool(candidates)
            if not candidates:
                candidates = everything
    else:
        outcome = _get(base)
        if not outcome["ok"]:
            return {"error": outcome["error"], "query": text}
        candidates = _candidates_from(outcome)
        from_cache = outcome.get("from_cache", False)

    if not candidates:
        return {
            "query": text,
            "candidates": [],
            "match_count": 0,
            "error": (
                f"No Pokemon card matched '{text}'. Do not search again with small "
                "variations of the same words. Ask the user one question instead: the "
                "exact card name as printed, or the set name."
            ),
        }

    result = {
        "query": text,
        "candidates": candidates,
        "match_count": len(candidates),
        "resolved": len(candidates) == 1,
        "served_from_cache": from_cache,
        "price_note": (
            "Prices here are raw market prices. Graded prices come from "
            "get_graded_market_prices."
        ),
    }

    if want and not number_matched:
        # Not an error: the name matched, so the user can still choose.
        result["resolved"] = False
        result["number_note"] = (
            f"None of these printings has collector number {number!r} in the catalog. "
            "Do not search again. Read the options back to the user (set and number) and "
            "ask which matches, or ask them to re-check the number and the set."
        )

    hint = _disambiguation_hint(candidates)
    if hint and not result["resolved"]:
        result["next_question"] = hint
        result["why"] = (
            "Several printings share this name. Art, set and year change the value a lot, "
            "so the printing must be pinned down before any price is quoted."
        )
    return result


def graded_prices(card_id: str = "", query: str = "") -> dict:
    """Raw and PSA-graded market prices for one specific printing."""
    card_id = (card_id or "").strip()
    text = (query or "").strip()

    params: dict = {"graded": "include", "grading_company": "PSA", "limit": 1}
    if card_id:
        params["card_id"] = card_id
    elif text:
        params["q"] = text
    else:
        return {
            "error": (
                "Neither a card_id nor a query was given. Call identify_card first and "
                "pass the card_id of the printing the user confirmed."
            )
        }

    outcome = _get(params)
    if not outcome["ok"]:
        return {"error": outcome["error"], "card_id": card_id or None, "query": text or None}

    rows = _rows(outcome["payload"])
    if not rows:
        return {
            "error": (
                f"No price record was found for {card_id or text!r}. Confirm the card_id "
                "came from identify_card."
            )
        }

    card = rows[0]
    summary = _summarise(card)
    variants = card.get("variants") if isinstance(card.get("variants"), list) else []

    graded: dict[str, dict] = {}
    unpriced: list[str] = []
    stale: list[str] = []
    now = time.time()

    for variant in variants:
        if not isinstance(variant, dict) or not _is_graded(variant):
            continue
        grading = variant.get("grading") if isinstance(variant.get("grading"), dict) else {}
        company = str(grading.get("company") or "").upper()
        if company and company != "PSA":
            continue
        try:
            grade = int(float(grading.get("grade")))
        except (TypeError, ValueError):
            continue
        if grade not in GRADES_OF_INTEREST:
            continue

        price, status, updated = _price_of(variant)
        label = f"psa{grade}"
        if price is None:
            unpriced.append(f"PSA {grade}")
            continue
        # Keep the highest price when several variants map to one grade.
        if label in graded and graded[label]["price"] >= price:
            continue
        entry = {"price": round(price, 2), "price_status": status}
        if updated:
            entry["updated"] = time.strftime("%Y-%m-%d", time.gmtime(updated))
            age_days = int((now - updated) / 86400)
            entry["age_days"] = age_days
            if age_days > 60:
                stale.append(f"the PSA {grade} price was last updated {age_days} days ago")
        if status and status != "current":
            stale.append(f"the PSA {grade} price is marked '{status}' rather than current")
        graded[label] = entry

    result = {
        "card": summary,
        "raw_market_price": summary["raw_market_price"],
        "raw_condition_priced": summary["raw_condition_priced"],
        "graded_prices": graded,
        "served_from_cache": outcome.get("from_cache", False),
        "data_source": (
            "JustTCG observed market prices per variant. These are market prices, not "
            "medians of completed sales, and there is no sale count. Describe them as "
            "what the card is listed and trading at, not as recorded sales."
        ),
    }

    caveats = stale[:]
    if unpriced:
        caveats.append(
            "no price is recorded for " + ", ".join(unpriced) +
            ", which usually means few or none have traded"
        )
    if not graded:
        result["caveat"] = (
            "No PSA prices are recorded for this printing at all. That usually means the "
            "card is too new or too inexpensive for anyone to be grading it, which is "
            "itself an argument against grading. Say so rather than guessing a price."
        )
    elif caveats:
        result["caveat"] = (
            "Treat these as soft comps: " + "; ".join(caveats) +
            ". Mention the weak data when giving the verdict."
        )

    if summary["raw_market_price"] is None:
        result["raw_price_missing"] = (
            "No raw market price is recorded for this printing, so the comparison against "
            "selling it ungraded is unavailable. Ask the user what they could sell it for "
            "raw, or say the comparison cannot be made."
        )

    return result