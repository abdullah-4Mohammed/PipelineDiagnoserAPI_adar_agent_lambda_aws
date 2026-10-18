import agent
import autofix_api

RUN = {"id": 1, "pipeline": "p", "pipelineId": 9, "failedSteps": ["Deploy"], "repo": "o/r",
       "commit": "abc", "branch": "main", "result": "failed"}


def setup_agent(monkeypatch, edits, current="a: 1\nb: 2\n"):
    saved = {}
    monkeypatch.setattr(agent, "save", lambda fix_id, **f: saved.update(f))
    monkeypatch.setattr(agent, "fetch_failed_log", lambda run_id: ("Error: bad value", RUN))
    monkeypatch.setattr(agent, "repo_files", lambda repo, ref: ["cicd/a.yaml"])
    monkeypatch.setattr(agent, "read_file", lambda repo, path, ref: (current, "sha"))
    replies = iter([{"file": "cicd/a.yaml"},
                    {"rootCause": "r", "summary": "s", "confidence": "high", "edits": edits}])
    monkeypatch.setattr(agent, "call_llm", lambda prompt, max_tokens=800: next(replies))
    return saved


def test_propose_builds_a_diff_and_keeps_the_rest_of_the_file(monkeypatch):
    saved = setup_agent(monkeypatch, [{"find": "a: 1", "replace": "a: 3"}])
    out = agent.propose({"fixId": "f1", "runId": 1}, None)
    assert out["hasFix"] is True
    assert saved["status"] == "proposed"
    assert saved["newContent"] == "a: 3\nb: 2\n"
    assert "-a: 1" in saved["diff"] and "+a: 3" in saved["diff"]


def test_propose_refuses_unchanged_file(monkeypatch):
    saved = setup_agent(monkeypatch, [{"find": "a: 1", "replace": "a: 1"}])
    assert agent.propose({"fixId": "f1", "runId": 1}, None)["hasFix"] is False
    assert saved["status"] == "no_fix"


def test_propose_refuses_edit_that_does_not_match(monkeypatch):
    saved = setup_agent(monkeypatch, [{"find": "a: 9", "replace": "a: 3"}])
    assert agent.propose({"fixId": "f1", "runId": 1}, None)["hasFix"] is False
    assert "exactly once" in saved["reason"]


def test_propose_refuses_masked_values_in_the_change(monkeypatch):
    saved = setup_agent(monkeypatch, [{"find": "a: 1", "replace": "account: ACCOUNT_1"}])
    assert agent.propose({"fixId": "f1", "runId": 1}, None)["hasFix"] is False
    assert "masked" in saved["reason"]


def test_decision_only_when_waiting_for_approval(monkeypatch):
    class FakeTable:
        def __init__(self, status):
            self.item = {"id": "f1", "status": status, "taskToken": "t"}

        def get_item(self, Key):
            return {"Item": self.item}

        def update_item(self, **kw):
            pass

    sent = []
    monkeypatch.setattr(autofix_api.sfn, "send_task_success", lambda **kw: sent.append(kw))
    monkeypatch.setattr(autofix_api, "table", FakeTable("running"))
    assert autofix_api.decide("f1", "approve") is False and sent == []
    monkeypatch.setattr(autofix_api, "table", FakeTable("waiting_approval"))
    assert autofix_api.decide("f1", "approve") is True and len(sent) == 1


def test_start_runs_the_branch_that_picks_the_environment(monkeypatch):
    calls, saved = [], {}
    monkeypatch.setattr(agent, "start_run", lambda pid, branch: calls.append(branch) or 100)
    monkeypatch.setattr(agent, "save", lambda fix_id, **f: saved.update(f))
    event = {"fixId": "f1", "pipelineId": 9, "branch": "fix/run-1"}
    agent.start({**event, "deployTo": "dev"}, None)
    assert calls[-1] == "fix/run-1" and saved["status"] == "running_dev" and saved["devRunId"] == 100
    agent.start({**event, "deployTo": "staging"}, None)
    assert calls[-1] == "staging" and saved["status"] == "running_staging" and saved["stagingRunId"] == 100
    agent.start({**event, "deployTo": "prod"}, None)
    assert calls[-1] == "main" and saved["status"] == "running_prod" and saved["prodRunId"] == 100


class FakeFixTable:
    def __init__(self, **item):
        self.item = {"id": "f1", **item}

    def get_item(self, Key):
        return {"Item": self.item}


FIX = {"pipeline": "p", "pipelineId": 9, "runId": 7, "repo": "o/r", "branch": "main", "baseCommit": "abc",
       "file": "cicd/a.yaml", "newContent": "a: 3\n", "summary": "s", "rootCause": "r"}


def test_push_creates_a_fix_branch_from_the_failed_commit(monkeypatch):
    created, committed, saved = [], [], {}
    monkeypatch.setenv("COAUTHOR", "Name <n@example.com>")
    monkeypatch.setattr(agent, "table", FakeFixTable(**FIX))
    monkeypatch.setattr(agent, "save", lambda fix_id, **f: saved.update(f))
    monkeypatch.setattr(agent, "create_branch", lambda repo, branch, sha: created.append((branch, sha)))
    monkeypatch.setattr(agent, "commit_file",
                        lambda repo, path, content, message, branch: committed.append((branch, message)) or "sha1")
    out = agent.push({"fixId": "f1"}, None)
    assert created == [("fix/run-7", "abc")]
    assert committed[0][0] == "fix/run-7" and "[skip ci]" in committed[0][1].splitlines()[0]
    assert out["branch"] == "fix/run-7" and saved["fixBranch"] == "fix/run-7"


