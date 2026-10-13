import agent
import autofix_api

RUN = {"id": 1, "pipeline": "p", "pipelineId": 9, "failedSteps": ["Deploy"], "repo": "o/r",
       "commit": "abc", "branch": "main", "result": "failed"}


def setup_agent(monkeypatch, new_content, current="a: 1\n"):
    saved = {}
    monkeypatch.setattr(agent, "save", lambda fix_id, **f: saved.update(f))
    monkeypatch.setattr(agent, "fetch_failed_log", lambda run_id: ("Error: bad value", RUN))
    monkeypatch.setattr(agent, "repo_files", lambda repo, ref: ["cicd/a.yaml"])
    monkeypatch.setattr(agent, "read_file", lambda repo, path, ref: (current, "sha"))
    replies = iter([{"file": "cicd/a.yaml"},
                    {"rootCause": "r", "summary": "s", "confidence": "high", "newContent": new_content}])
    monkeypatch.setattr(agent, "call_llm", lambda prompt, max_tokens=800: next(replies))
    return saved


def test_propose_builds_a_diff(monkeypatch):
    saved = setup_agent(monkeypatch, "a: 2\n")
    out = agent.propose({"fixId": "f1", "runId": 1}, None)
    assert out["hasFix"] is True
    assert saved["status"] == "proposed"
    assert "-a: 1" in saved["diff"] and "+a: 2" in saved["diff"]


def test_propose_refuses_unchanged_file(monkeypatch):
    saved = setup_agent(monkeypatch, "a: 1\n")
    assert agent.propose({"fixId": "f1", "runId": 1}, None)["hasFix"] is False
    assert saved["status"] == "no_fix"


def test_propose_refuses_masked_values_in_the_change(monkeypatch):
    saved = setup_agent(monkeypatch, "account: ACCOUNT_1\n")
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
