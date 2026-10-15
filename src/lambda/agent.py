"""Auto-fix agent steps, run in order by the step functions workflow:
propose -> approve fix -> push -> deploy staging + check -> approve prod -> deploy prod + check -> finish.
Approvals wait for you on the web page.
Each handler is small and does one job, so each step can be retried and controlled on its own."""
import difflib
import json
import os
import re
import time

import boto3

from shared import (call_llm, commit_file, fetch_failed_log, mask, read_file,
                    repo_files, run_status, start_run)

table = boto3.resource("dynamodb").Table(os.environ["FIX_TABLE"])
MAX_FILE_CHARS = 15000
PLACEHOLDER = re.compile(r"\b(?:ACCOUNT|AWS_KEY|SECRET|BEARER|EMAIL|IP)_\d+\b")


def save(fix_id, **fields):
    # update the fix record and keep a short history of every step (audit trail)
    fields["updated"] = int(time.time())
    names = {f"#{k}": k for k in fields}
    values = {f":{k}": v for k, v in fields.items()}
    expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
    expr += ", #h = list_append(if_not_exists(#h, :empty), :h)"
    names["#h"] = "history"
    values[":empty"] = []
    values[":h"] = [{"t": fields["updated"], "status": fields.get("status", "")}]
    table.update_item(Key={"id": fix_id}, UpdateExpression=expr,
                      ExpressionAttributeNames=names, ExpressionAttributeValues=values)


# ---------- 1. read the failed run + the repo, propose a one-file fix ----------
PICK_PROMPT = """A CI/CD pipeline failed. From the repository files below, pick the ONE file that most likely
needs changing to fix it. Reply only with JSON: {{"file": "path/from/the/list"}}

Failed step(s): {steps}
Log (masked):
{log}

Repository files:
{files}"""

FIX_PROMPT = """You are a senior Cloud/DevOps engineer fixing a failed pipeline.
Placeholders like ACCOUNT_1 or SECRET_1 in the log are masked values, never copy them into the file.
Change only what is needed to fix the failure, keep the file's existing style, comments and structure.
Return the change as exact find/replace edits on {path}: each "find" must be copied exactly
from the current content (enough lines to be unique), "replace" is the new text for it.
Reply only with JSON:
{{
  "rootCause": "one or two sentences",
  "summary": "what you changed, one sentence",
  "confidence": "high | medium | low",
  "edits": [{{"find": "exact current text", "replace": "new text"}}]
}}

Failed step(s): {steps}
Log (masked):
{log}

Current content of {path}:
{content}"""


def propose(event, context):
    fix_id, run_id, attempt = event["fixId"], int(event["runId"]), int(event.get("attempt", 1))
    save(fix_id, status="diagnosing", runId=run_id, attempt=attempt)

    log, run = fetch_failed_log(run_id)
    masked_log, _ = mask(log[-12000:])
    files = repo_files(run["repo"], run["commit"])

    path = call_llm(PICK_PROMPT.format(steps=", ".join(run["failedSteps"]), log=masked_log,
                                       files="\n".join(files)), max_tokens=200).get("file", "")
    if path not in files:
        return no_fix(fix_id, run, f"Could not pick a file to change ({path or 'none'})")

    # repo files are public-safe, so they are sent as they are (not masked)
    content, _ = read_file(run["repo"], path, run["commit"])
    if len(content) > MAX_FILE_CHARS:
        return no_fix(fix_id, run, f"{path} is too large to rewrite safely")

    fix = call_llm(FIX_PROMPT.format(steps=", ".join(run["failedSteps"]), log=masked_log,
                                     path=path, content=content), max_tokens=2000)
    new, problem = apply_edits(content, fix.get("edits"))
    if problem:
        return no_fix(fix_id, run, problem)

    diff = "".join(difflib.unified_diff(content.splitlines(True), new.splitlines(True),
                                        fromfile=f"a/{path}", tofile=f"b/{path}"))
    save(fix_id, status="proposed", pipeline=run["pipeline"], pipelineId=run["pipelineId"],
         repo=run["repo"], branch=run["branch"], failedSteps=run["failedSteps"], file=path,
         rootCause=fix.get("rootCause", ""), summary=fix.get("summary", ""),
         confidence=fix.get("confidence", ""), diff=diff, newContent=new)
    return {**event, "hasFix": True, "attempt": attempt}


