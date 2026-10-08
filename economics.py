"""PSA submission costs and the expected-value verdict.

PSA's fee schedule is not available as an API, so it lives here as a versioned
table with an `as_of` date that every result carries, because these numbers move
(the Value tiers were suspended in 2026 and the floor jumped to $59.99/card).

The decision is not "is the PSA 10 worth more than the fee". It is:

    E[net proceeds after grading]  vs.  net proceeds selling it raw today

Selling fees apply to both sides, and the grading side also carries a fee tier
chosen from the declared value, plus shipping that is mostly per *submission* --
which is why a single card is usually a bad submission and the same card inside
a stack of ten is often fine.
"""

FEES_AS_OF = "2026-10-07"
FEES_SOURCE = "https://www.psacard.com/services/tradingcardgrading"

# (name, price per card, max declared value per card, turnaround, available)
SERVICE_LEVELS = (
    ("Value Bulk", 18.99, 500, "45-55 business days", False),
    ("Value", 24.99, 500, "45-55 business days", False),
    ("Standard", 59.99, 1_000, "90-100 business days", True),
    ("Priority", 79.99, 1_500, "70-80 business days", True),
    ("Express", 199.00, 2_500, "20-30 business days", True),
    ("Super Express", 349.00, 5_000, "10-15 business days", True),
    ("Premier", 599.00, 10_000, "7-10 business days", True),
    ("Premium 1", 999.00, 25_000, "5-7 business days", True),
    ("Premium 2", 1_999.00, 50_000, "5-7 business days", True),
    ("Premium 3", 2_999.00, 100_000, "5-7 business days", True),
    ("Premium 5", 4_999.00, 250_000, "5-7 business days", True),
)

# Per-submission, not per-card. Outbound is a tracked and insured mailer;
# return is PSA's published rate for a small submission.
OUTBOUND_SHIPPING = 20.00
RETURN_SHIPPING = 29.99
SUPPLIES_PER_CARD = 2.00

# eBay final value fee plus payment processing, applied to both sides of the
# comparison so the verdict is not flattered by ignoring the cost of selling.
SELLING_FEE_RATE = 0.1325

CLUB_MEMBERSHIP = 99.00
CLUB_BULK_PRICE = 22.00
CLUB_BULK_MINIMUM = 25


def choose_service_level(declared_value: float) -> dict:
    """Cheapest currently available tier whose insurance covers the card."""
    unavailable_but_cheaper = [
        name for name, price, cap, _, available in SERVICE_LEVELS
        if not available and declared_value <= cap
    ]
    for name, price, cap, turnaround, available in SERVICE_LEVELS:
        if available and declared_value <= cap:
            return {
                "service_level": name,
                "price_per_card": price,
                "max_declared_value": cap,
                "turnaround": turnaround,
                "cheaper_tiers_suspended": unavailable_but_cheaper,
            }
    name, price, cap, turnaround, _ = SERVICE_LEVELS[-1]
    return {
        "service_level": f"{name} or above",
        "price_per_card": price,
        "max_declared_value": cap,
        "turnaround": turnaround,
        "cheaper_tiers_suspended": [],
        "note": "Declared value exceeds the published table; PSA quotes these individually.",
    }


def _net_after_selling(price: float) -> float:
    return price * (1 - SELLING_FEE_RATE)


