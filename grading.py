"""The grade model: a condition rubric that returns a probability distribution.

PSA publishes centering tolerances but not a formula, so this module does two
things and keeps them separate:

1. Hard gates. PSA's published centering tolerances are a ceiling. A card whose
   front is worse than 60/40 is not getting a 10 no matter how clean the
   surface is, so centering sets `cap`, the best grade still reachable.

2. Soft penalties. Corners, edges and surface wear each demote the expected
   grade by a number of grade points, tuned against published gem rates by era.

The output is a distribution rather than a single grade because the decision is
driven almost entirely by P(PSA 10): the 9 -> 10 price cliff is routinely 3-5x,
so "probably an 8 or 9" and "probably a 9 or 10" are the same +/-1 window and
opposite answers.

Sources are listed in README.md. Era priors come from population-report gem
rates, which are conditional on someone having chosen to submit the card, so
they skew optimistic for a card pulled at random. That bias is documented to
the model in every result.
"""

import math

RUBRIC_VERSION = "1.1.0"

# PSA's published centering tolerance, front. The ratio is the larger border
# share, so 55 means 55/45. Each entry is the best grade still reachable.
FRONT_CENTERING_CAPS = (
    (55, 10, 0.0),
    (60, 10, 0.30),  # PSA states 10 as "55/45 to 60/40"; 60/40 is borderline.
    (65, 9, 0.0),
    (70, 8, 0.0),
    (80, 7, 0.0),
)

# Back tolerance is looser and flattens out at 90/10 from grade 9 down.
BACK_CENTERING_CAPS = (
    (75, 10, 0.0),
    (90, 9, 0.0),
)

# Grade points of demotion per defect. Surface on the front is weighted about
# 1.5x the back, matching how PSA describes front-face eye appeal.
#
# An 'unsure' answer costs very little, because not knowing is not the same as
# the card being flawed. Unknowns instead widen the distribution via SIGMA_PER
# _UNKNOWN below, which is the honest representation: less information means a
# less certain estimate, not a worse card.
PENALTIES = {
    "corners": {
        "all_sharp": 0.0,
        "one_slight_white": 0.45,
        "multiple_slight_white": 0.90,
        "visible_wear": 1.80,
        "soft_or_dinged": 2.80,
        "unsure": 0.20,
    },
    "edges": {
        "clean": 0.0,
        "minor_whitening_one_edge": 0.35,
        "whitening_several_edges": 0.90,
        "chipping": 1.90,
        "unsure": 0.18,
    },
    "surface_front": {
        "flawless": 0.0,
        "one_light_scratch": 0.50,
        "several_scratches": 1.30,
        "print_line_or_dimple": 1.10,
        "scuffed_or_creased": 3.00,
        "unsure": 0.20,
    },
    "surface_back": {
        "flawless": 0.0,
        "one_light_scratch": 0.30,
        "several_scratches": 0.90,
        "print_line_or_dimple": 0.70,
        "scuffed_or_creased": 2.20,
        "unsure": 0.15,
    },
    "handling": {
        "pack_fresh_sleeved": 0.0,
        "sleeved_later": 0.15,
        "loose_or_played": 0.80,
        "unsure": 0.15,
    },
}

# PSA grades holistically: the worst defect largely sets the grade and the rest
# nudge it, so penalties combine sub-additively rather than simply summing. A
# card with four small flaws is not four grades worse than a clean one.
SECONDARY_PENALTY_WEIGHT = 0.45

# (handicap in grade points, sigma) per era. Calibrated so that a card
# described as flawless reproduces the published PSA 10 gem rate for its era:
# about 61% for 2023+ modern and about 14% for pre-2003 vintage.
ERA_PRIORS = {
    "japanese_modern": (0.22, 0.50),
    "modern_2023_plus": (0.365, 0.55),
    "modern_2019_2022": (0.52, 0.58),
    "late_2010s": (0.66, 0.62),
    "mid_2000s": (0.92, 0.68),
    "vintage_pre_2003": (1.20, 0.75),
    "unsure": (0.70, 0.80),
}

# Each unanswered factor widens the spread. Capped so that a user who answers
# nothing still gets a usable distribution rather than a flat line.
SIGMA_PER_UNKNOWN = 0.16
SIGMA_CEILING = 1.60