def test_push_on_a_retry_keeps_the_same_fix_branch(monkeypatch):
    created, committed = [], []
    monkeypatch.setenv("COAUTHOR", "Name <n@example.com>")
    monkeypatch.setattr(agent, "table", FakeFixTable(**{**FIX, "branch": "fix/run-7"}))
    monkeypatch.setattr(agent, "save", lambda fix_id, **f: None)
    monkeypatch.setattr(agent, "create_branch", lambda *a: created.append(a))
    monkeypatch.setattr(agent, "commit_file", lambda repo, path, content, message, branch: committed.append(branch) or "sha2")
    assert agent.push({"fixId": "f1"}, None)["branch"] == "fix/run-7"
    assert created == [] and committed == ["fix/run-7"]


def test_merge_staging_opens_a_pr_and_merges_it_with_skip_ci(monkeypatch):
    opened, merged, saved = [], [], {}
    monkeypatch.setattr(agent, "table", FakeFixTable(**FIX))
    monkeypatch.setattr(agent, "save", lambda fix_id, **f: saved.update(f))
    monkeypatch.setattr(agent, "create_pr",
                        lambda repo, head, base, title, body: opened.append((head, base)) or (8, "https://github.com/o/r/pull/8"))
    monkeypatch.setattr(agent, "merge_pr", lambda repo, number, title, msg: merged.append((number, title)) or "m1")
    event = {"fixId": "f1", "branch": "fix/run-7"}
    assert agent.merge_staging(event, None) == event
    assert opened == [("fix/run-7", "staging")]
    assert merged == [(8, "Merge fix/run-7 into staging [skip ci]")]
    assert saved["status"] == "merged_staging" and saved["stagingPr"] == 8 and saved["stagingCommit"] == "m1"


def test_merge_pr_merges_once_and_returns_the_commit_for_an_already_merged_pr(monkeypatch):
    import shared
    calls = []
    state = {"merged": False}

    def fake_gh(path, payload=None, method=None):
        calls.append((path, method))
        if method == "PUT":
            return {"sha": "m9"}
        return {"merged": state["merged"], "merge_commit_sha": "m5"}

    monkeypatch.setattr(shared, "gh", fake_gh)
    assert shared.merge_pr("o/r", 8, "t", "") == "m9" and calls[-1] == ("repos/o/r/pulls/8/merge", "PUT")
    state["merged"] = True
    calls.clear()
    assert shared.merge_pr("o/r", 8, "t", "") == "m5" and all(m != "PUT" for _, m in calls)


def test_open_pr_goes_from_staging_to_main_and_sets_a_deadline(monkeypatch):
    opened, saved = [], {}
    monkeypatch.setattr(agent, "table", FakeFixTable(**FIX))
    monkeypatch.setattr(agent, "save", lambda fix_id, **f: saved.update(f))
    monkeypatch.setattr(agent, "create_pr",
                        lambda repo, head, base, title, body: opened.append((head, base)) or (12, "https://github.com/o/r/pull/12"))
    out = agent.open_pr({"fixId": "f1"}, None)
    assert opened == [("staging", "main")] and out["prNumber"] == 12 and out["prDeadline"] > 0
    assert saved["status"] == "pr_open" and saved["prUrl"].endswith("/pull/12")


def test_check_pr_reports_merged_closed_open_and_expired(monkeypatch):
    monkeypatch.setattr(agent, "table", FakeFixTable(**FIX))
    state = {"v": "open"}
    monkeypatch.setattr(agent, "pr_state", lambda repo, number: state["v"])
    future, past = agent.time.time() + 1000, agent.time.time() - 1000
    event = {"fixId": "f1", "prNumber": 12, "prDeadline": future}
    assert agent.check_pr(event, None)["prState"] == "open"
    assert agent.check_pr({**event, "prDeadline": past}, None)["prState"] == "expired"
    state["v"] = "merged"
    assert agent.check_pr({**event, "prDeadline": past}, None)["prState"] == "merged"
    state["v"] = "closed"
    assert agent.check_pr(event, None)["prState"] == "closed"


def test_pr_only_decision_is_refused_outside_the_promote_approval(monkeypatch):
    class Table:
        def __init__(self, kind):
            self.item = {"id": "f1", "status": "waiting_approval", "taskToken": "t", "approvalType": kind}

        def get_item(self, Key):
            return {"Item": self.item}

        def update_item(self, **kw):
            pass

    sent = []
    monkeypatch.setattr(autofix_api.sfn, "send_task_success", lambda **kw: sent.append(kw))
    monkeypatch.setattr(autofix_api, "table", Table("fix"))
    assert autofix_api.decide("f1", "pr_only") is False and sent == []
    monkeypatch.setattr(autofix_api, "table", Table("promote"))
    assert autofix_api.decide("f1", "pr_only") is True and len(sent) == 1


def test_approval_records_its_kind(monkeypatch):
    saved = {}
    monkeypatch.setattr(agent, "save", lambda fix_id, **f: saved.update(f))
    agent.request_approval({"fixId": "f1", "kind": "promote", "taskToken": "t"}, None)
    assert saved["status"] == "waiting_approval" and saved["approvalType"] == "promote"


def test_create_pr_returns_the_open_pr_when_one_already_exists(monkeypatch):
    import io
    import urllib.error

    import shared
    calls = []

    def fake_gh(path, payload=None, method=None):
        calls.append(path)
        if payload is not None:
            raise urllib.error.HTTPError("u", 422, "exists", {}, io.BytesIO(b"{}"))
        return [{"number": 5, "html_url": "https://github.com/o/r/pull/5"}]

    monkeypatch.setattr(shared, "gh", fake_gh)
    assert shared.create_pr("o/r", "staging", "main", "t", "b") == (5, "https://github.com/o/r/pull/5")
    assert "head=o:staging" in calls[-1] and "base=main" in calls[-1]
