"""One failing step must not skip the nine after it.

2026-10-13..16: the daily health snapshot failed on every scheduled run (an
upstream Smartlead read returning HTTP 200 with an empty body). Because that
step had no `continue-on-error`, GitHub Actions skipped every step after it:
the inboxes view, the invariants refresh, acquisition capacity, the client
detail cache, the reply scan, the onboarding view, the warm-up check and the
slow caches. Steps carrying their own `continue-on-error: true` were skipped
too — that flag covers a step's own failure, not being skipped after an
earlier one.

Cost: four days with the burned-inbox list frozen (reading as "nothing burned",
which was taken for a quiet week — it was 15 burned, all on live clients), the
rule results stale, and the warm-up panel empty.

The shape of the fix: a step that must be loud is `continue-on-error: true`
and the job is failed at the END by a gate that inspects its outcome. Loud,
but not blocking.

`yaml` is not available in the test environment, so this parses the step
blocks directly. It is deliberately strict about indentation, which the
workflow uses consistently.
"""
import re

WORKFLOW = ".github/workflows/zapmail-watch.yml"

# Steps that legitimately block, with the reason. Everything else after the
# snapshot must be non-blocking.
ALLOWED_BLOCKING = {
    # First step; nothing to skip before it.
    "Zapmail removal check (deletion-date alerts + removal confirmation)",
    # The snapshot reads the overview_v2 cache that THIS writes. Running the
    # snapshot on a stale overview fails instantly with "no inboxes in
    # overview", so this one genuinely gates what follows.
    "Rebuild the Overview (sync)",
}


def steps():
    """[(name, body)] in file order."""
    text = open(WORKFLOW, encoding="utf-8").read()
    parts = re.split(r"\n      - name: ", text)
    out = []
    for chunk in parts[1:]:
        name, _, body = chunk.partition("\n")
        out.append((name.strip(), body))
    return out


def test_the_workflow_still_parses_into_steps():
    names = [n for n, _ in steps()]
    assert len(names) >= 10, names
    assert "Rebuild the Overview (sync)" in names


def test_no_step_can_silently_skip_the_rest_of_the_job():
    blocking = [n for n, body in steps()
                if "continue-on-error: true" not in body
                and not n.startswith("Fail the job")]
    unexpected = [n for n in blocking if n not in ALLOWED_BLOCKING]
    assert not unexpected, (
        "these steps have no `continue-on-error: true`, so if one fails every "
        "step after it is SKIPPED — the exact 2026-10-13 outage. Either add the "
        "flag and gate the step at the end of the job, or add it to "
        f"ALLOWED_BLOCKING with the reason it must gate: {unexpected}")


def test_the_loud_steps_are_gated_so_a_failure_is_still_red():
    """Non-blocking must not become unnoticed. Every step that opts out of
    blocking by id must be named in the final gate."""
    text = open(WORKFLOW, encoding="utf-8").read()
    ids = re.findall(r"\n        id: (\w+)", text)
    assert ids, "no step ids — nothing can be gated"
    gate = text[text.index("- name: Fail the job"):]
    for sid in ids:
        assert f"steps.{sid}.outcome" in gate, (
            f"step id `{sid}` is continue-on-error but the final gate never "
            f"checks it, so its failure would be an invisible green run")


def test_the_gate_runs_even_when_an_earlier_step_failed():
    """Without always() the gate is itself skipped by a prior failure, and a
    broken run reports success."""
    text = open(WORKFLOW, encoding="utf-8").read()
    gate = text[text.index("- name: Fail the job"):]
    assert "always()" in gate
    assert "exit 1" in gate


def test_the_gate_is_the_last_step():
    """A gate in the middle would skip whatever follows it — reintroducing the
    bug it exists to prevent."""
    assert steps()[-1][0].startswith("Fail the job")
