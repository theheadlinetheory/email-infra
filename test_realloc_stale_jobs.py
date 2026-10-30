"""Reallocate must not skip an inbox because of a job that no longer applies.

2026-10-30, landscapecarespecialists.info: two burned PC Landscape inboxes were
skipped as "already handled" because of jobs from when they belonged to
Ecological Improvements (July swap, Oct 9 half-done reservation).
"""
import health_replace as hr


def _job(id, email, status, client):
    return {"id": id, "old_email": email, "status": status, "client": client}


CLIENT_OF = {"a@x.info": "PC Landscape Services", "b@x.info": "PC Landscape Services"}


def test_a_job_for_the_inboxs_previous_client_does_not_count():
    jobs = [_job(61, "a@x.info", "swapped", "Ecological Improvements Group")]
    assert hr.handled_emails(jobs, CLIENT_OF) == set()


def test_a_finished_swap_for_the_same_client_still_counts():
    jobs = [_job(206, "a@x.info", "swapped", "PC Landscape Services")]
    assert hr.handled_emails(jobs, CLIENT_OF) == {"a@x.info"}


def test_a_job_that_never_moved_anything_does_not_lock_the_inbox():
    """A failed attempt leaves a flagged/reserved job; the next click resumes it."""
    jobs = [_job(205, "a@x.info", "reserved", "PC Landscape Services"),
            _job(207, "b@x.info", "flagged", "PC Landscape Services")]
    assert hr.handled_emails(jobs, CLIENT_OF) == set()


def test_client_names_match_loosely():
    jobs = [_job(1, "a@x.info", "swapped", "Ecological Improvements Group")]
    assert hr.handled_emails(jobs, {"a@x.info": "Ecological Improvements"}) == {"a@x.info"}


def test_create_jobs_cancels_an_open_job_from_the_previous_client(monkeypatch):
    st = {"jobs": [_job(198, "a@x.info", "reserved", "Ecological Improvements")]}
    saved = {}
    monkeypatch.setattr(hr, "_load", lambda: st)
    monkeypatch.setattr(hr, "_save", lambda s: saved.update(s))
    monkeypatch.setattr(hr.store, "get_health_status_all",
                        lambda: [{"email": "a@x.info", "client": "PC Landscape Services",
                                  "domain": "x.info", "campaigns": ["PC #1"]}])
    out = hr.create_jobs(["a@x.info"])
    assert out["created"] == 1
    old, new = saved["jobs"]
    assert old["status"] == "cancelled" and "superseded" in old["cancel_reason"]
    assert new["client"] == "PC Landscape Services" and new["status"] == "flagged"
