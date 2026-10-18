"""Auto-fix agent steps, run in order by the step functions workflow:
propose -> approve fix -> push to a fix branch -> deploy dev + check -> merge into staging -> deploy staging + check
-> approve (open a PR only, or open it and deploy prod after you merge it) -> deploy prod + check -> finish.
Approvals wait for you on the web page. The pipeline picks the environment from the branch it runs on.
Each handler is small and does one job, so each step can be retried and controlled on its own."""
import difflib
import json
import os
import re
import time

import boto3

from shared import (call_llm, commit_file, create_branch, create_pr, fetch_failed_log, mask,
                    merge_branch, pr_state, read_file, repo_files, run_status, start_run)

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
         repo=run["repo"], branch=run["branch"], baseCommit=run["commit"],
         failedSteps=run["failedSteps"], file=path,
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


# ---------- 3. push the approved change to a fix branch ----------
def push(event, context):
    item = table.get_item(Key={"id": event["fixId"]})["Item"]
    # [skip ci]: the agent starts the run itself, so the push must not start a second one
    message = (f"Fix {item['pipeline']} run {item['runId']}: {item['summary']} [skip ci]\n\n"
               f"Root cause: {item['rootCause']}\n\n"
               f"Co-authored-by: {os.environ['COAUTHOR']}")
    source = item["branch"]
    if source.startswith("fix/"):
        # a retry keeps working on the same fix branch
        branch = source
    else:
        branch = f"fix/run-{item['runId']}"
        create_branch(item["repo"], branch, item["baseCommit"])
    sha = commit_file(item["repo"], item["file"], item["newContent"], message, branch)
    save(event["fixId"], status="pushed", commit=sha, fixBranch=branch)
    return {**event, "commit": sha, "pipelineId": int(item["pipelineId"]), "branch": branch}


# ---------- 4. deploy dev, then staging, then prod, and watch each run ----------
# the pipeline picks the environment from the branch: fix branch -> dev, staging -> staging, main -> prod
RUN_BRANCH = {"staging": "staging", "prod": "main"}


def start(event, context):
    env = event.get("deployTo", "dev")
    branch = RUN_BRANCH.get(env, event["branch"])
    new_run = start_run(event["pipelineId"], branch)
    save(event["fixId"], status=f"running_{env}", newRunId=new_run, **{f"{env}RunId": new_run})
    return {**event, "newRunId": new_run}


def check(event, context):
    env = event.get("deployTo", "dev")
    status, result = run_status(event["newRunId"])
    done = status == "completed"
    if done:
        save(event["fixId"], status=f"green_{env}" if result == "succeeded" else "failed_again", result=result)
    return {**event, "done": done, "result": result}


# ---------- 5. dev is green: merge the fix branch into staging (no review, the agent may do this) ----------
def merge_staging(event, context):
    item = table.get_item(Key={"id": event["fixId"]})["Item"]
    sha = merge_branch(item["repo"], "staging", event["branch"], f"Merge {event['branch']} into staging [skip ci]")
    save(event["fixId"], status="merged_staging", stagingCommit=sha)
    return event


# ---------- 6. staging is green: open the pull request staging -> main (only you merge into main) ----------
def open_pr(event, context):
    item = table.get_item(Key={"id": event["fixId"]})["Item"]
    body = (f"{item['summary']}\n\nRoot cause: {item['rootCause']}\n\n"
            "The change is green on dev and staging.")
    number, url = create_pr(item["repo"], "staging", "main", f"Promote staging to main: {item['summary']}", body)
    save(event["fixId"], status="pr_open", prNumber=number, prUrl=url)
    return {**event, "prNumber": number, "prDeadline": int(time.time()) + 86400}


def check_pr(event, context):
    item = table.get_item(Key={"id": event["fixId"]})["Item"]
    state = pr_state(item["repo"], int(event["prNumber"]))
    if state == "open" and time.time() > event["prDeadline"]:
        state = "expired"
    return {**event, "prState": state}


def finish(event, context):
    item = table.get_item(Key={"id": event["fixId"]})["Item"]
    outcome = event.get("outcome", "")
    text = {
        "green": f"Fix is green on dev, staging and prod (prod run {item.get('prodRunId')}).",
        "pr_opened": f"Fix is green on dev and staging. PR #{item.get('prNumber')} is open for you to review and merge. Nothing was deployed to prod.",
        "rejected": "You rejected the proposed fix. Nothing was pushed.",
        "prod_rejected": f"Fix is green on staging (run {item.get('stagingRunId')}). You chose not to promote it: no PR was opened and nothing was deployed to prod.",
        "pr_closed": f"PR #{item.get('prNumber')} was closed without merging. Nothing was deployed to prod.",
        "gave_up": f"Pipeline {item.get('pipeline')} still fails after {item.get('attempt')} attempts. Stopped.",
        "expired": "No approval or merge within 24 hours, stopped.",
        "error": f"The agent hit an error: {str(event.get('detail', ''))[:500]}",
    }.get(outcome, outcome)
    save(event["fixId"], status=outcome, reason=text)
    return {"outcome": outcome}