# How each defect reads back to the user when it is the binding constraint.
DEFECT_LABELS = {
    "corners": {
        "one_slight_white": "one corner with slight whitening",
        "multiple_slight_white": "slight whitening on several corners",
        "visible_wear": "visible corner wear",
        "soft_or_dinged": "a soft or dinged corner",
        "unsure": "corners not described",
    },
    "edges": {
        "minor_whitening_one_edge": "minor whitening on one edge",
        "whitening_several_edges": "whitening along several edges",
        "chipping": "edge chipping",
        "unsure": "edges not described",
    },
    "surface_front": {
        "one_light_scratch": "a light scratch on the front",
        "several_scratches": "several scratches on the front",
        "print_line_or_dimple": "a print line or dimple on the front",
        "scuffed_or_creased": "front scuffing or a crease",
        "unsure": "front surface not described",
    },
    "surface_back": {
        "one_light_scratch": "a light scratch on the back",
        "several_scratches": "several scratches on the back",
        "print_line_or_dimple": "a print line or dimple on the back",
        "scuffed_or_creased": "back scuffing or a crease",
        "unsure": "back surface not described",
    },
    "handling": {
        "sleeved_later": "sleeved some time after the pull",
        "loose_or_played": "stored loose or played with",
        "unsure": "storage history not described",
    },
}

REPORTED_GRADES = (10, 9, 8, 7, 6)


def parse_centering(value: str) -> tuple[float | None, str]:
    """Read a centering ratio into the larger border's share of the total.

    Accepts '55/45', '55-45', '60 40', 'dead centered', 'unsure'. Returns
    (ratio, note); ratio is None when it could not be read, which the caller
    treats as an unknown rather than guessing.
    """
    if value is None:
        return None, "not provided"
    text = str(value).strip().lower()
    if not text or text in {"unsure", "unknown", "dont know", "don't know", "idk", "na", "n/a"}:
        return None, "not measured"
    if any(word in text for word in ("dead cent", "perfectly cent", "50/50", "50-50")):
        return 50.0, "described as dead centered"

    digits: list[float] = []
    current = ""
    for char in text:
        if char.isdigit() or char == ".":
            current += char
        else:
            if current:
                digits.append(float(current))
                current = ""
    if current:
        digits.append(float(current))

    if len(digits) >= 2:
        a, b = digits[0], digits[1]
        total = a + b
        if total <= 0:
            return None, f"could not read '{value}'"
        # Normalise to a percentage split, then take the worse side.
        larger = max(a, b) / total * 100
        return round(larger, 1), f"read as {round(larger)}/{round(100 - larger)}"
    if len(digits) == 1 and 50 <= digits[0] <= 100:
        return digits[0], f"read as {round(digits[0])}/{round(100 - digits[0])}"

    # Qualitative descriptions, mapped conservatively.
    if "slight" in text or "barely" in text:
        return 60.0, "qualitative: treated as about 60/40"
    if "noticeab" in text or "off" in text:
        return 68.0, "qualitative: treated as about 68/32"
    if "bad" in text or "way off" in text or "terrible" in text:
        return 78.0, "qualitative: treated as about 78/22"
    return None, f"could not read '{value}'"


def _cap_from_ratio(ratio: float | None, table) -> tuple[int, float]:
    """Return (best reachable grade, extra penalty) for a centering ratio."""
    if ratio is None:
        return 10, 0.0
    for threshold, cap, extra in table:
        if ratio <= threshold:
            return cap, extra
    return 6, 0.0


def _normal_weight(grade: int, center: float, sigma: float) -> float:
    return math.exp(-((grade - center) ** 2) / (2 * sigma * sigma))