def apply_edits(content, edits):
    # apply the model's find/replace edits; every find must match exactly once, otherwise refuse
    if not isinstance(edits, list) or not edits:
        return None, "The model did not propose a change"
    new = content
    for e in edits:
        find, replace = (e or {}).get("find", ""), (e or {}).get("replace", "")
        if not find or new.count(find) != 1:
            return None, "Proposed edit did not match the file exactly once, refused for safety"
        if PLACEHOLDER.search(replace):
            return None, "Proposed change contained masked values, refused for safety"
        new = new.replace(find, replace, 1)
    if new == content:
        return None, "The model did not propose a change"
    return new, None


def no_fix(fix_id, run, reason):
    save(fix_id, status="no_fix", reason=reason, pipeline=run["pipeline"])
    return {"hasFix": False, "reason": reason}


# ---------- 2. ask for approval (the workflow waits here until you approve or reject on the web page) ----------
def request_approval(event, context):
    # kind: "fix" = push the proposed change, "promote" = deploy the checked change to prod
    save(event["fixId"], status="waiting_approval", approvalType=event.get("kind", "fix"),
         taskToken=event["taskToken"])
    return {}


# ---------- 3. push the approved change ----------
def push(event, context):
    item = table.get_item(Key={"id": event["fixId"]})["Item"]
    message = (f"Fix {item['pipeline']} run {item['runId']}: {item['summary']}\n\n"
               f"Root cause: {item['rootCause']}\n\n"
               f"Co-authored-by: {os.environ['COAUTHOR']}")
    sha = commit_file(item["repo"], item["file"], item["newContent"], message, item["branch"])
    save(event["fixId"], status="pushed", commit=sha)
    return {**event, "commit": sha, "pipelineId": int(item["pipelineId"]), "branch": item["branch"]}


# ---------- 4. deploy (staging first, prod after approval) and watch it ----------
def start(event, context):
    env = event.get("deployTo", "staging")
    new_run = start_run(event["pipelineId"], event["branch"], None if env == "staging" else env)
    save(event["fixId"], status=f"running_{env}", newRunId=new_run, **{f"{env}RunId": new_run})
    return {**event, "newRunId": new_run}


def check(event, context):
    env = event.get("deployTo", "staging")
    status, result = run_status(event["newRunId"])
    done = status == "completed"
    if done:
        save(event["fixId"], status=f"green_{env}" if result == "succeeded" else "failed_again", result=result)
    return {**event, "done": done, "result": result}


def finish(event, context):
    item = table.get_item(Key={"id": event["fixId"]})["Item"]
    outcome = event.get("outcome", "")
    text = {
        "green": f"Fix deployed to staging and prod, both green (prod run {item.get('prodRunId')}).",
        "rejected": "You rejected the proposed fix. Nothing was pushed.",
        "green_branch": f"Fix is green on branch {item.get('branch')} (run {item.get('stagingRunId')}). Branches never deploy to prod; merge to main to promote.",
        "prod_rejected": f"Fix is live and green on staging (run {item.get('stagingRunId')}). You chose not to deploy it to prod.",
        "gave_up": f"Pipeline {item.get('pipeline')} still fails after {item.get('attempt')} attempts. Stopped.",
        "expired": "No approval within 24 hours, stopped.",
        "error": f"The agent hit an error: {str(event.get('detail', ''))[:500]}",
    }.get(outcome, outcome)
    save(event["fixId"], status=outcome, reason=text)
    return {"outcome": outcome}
