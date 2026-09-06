"""The voice prompt.

Prompts cannot be unit tested for quality, but they can be pinned against
the specific behaviours that were decided deliberately -- so that a later
edit that quietly drops one is visible in a diff.
"""

from __future__ import annotations

from orchestrator.voice.prompt import INSTRUCTIONS, build_instructions

#: The prompt is hard-wrapped, so a phrase can straddle a line break. Tests
#: assert against this rather than tripping over where the wrap happened.
FLAT = " ".join(INSTRUCTIONS.split())


def test_it_says_output_is_heard_not_read() -> None:
    assert "heard" in INSTRUCTIONS


def test_it_forbids_reading_out_logs_and_identifiers() -> None:
    lowered = INSTRUCTIONS.lower()
    for thing in ("log", "stack trace", "commit hash", "file path"):
        assert thing.split()[0] in lowered


def test_it_tells_the_model_never_to_wait_on_a_job() -> None:
    """The single most important behaviour: a voice agent that blocks on a
    four-hour job is useless."""
    assert "Never wait for a job" in INSTRUCTIONS


def test_it_starts_jobs_without_asking() -> None:
    """The user chose this explicitly: confirm grants, not jobs."""
    assert "Start jobs without asking" in INSTRUCTIONS


def test_it_must_report_every_credential_it_shares() -> None:
    """The confirmation step is gone at the owner's request, so this is the
    control that replaces it."""
    assert "say plainly what you did" in INSTRUCTIONS
    assert "Every time, unprompted" in INSTRUCTIONS


def test_it_shares_only_what_was_asked_for() -> None:
    assert "Never speculatively" in INSTRUCTIONS
    assert "not sure which credential" in INSTRUCTIONS


def test_it_explains_what_redacted_means() -> None:
    """Otherwise the model narrates around it or speculates, which is worse
    than the redaction itself."""
    assert "redacted" in INSTRUCTIONS
    assert "Never speculate" in INSTRUCTIONS


def test_it_handles_ambiguous_repositories_by_asking() -> None:
    assert "Do not pick." in INSTRUCTIONS


def test_it_prioritises_a_job_waiting_for_input() -> None:
    assert "waiting for input" in INSTRUCTIONS


def test_it_expects_to_be_interrupted() -> None:
    assert "stop immediately and listen" in INSTRUCTIONS


def test_it_forbids_inventing_job_names() -> None:
    assert "Never invent one" in INSTRUCTIONS


def test_it_stays_short_enough_to_be_worth_sending_every_session() -> None:
    """Sent once when a call is accepted, not on every turn, so about a
    thousand tokens is cheap. The cap is against sprawl, not cost: a prompt
    nobody can hold in their head is one nobody edits carefully."""
    assert len(INSTRUCTIONS) < 6000, "the prompt is sprawling"


def test_it_tells_the_model_it_can_stop_a_job() -> None:
    assert "You can stop a job" in INSTRUCTIONS


def test_it_says_stopping_is_not_undoing() -> None:
    """Otherwise "stop it" reads as "undo it", and the work is still on the
    branch."""
    assert "Stopping is not undoing" in INSTRUCTIONS


def test_extra_context_is_appended_not_merged() -> None:
    """Site notes must not be able to override the base rules."""
    out = build_instructions("The kiln repo is the important one.")
    assert out.startswith(INSTRUCTIONS)
    assert "kiln repo" in out


def test_no_extra_context_leaves_the_prompt_alone() -> None:
    assert build_instructions(None) == INSTRUCTIONS
    assert build_instructions("   ") == INSTRUCTIONS


def test_it_knows_how_to_choose_an_agent() -> None:
    """ask() requires one and the user will usually not name one, so
    without this "do XYZ" is underspecified and the model guesses."""
    assert "Choosing who does the work" in FLAT
    assert "Pick, do not ask" in FLAT


def test_it_says_which_agent_it_picked() -> None:
    """A silent wrong choice is discovered ten minutes later."""
    assert "say which you picked" in FLAT


def test_it_has_a_tiebreak() -> None:
    assert "toss-up" in FLAT


def test_it_refuses_to_guess_at_an_unregistered_repository() -> None:
    """Work done in the wrong repository is worse than work not started."""
    assert "not registered" in FLAT
    assert "Do not start the job somewhere else" in FLAT
