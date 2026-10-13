import json

import importlib

# "lambda" is a python keyword, so the module is imported by name
api = importlib.import_module("lambda")


def call(body):
    res = api.handler({"body": body}, None)
    return res["statusCode"], json.loads(res["body"])


def test_rejects_bad_input():
    assert call("not json")[0] == 400
    assert call("{}")[0] == 400
    assert call(json.dumps({"runId": "abc"}))[0] == 400


def test_diagnoses_pasted_log_with_masking(monkeypatch):
    sent = {}

    def fake_llm(prompt, max_tokens=800):
        sent["prompt"] = prompt
        return {"rootCause": "x", "failedStep": "y", "fix": ["z"], "codeSnippet": "", "confidence": "high"}

    monkeypatch.setattr(api, "call_llm", fake_llm)
    status, body = call(json.dumps({"log": "Error for account 123456789012", "source": "terraform"}))
    assert status == 200
    assert "123456789012" not in sent["prompt"]     # never sent unmasked
    assert body["masked"] == {"ACCOUNT": 1}
    assert body["confidence"] == "high"
