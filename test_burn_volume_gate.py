"""The burn rule only runs on inboxes that cleared the 105-send / 10-sending-day
gate set 2026-09-10. Below it, 3% bounce is one bounce and 0.5% reply is zero
replies -- 58 of the 60 inboxes called burned on 2026-10-22 were that noise."""
import health_model as hm


def sig(sent, bounce, reply):
    return {"sent_3d": sent, "bounce": bounce, "reply": reply,
            "in_warmup": False, "in_campaign": True, "smtp_ok": True}


def test_one_bounce_in_45_sends_is_not_enough_to_judge():
    assert hm.score_inbox(sig(45, 4.4, 0.0))["status"] == hm.INSUFFICIENT


def test_bad_rates_over_a_full_window_still_burn():
    assert hm.score_inbox(sig(126, 5.4, 0.0))["status"] == hm.BURNED


def test_the_live_config_row_wins_and_legacy_rows_still_work():
    assert hm.min_sends({"min_sends_window": 105, "min_sent_3d": 30}) == 105
    assert hm.min_sends({"min_sends_window": None, "min_sent_3d": 30}) == 30
    assert hm.window_sending_days({"window_sending_days": 10}) == 10
