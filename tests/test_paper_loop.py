from types import SimpleNamespace

from app.services.paper_loop import paper_loop_decision


def run(id: str = "r1", status: str = "completed", dry_run: bool = False):
    return SimpleNamespace(id=id, status=status, dry_run=dry_run)


def test_paper_loop_waits_for_first_collection() -> None:
    decision = paper_loop_decision(None, None)

    assert decision.process is False
    assert decision.mark_seen is False
    assert decision.reason == "no_collection_run"


def test_paper_loop_skips_already_seen_collection() -> None:
    decision = paper_loop_decision(run("r1"), "r1")

    assert decision.process is False
    assert decision.reason == "already_seen"


def test_paper_loop_compares_collection_ids_as_strings() -> None:
    decision = paper_loop_decision(run(123), "123")

    assert decision.process is False
    assert decision.reason == "already_seen"


def test_paper_loop_processes_new_completed_collection() -> None:
    decision = paper_loop_decision(run("r2"), "r1")

    assert decision.process is True
    assert decision.mark_seen is True
    assert decision.reason == "ready"


def test_paper_loop_does_not_mark_running_collection_seen() -> None:
    decision = paper_loop_decision(run("r2", status="running"), "r1")

    assert decision.process is False
    assert decision.mark_seen is False
    assert decision.reason == "collection_running"


def test_paper_loop_marks_failed_collection_seen_without_processing() -> None:
    decision = paper_loop_decision(run("r2", status="completed_with_errors"), "r1")

    assert decision.process is False
    assert decision.mark_seen is True
    assert decision.reason == "collection_status_completed_with_errors"
