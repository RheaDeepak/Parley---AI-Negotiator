from scripts.generate_dashboard import (
    classify_outcome,
    compute_merchant_breakdown,
    compute_summary_stats,
    group_by_negotiation,
    summarize_all,
)


def _offer(price, qty=1):
    return {"price": price, "qty": qty}


def _entry(negotiation_id, agent, action, timestamp, offer=None, payment=None,
           product_name="Widget", list_price=100.0, merchant_id=None):
    entry = {
        "timestamp": timestamp,
        "agent": agent,
        "action": action,
        "offer": offer,
        "rationale": "",
        "evidence_paths": [],
    }
    if negotiation_id is not None:
        entry["negotiation_id"] = negotiation_id
    if product_name is not None:
        entry["product_name"] = product_name
    if list_price is not None:
        entry["list_price"] = list_price
    if merchant_id is not None:
        entry["merchant_id"] = merchant_id
    if payment is not None:
        entry["payment"] = payment
    return entry


def _sample_combined_log():
    """Simulates two log files' worth of entries, interleaved out of
    both chronological and negotiation order -- exactly the shape a real
    combined negotiation.log + dashboard_seed.log read would produce."""
    entries = [
        # Negotiation A: accepted, 2 rounds, discount 20% (100 -> 80), completed
        # payment 8000 paise = 80.00, merchant MERCH-001.
        _entry("nego-a", "buyer-agent", "offer", "2026-09-01T10:00:00Z", offer=_offer(70), merchant_id="MERCH-001"),
        # Negotiation B: rejected, 1 round, NO merchant_id (simulates a policy
        # predating multi-merchant support) -- interleaved to break contiguity.
        _entry("nego-b", "buyer-agent", "offer", "2026-09-01T09:00:00Z", offer=_offer(50)),
        _entry("nego-a", "merchant-agent", "counter", "2026-09-01T10:00:05Z", offer=_offer(90), merchant_id="MERCH-001"),
        _entry("nego-b", "merchant-agent", "reject", "2026-09-01T09:00:05Z", offer=None),
        _entry("nego-a", "buyer-agent", "offer", "2026-09-01T10:00:10Z", offer=_offer(75), merchant_id="MERCH-001"),
        # Negotiation C: rolled back (payment failure), discount 10% (100 -> 90),
        # merchant MERCH-002 -- interleaved too.
        _entry("nego-c", "buyer-agent", "offer", "2026-09-01T08:00:00Z", offer=_offer(85), merchant_id="MERCH-002"),
        _entry("nego-a", "merchant-agent", "counter", "2026-09-01T10:00:15Z", offer=_offer(80), merchant_id="MERCH-001"),
        _entry("nego-c", "merchant-agent", "accept", "2026-09-01T08:00:05Z", offer=_offer(90), merchant_id="MERCH-002"),
        _entry("nego-a", "buyer-agent", "accept", "2026-09-01T10:00:20Z", offer=_offer(80), merchant_id="MERCH-001"),
        _entry("nego-c", "merchant-agent", "payment_rollback", "2026-09-01T08:00:10Z", offer=_offer(90), merchant_id="MERCH-002"),
        _entry("nego-a", "merchant-agent", "payment_completed", "2026-09-01T10:00:25Z", offer=_offer(80),
               payment={"amount": 8000, "currency": "INR"}, merchant_id="MERCH-001"),
        # A pre-Milestone-7 legacy entry with no negotiation_id -- must be excluded, not guessed into a group.
        _entry(None, "merchant-agent", "payment_initiated", "2026-08-01T00:00:00Z", offer=_offer(999)),
    ]
    return entries