def grading_economics(
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
) -> dict:
    """Expected value of submitting versus selling raw. Pure function, no I/O."""
    card_count = max(1, int(card_count or 1))
    raw_market_price = max(0.0, float(raw_market_price or 0))
    psa10_price = max(0.0, float(psa10_price or 0))
    psa9_price = max(0.0, float(psa9_price or 0))

    estimates: list[str] = []

    # Without a single graded comp there is nothing to compute an expected value
    # against, and inventing one would be worse than declining.
    if psa10_price <= 0 and psa9_price <= 0:
        return {
            "error": (
                "No PSA sale prices were supplied, so the expected value of grading cannot "
                "be computed. get_graded_market_prices found no recorded PSA sales for this "
                "printing, which usually means the card is too new or too inexpensive for "
                "anyone to be grading it. Tell the user that directly instead of estimating: "
                "the absence of a graded market is itself an argument against grading."
            ),
            "raw_market_price": round(raw_market_price, 2),
        }

    # One side missing is recoverable, but only conservatively: the fallback
    # never invents a premium, so the verdict comes out as a lower bound.
    if psa10_price <= 0:
        psa10_price = psa9_price
        estimates.append(
            "No PSA 10 sales on record, so the PSA 10 was treated as equal to the PSA 9. "
            "A real PSA 10 would almost certainly sell for more, so this verdict is a "
            "conservative lower bound on the upside"
        )
    if psa9_price <= 0:
        psa9_price = max(raw_market_price, psa10_price * 0.35)
        estimates.append(
            "No PSA 9 sales on record, so the PSA 9 was estimated from the raw price and "
            "35% of the PSA 10"
        )

    # Grades below 9 rarely have a usable comp. A PSA 8 of a modern card tends
    # to sell near its raw price, so that is the floor we fall back to.
    if psa8_price and psa8_price > 0:
        price8 = float(psa8_price)
    else:
        price8 = max(raw_market_price, psa9_price * 0.40)
        estimates.append("PSA 8 price estimated as the greater of raw price and 40% of the PSA 9")

    price7 = max(raw_market_price * 0.95, price8 * 0.65)
    price6 = max(raw_market_price * 0.80, price7 * 0.70)
    estimates.append(
        "PSA 7 and below estimated from the raw price, since low-grade slabs rarely sell "
        "for much more than the ungraded card"
    )

    weights = {
        10: max(0.0, float(p10 or 0)),
        9: max(0.0, float(p9 or 0)),
        8: max(0.0, float(p8 or 0)),
        7: max(0.0, float(p7 or 0)),
        6: max(0.0, float(p6_or_lower or 0)),
    }
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return {
            "error": (
                "No grade probabilities were supplied, so the expected value cannot be "
                "computed. Call score_card_condition first and pass its grade_distribution."
            )
        }
    probabilities = {g: w / total_weight for g, w in weights.items()}

    prices = {10: psa10_price, 9: psa9_price, 8: price8, 7: price7, 6: price6}

    expected_gross = sum(probabilities[g] * prices[g] for g in prices)

    # Declared value drives the fee tier, and both extremes cost money:
    # under-declaring voids the insurance the fee partly buys, over-declaring
    # pushes the card into a tier it does not need. The expected graded value is
    # the defensible middle, floored at the raw price.
    declared_value = max(expected_gross, raw_market_price, 1.0)
    tier = choose_service_level(declared_value)

    shipping_per_card = (OUTBOUND_SHIPPING + RETURN_SHIPPING) / card_count
    cost_per_card = tier["price_per_card"] + shipping_per_card + SUPPLIES_PER_CARD

    expected_net = sum(probabilities[g] * _net_after_selling(prices[g]) for g in prices)
    net_if_graded = expected_net - cost_per_card
    net_if_raw = _net_after_selling(raw_market_price)
    edge = net_if_graded - net_if_raw

    # The lowest grade that still beats selling it raw today.
    break_even_grade = None
    for grade in (6, 7, 8, 9, 10):
        if _net_after_selling(prices[grade]) - cost_per_card >= net_if_raw:
            break_even_grade = grade
            break
    probability_of_profit = (
        round(sum(p for g, p in probabilities.items() if g >= break_even_grade), 4)
        if break_even_grade is not None else 0.0
    )

    if break_even_grade is None:
        verdict = "DON'T GRADE"
        headline = (
            "No grade on the scale beats selling it raw once fees and shipping are in. "
            "The card is worth less than the cost of grading it."
        )
    elif edge > max(25.0, 0.25 * max(net_if_raw, 1.0)):
        verdict = "GRADE IT"
        headline = (
            f"Expected net is about ${net_if_graded:,.0f} graded versus ${net_if_raw:,.0f} raw, "
            f"an edge of about ${edge:,.0f} per card."
        )
    elif edge > 0:
        verdict = "MARGINAL"
        headline = (
            f"Grading comes out about ${edge:,.0f} ahead per card, which is inside the "
            "noise on comps and grade luck."
        )
    else:
        verdict = "DON'T GRADE"
        headline = (
            f"Grading comes out about ${abs(edge):,.0f} per card behind selling it raw."
        )

    result = {
        "verdict": verdict,
        "headline": headline,
        "break_even_grade": break_even_grade,
        "probability_of_beating_raw": probability_of_profit,
        "expected_value": {
            "gross_before_selling_fees": round(expected_gross, 2),
            "net_after_selling_fees": round(expected_net, 2),
            "net_after_grading_costs": round(net_if_graded, 2),
            "net_if_sold_raw_today": round(net_if_raw, 2),
            "edge_from_grading": round(edge, 2),
        },
        "submission": {
            "declared_value_used": round(declared_value, 2),
            "service_level": tier["service_level"],
            "grading_fee_per_card": tier["price_per_card"],
            "turnaround": tier["turnaround"],
            "cards_in_submission": card_count,
            "shipping_per_card": round(shipping_per_card, 2),
            "supplies_per_card": SUPPLIES_PER_CARD,
            "all_in_cost_per_card": round(cost_per_card, 2),
        },
        "prices_used": {f"psa{g}": round(prices[g], 2) for g in sorted(prices, reverse=True)},
        "grade_probabilities_used": {f"psa{g}": round(p, 4) for g, p in
                                     sorted(probabilities.items(), reverse=True)},
        "assumptions": [
            f"Selling fees of {SELLING_FEE_RATE:.2%} applied to both grading and raw sale",
            f"Shipping of ${OUTBOUND_SHIPPING:.0f} out and ${RETURN_SHIPPING:.2f} back, "
            f"split across {card_count} card(s)",
            *estimates,
        ],
        "fees_as_of": FEES_AS_OF,
        "fees_source": FEES_SOURCE,
    }

    # A long-shot 10 can be worth far more than the expected value the tier was
    # chosen from, which is a real exposure the user should decide on knowingly.
    if psa10_price > 2 * declared_value:
        result["submission"]["insurance_tradeoff"] = (
            f"The tier was chosen from an expected value of ${declared_value:,.0f}, which "
            f"keeps the fee down, but a PSA 10 of this card sells near ${psa10_price:,.0f}. "
            f"If it gems, the return shipment is insured well below what it is worth. "
            f"Declaring higher moves it to a pricier tier; that is a judgement call, not "
            f"a calculation."
        )

    if tier.get("cheaper_tiers_suspended"):
        result["submission"]["note"] = (
            f"PSA's cheaper {' and '.join(tier['cheaper_tiers_suspended'])} tier(s) would "
            f"cover this card but are suspended as of {FEES_AS_OF}, so the floor is "
            f"{tier['service_level']} at ${tier['price_per_card']:.2f}."
        )

    # Shipping is per submission, so the same card can flip verdict in a stack.
    if card_count == 1:
        bundled = (OUTBOUND_SHIPPING + RETURN_SHIPPING) / 10
        bundled_cost = tier["price_per_card"] + bundled + SUPPLIES_PER_CARD
        bundled_edge = expected_net - bundled_cost - net_if_raw
        if bundled_edge > 0 >= edge:
            result["bundling_advice"] = (
                f"Shipping is charged per submission, not per card. Sent on its own this "
                f"card loses about ${abs(edge):,.0f}, but inside a 10-card submission the "
                f"shipping share drops from ${shipping_per_card:,.2f} to ${bundled:,.2f} "
                f"and it turns a roughly ${bundled_edge:,.0f} profit. Wait until you have "
                f"a stack."
            )
        else:
            result["bundling_advice"] = (
                f"Sent alone, shipping adds ${shipping_per_card:,.2f} to this card. In a "
                f"10-card submission that falls to ${bundled:,.2f} per card."
            )

    if card_count >= CLUB_BULK_MINIMUM:
        result["club_advice"] = (
            f"At {card_count} cards, PSA's ${CLUB_MEMBERSHIP:.0f}/year Collectors Club "
            f"unlocks bulk grading near ${CLUB_BULK_PRICE:.0f}/card with a "
            f"{CLUB_BULK_MINIMUM}-card minimum, which would undercut "
            f"{tier['service_level']} at ${tier['price_per_card']:.2f}."
        )

    return result
