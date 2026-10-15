from steward.policy import classify_instruction, parse_avoid_globs, violations


def test_avoid_is_policy_hard():
    kind, enforcement = classify_instruction("avoid", "steward/store.py")
    assert (kind, enforcement) == ("policy", "hard")


def test_goal_is_intent_review():
    kind, enforcement = classify_instruction("goal", "Also handle reconnect storms")
    assert (kind, enforcement) == ("intent", "review")


def test_focus_is_execution_preference_by_default():
    kind, enforcement = classify_instruction("focus", "work on the retry loop first")
    assert (kind, enforcement) == ("execution", "preference")


def test_nudge_is_execution_preference_by_default():
    kind, enforcement = classify_instruction("nudge", "double check the timeout")
    assert (kind, enforcement) == ("execution", "preference")


def test_focus_mentioning_api_is_upgraded_to_review():
    kind, enforcement = classify_instruction("focus", "keep the public API stable")
    assert (kind, enforcement) == ("execution", "review")


def test_nudge_mentioning_interface_is_upgraded_to_review():
    kind, enforcement = classify_instruction("nudge", "the interface must stay compatible")
    assert (kind, enforcement) == ("execution", "review")


def test_priority_is_policy_preference():
    kind, enforcement = classify_instruction("priority", "quality")
    assert (kind, enforcement) == ("policy", "preference")


def test_unknown_verb_raises():
    try:
        classify_instruction("bogus", "text")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_parse_avoid_globs_splits_on_comma_and_whitespace():
    assert parse_avoid_globs("steward/store.py, tests/*") == ["steward/store.py", "tests/*"]
    assert parse_avoid_globs("db/**") == ["db/**"]


def test_parse_avoid_globs_drops_prose_and_strips_trailing_punctuation():
    # Regression (chair_note, phase 06-01): a sentence-shaped avoid instruction
    # used to return every whitespace-split word, including "Don't"/"touch",
    # which meant `violations()` never matched a real diff path.
    assert parse_avoid_globs("Don't touch migrations/**.") == ["migrations/**"]


def test_parse_avoid_globs_excludes_slash_separated_word_lists():
    # "deploy/publish/network" has slashes but no extension/wildcard — it's
    # a list of alternatives, not a filesystem path (SP-10).
    assert parse_avoid_globs("no deploy/publish/network") == []


def test_avoid_with_no_path_token_is_not_hard():
    kind, enforcement = classify_instruction("avoid", "Don't touch the db.")
    assert (kind, enforcement) == ("policy", "preference")


def test_avoid_with_path_token_in_prose_is_still_hard():
    kind, enforcement = classify_instruction("avoid", "Don't touch migrations/**.")
    assert (kind, enforcement) == ("policy", "hard")


def test_authority_verb_is_policy_authority_not_hard():
    kind, enforcement = classify_instruction("authority", "no deploy/publish/network")
    assert (kind, enforcement) == ("policy", "authority")


def test_hard_constraint_seeded_from_prose_goal_actually_blocks_end_to_end(steward_home):
    # Regression (chair_note): a HARD-labelled constraint must be enforceable
    # by violations() against real diff paths, not just labelled hard.
    from steward.steering import Steering
    from steward.store import Store

    store = Store(steward_home)
    job = store.create_job("Don't touch migrations/**.", "/repo")
    steering = Steering(store)
    steering.record(job.job_id, "avoid", "Don't touch migrations/**.", "req-1")

    hits = violations(["migrations/001.sql", "steward/other.py"], parse_avoid_globs("Don't touch migrations/**."))
    assert hits == ["migrations/001.sql"]


def test_violations_matches_glob_against_diff_paths():
    hits = violations(
        ["steward/store.py", "steward/cli.py", "docs/x.md"],
        ["steward/store.py", "docs/*"],
    )
    assert set(hits) == {"steward/store.py", "docs/x.md"}


def test_violations_empty_when_no_hard_globs():
    assert violations(["a.py"], []) == []


AB83_GOAL = (
    "Use gsd-quick to fix the autopilot merge-block issues from the triage at "
    "/home/user/.claude/responses/2026-09-30/merge-block-triage.md (read it first). "
    "(1) autopilot must LAUNCH the fix-up job it creates (steward/autopilot.py _default_followup), "
    "and escalate NEEDS_YOU if launch fails; add a test that fails when the follow-up is only created. "
    "(2) Per-job question files: steward/brief.py tells workers to put STEWARD-QUESTIONS.json under "
    ".steward/questions/<job8>/ (the adapter matches by basename, answers file lands beside it); one commit "
    "moving master's root STEWARD-QUESTIONS.json/STEWARD-ANSWERS.json out of the root (keep them under "
    ".steward/questions/11ed0088/ so history reads right). (3) When a fix-up for job X merges, mark X as "
    "incorporated via the fix-up's merge commit and release X automatically (incorporate.py/autopilot.py); "
    "a manual incorporate of X after the fact also records it. (4) Status pane (steward/dashrender.py "
    "done_line/incorporate_panel): an accepted job with a blocked auto-merge shows \"merge blocked\" and hides "
    "[ incorporate ]. (5) Fix-up instructions: \"merge master into YOUR branch\", the original goal as "
    "background only (do not redo it), and per-file resolution hints. Tests with fakes for each. Follow "
    ".planning/phases/EXECUTOR-RULES.md; never touch the live Steward; never git push. FINISH LINE: all five."
)


def test_replay_ab83bf58_goal_protects_nothing_it_asks_to_edit():
    from steward.contract import _split_sentences, _BOUNDARY_SENTENCE

    globs = []
    for sentence in _split_sentences(AB83_GOAL):
        if _BOUNDARY_SENTENCE.search(sentence):
            globs.extend(parse_avoid_globs(sentence))
    assert globs == []


def test_explicit_prohibitions_protect_paths():
    assert parse_avoid_globs("Do not touch build.gradle.kts") == ["build.gradle.kts"]
    assert parse_avoid_globs("Never change .planning/STATE.md; edit src/a.py") == [".planning/STATE.md"]
    assert parse_avoid_globs("Leave steward/store.py alone.") == ["steward/store.py"]
    assert parse_avoid_globs("steward/brief.py is read-only.") == ["steward/brief.py"]
    assert parse_avoid_globs("Do not edit A.json/B.json") == ["A.json", "B.json"]


def test_mentioned_or_listed_paths_are_not_protected():
    assert parse_avoid_globs("Keep them under .steward/questions/x/ and edit steward/dashrender.py") == []
    assert parse_avoid_globs("Please edit steward/dashrender.py, don't refactor it") == []


def test_avoid_globs_come_only_from_explicit_prohibitions_and_each_path_once():
    # audit rec 10 (ec7afc69): files the goal asks to edit are never protected, and a repeated path is one glob
    assert parse_avoid_globs("Fix the app models in core/src/main/ReadModels.kt and deploy/cd.sh") == []
    assert parse_avoid_globs("Do not edit steward/store.py; update steward/api.py and tests/test_api.py") == ["steward/store.py"]
    assert parse_avoid_globs("avoid steward/store.py, steward/store.py") == ["steward/store.py"]
    assert parse_avoid_globs("keep the deliverables in docs/audits/x/README.md") == []