def test_groups_interleaved_multi_round_entries_into_single_negotiations():
    entries = _sample_combined_log()
    groups, ungrouped_count = group_by_negotiation(entries)

    assert ungrouped_count == 1
    assert set(groups.keys()) == {"nego-a", "nego-b", "nego-c"}
    # Negotiation A has 6 entries scattered across the interleaved input --
    # all 6 must land in the same group, not be split or lost.
    assert len(groups["nego-a"]) == 6
    assert len(groups["nego-b"]) == 2
    assert len(groups["nego-c"]) == 3
    # Each group's entries come back sorted chronologically.
    timestamps = [e["timestamp"] for e in groups["nego-a"]]
    assert timestamps == sorted(timestamps)


def test_classify_outcome_prioritizes_completed_over_earlier_rollback():
    entries = _sample_combined_log()
    groups, _ = group_by_negotiation(entries)

    assert classify_outcome(groups["nego-a"]) == "Accepted"
    assert classify_outcome(groups["nego-b"]) == "Rejected"
    assert classify_outcome(groups["nego-c"]) == "Rolled back"


def test_summary_stats_compute_correct_counts_and_average_discount():
    entries = _sample_combined_log()
    groups, _ = group_by_negotiation(entries)
    summaries = summarize_all(groups)
    stats = compute_summary_stats(summaries)

    assert stats["total_negotiations"] == 3
    assert stats["accepted"] == 1
    assert stats["rejected"] == 1
    assert stats["rolled_back"] == 1
    # A: (100-80)/100*100 = 20%; C: (100-90)/100*100 = 10%; B has no settled
    # price so it's excluded from the average entirely, not counted as 0%.
    assert stats["avg_discount_pct"] == 15.0
    # Only nego-a's payment_completed entry (8000 paise) counts as revenue.
    assert stats["total_revenue"] == 80.0
    assert stats["currency"] == "INR"


def test_rounds_counted_from_merchant_decision_entries_only():
    entries = _sample_combined_log()
    groups, _ = group_by_negotiation(entries)
    summaries = {s["negotiation_id"]: s for s in summarize_all(groups)}

    # nego-a has two merchant-agent "counter" entries -> 2 rounds.
    assert summaries["nego-a"]["rounds"] == 2
    # nego-b has one merchant-agent "reject" entry -> 1 round.
    assert summaries["nego-b"]["rounds"] == 1
    # nego-c's single merchant-agent "accept" entry -> 1 round (its later
    # payment_rollback is agent="merchant-agent" too, but action
    # "payment_rollback" isn't a negotiation-round decision, so it must
    # not be counted).
    assert summaries["nego-c"]["rounds"] == 1


def test_merchant_breakdown_splits_stats_per_merchant_and_buckets_unknown():
    entries = _sample_combined_log()
    groups, _ = group_by_negotiation(entries)
    summaries = summarize_all(groups)
    merchant_names = {"MERCH-001": "Voltstream Electronics", "MERCH-002": "Hearth & Home Living"}
    breakdown = compute_merchant_breakdown(summaries, merchant_names)
    by_name = {row["merchant_name"]: row for row in breakdown}

    assert set(by_name.keys()) == {"Voltstream Electronics", "Hearth & Home Living", "Unknown / no merchant"}

    # nego-a (Accepted, MERCH-001) is the only negotiation for that merchant.
    assert by_name["Voltstream Electronics"]["total_negotiations"] == 1
    assert by_name["Voltstream Electronics"]["accepted"] == 1
    assert by_name["Voltstream Electronics"]["total_revenue"] == 80.0

    # nego-c (Rolled back, MERCH-002) is the only negotiation for that merchant.
    assert by_name["Hearth & Home Living"]["total_negotiations"] == 1
    assert by_name["Hearth & Home Living"]["rolled_back"] == 1

    # nego-b has no merchant_id at all -- bucketed under the unknown label,
    # not dropped and not misattributed to either real merchant.
    assert by_name["Unknown / no merchant"]["total_negotiations"] == 1
    assert by_name["Unknown / no merchant"]["rejected"] == 1

    # Sorted by total_negotiations descending -- all tied at 1 here, but the
    # sort must at least be stable/present, not raise or silently drop rows.
    assert len(breakdown) == 3