def score_condition(
    centering_front: str,
    centering_back: str,
    corners: str,
    edges: str,
    surface_front: str,
    surface_back: str,
    era: str,
    handling: str,
) -> dict:
    """Turn condition answers into a grade distribution. Pure function, no I/O."""
    answers = {
        "corners": corners,
        "edges": edges,
        "surface_front": surface_front,
        "surface_back": surface_back,
        "handling": handling,
    }

    # Unknown answers are penalised mildly and widen the distribution rather
    # than being silently treated as perfect.
    unsure_fields: list[str] = []
    costs: list[float] = []
    penalty_breakdown: list[dict] = []

    for field, raw_value in answers.items():
        table = PENALTIES[field]
        value = (raw_value or "unsure").strip().lower()
        if value not in table:
            value = "unsure"
        if value == "unsure":
            unsure_fields.append(field)
        cost = table[value]
        if cost > 0:
            costs.append(cost)
            penalty_breakdown.append({
                "factor": field,
                "answer": value,
                "grade_points": round(cost, 2),
                "reads_as": DEFECT_LABELS.get(field, {}).get(value, value),
            })

    front_ratio, front_note = parse_centering(centering_front)
    back_ratio, back_note = parse_centering(centering_back)
    if front_ratio is None:
        unsure_fields.append("centering_front")
        costs.append(0.20)
    if back_ratio is None:
        unsure_fields.append("centering_back")
        costs.append(0.12)

    front_cap, front_extra = _cap_from_ratio(front_ratio, FRONT_CENTERING_CAPS)
    back_cap, back_extra = _cap_from_ratio(back_ratio, BACK_CENTERING_CAPS)
    for extra in (front_extra, back_extra):
        if extra > 0:
            costs.append(extra)

    # Worst defect in full, the rest at a discount.
    if costs:
        costs.sort(reverse=True)
        penalty_total = costs[0] + SECONDARY_PENALTY_WEIGHT * sum(costs[1:])
    else:
        penalty_total = 0.0

    cap = min(front_cap, back_cap)

    era_key = (era or "unsure").strip().lower()
    if era_key not in ERA_PRIORS:
        era_key = "unsure"
    handicap, base_sigma = ERA_PRIORS[era_key]

    center = cap - penalty_total - handicap
    sigma = min(base_sigma + SIGMA_PER_UNKNOWN * len(unsure_fields), SIGMA_CEILING)

    # Discretised normal, truncated at the centering cap.
    weights = {}
    for grade in range(1, 11):
        if grade > cap:
            continue
        weights[grade] = _normal_weight(grade, center, sigma)

    total = sum(weights.values())
    if total <= 0:  # Center fell far below the grade scale.
        weights = {max(1, min(cap, int(round(center)))): 1.0}
        total = 1.0

    # Collapse everything at or below 6 into the reported bottom bucket.
    distribution = {}
    for grade in REPORTED_GRADES:
        if grade == 6:
            mass = sum(w for g, w in weights.items() if g <= 6)
        else:
            mass = weights.get(grade, 0.0)
        distribution[grade] = round(mass / total, 4)

    # Rounding can leave the bucket sum slightly off; push the drift into the mode.
    drift = round(1.0 - sum(distribution.values()), 4)
    if abs(drift) >= 0.0001:
        mode = max(distribution, key=lambda g: distribution[g])
        distribution[mode] = round(distribution[mode] + drift, 4)

    limiting_factor, ceiling_reason = _limiting_factor(
        cap, front_cap, back_cap, front_ratio, back_ratio, penalty_breakdown, penalty_total
    )

    confidence = "high"
    if len(unsure_fields) >= 4:
        confidence = "low"
    elif len(unsure_fields) >= 2:
        confidence = "medium"

    return {
        "grade_distribution": {f"psa{g}" if g > 6 else "psa6_or_lower": p
                               for g, p in distribution.items()},
        "most_likely_grade": max(distribution, key=lambda g: distribution[g]),
        "p_psa10": distribution[10],
        "p_psa9_or_better": round(distribution[10] + distribution[9], 4),
        "best_reachable_grade": cap,
        "ceiling_reason": ceiling_reason,
        "limiting_factor": limiting_factor,
        "centering_read": {"front": front_note, "back": back_note},
        "penalties_applied": penalty_breakdown,
        "total_demotion_grade_points": round(penalty_total, 2),
        "unanswered_factors": sorted(set(unsure_fields)),
        "confidence": confidence,
        "era_assumed": era_key,
        "rubric_version": RUBRIC_VERSION,
        "how_to_read_this": (
            "An estimate from a described card, not a grade. Centering sets a hard "
            "ceiling from PSA's published tolerances; the other answers demote the "
            "expected grade from there. Era priors come from population-report gem "
            "rates, which only count cards someone chose to submit, so they run "
            "optimistic for a randomly pulled card."
        ),
    }


def _limiting_factor(cap, front_cap, back_cap, front_ratio, back_ratio, penalties, penalty_total):
    """Name the one thing most responsible for holding the grade down.

    Two things can hold a grade down: the centering ceiling, and accumulated
    wear. Whichever costs more grade points is the one reported, so a creased
    card is not told its problem is centering.
    """
    ceiling_reason = "no centering ceiling: centering alone leaves a 10 reachable"
    if cap < 10:
        if back_cap <= front_cap and back_ratio is not None:
            ceiling_reason = (
                f"back centering about {round(back_ratio)}/{round(100 - back_ratio)} "
                f"caps this at a {cap} (PSA wants 75/25 or better on the back for a 10)"
            )
        elif front_ratio is not None:
            ceiling_reason = (
                f"front centering about {round(front_ratio)}/{round(100 - front_ratio)} "
                f"caps this at a {cap} (PSA wants 55/45-60/40 on the front for a 10)"
            )

    worst = max(penalties, key=lambda p: p["grade_points"]) if penalties else None
    ceiling_cost = 10 - cap

    # Wear outweighs the ceiling: report the wear instead.
    if worst and penalty_total > ceiling_cost:
        detail = (
            f"{worst['reads_as']} is the biggest single drag "
            f"({worst['grade_points']} grade points)"
        )
        if len(penalties) > 1:
            detail += f", with {len(penalties) - 1} smaller flaw(s) on top"
        if cap < 10:
            detail += f"; centering separately caps this at a {cap}"
        return detail, ceiling_reason

    if cap < 10:
        return ceiling_reason, ceiling_reason
    if worst:
        return (
            f"{worst['reads_as']} is the biggest single drag "
            f"({worst['grade_points']} grade points)"
        ), ceiling_reason
    return "nothing flagged: this reads as a clean card", ceiling_reason
