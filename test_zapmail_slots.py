from zapmail_slots import free_slots

LIVE_2026_11_07 = {  # /v2/workspaces currentWorkspace, trimmed
    "assignedMailboxesCountGoogle": "1420", "assignedMailboxesCountMicrosoft": "12",
    "totalMailboxesPurchasedGoogle": "1395", "totalMailboxesPurchasedMicrosoft": "0",
    "assignedMailboxesCount": "1432", "totalMailboxesPurchased": "1443",
}


def test_uses_workspace_totals_not_google_counters():
    assert free_slots(LIVE_2026_11_07) == 11


def test_one_contact_inbox_needs_no_purchase():
    assert max(0, 1 - free_slots(LIVE_2026_11_07)) == 0


def test_falls_back_to_google_counters():
    assert free_slots({"totalMailboxesPurchasedGoogle": "10", "assignedMailboxesCountGoogle": "7"}) == 3


def test_unreadable_is_none():
    assert free_slots({}) is None
    assert free_slots(None) is None
    assert free_slots({"totalMailboxesPurchased": "x", "assignedMailboxesCount": "1"}) is None
