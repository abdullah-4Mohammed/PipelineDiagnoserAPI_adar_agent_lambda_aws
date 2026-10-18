"""Shared helpers for the diagnoser API and the auto-fix agent steps:
ssm config, log masker, llm call, azure devops (read + run) and github (read + commit)."""
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

import boto3

# groq exposes an openai compatible chat completions endpoint
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = os.environ["MODEL_ID"]
KEY_PARAM = os.environ["API_KEY_PARAM"]
# azure devops: token, organisation and project are read from ssm parameters
ADO_PARAMS = {
    "token": os.environ["ADO_TOKEN_PARAM"],
    "org": os.environ["ADO_ORG_PARAM"],
    "project": os.environ["ADO_PROJECT_PARAM"],
}
# github token (read + write contents), only needed by the agent steps
GITHUB_TOKEN_PARAM = os.environ.get("GITHUB_TOKEN_PARAM", "")
# lines kept from the end of each failed step log
LOG_TAIL_LINES = 150

ssm = boto3.client("ssm")
_cache = {}


def param(name):
    # read once per container, then reuse on warm invocations
    if name not in _cache:
        _cache[name] = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    return _cache[name]


def http(url, headers, data=None, method=None, timeout=20, raw=False):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    return body if raw else (json.loads(body) if body else {})


# ---------- masker ----------
# replaces sensitive values with stable placeholders (same value -> same code),
# so the log keeps its structure but nothing private leaves the account.
# order matters: specific patterns before general ones.
# a named group "v" means only that part is masked (the label stays readable).
MASK_RULES = [
    ("AWS_KEY", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("SECRET", re.compile(r"(?i)(?:secret|password|passwd|token|apikey|api_key)[\"']?\s*[:=]\s*[\"']?(?P<v>[^\s\"',;]{6,})")),
    ("BEARER", re.compile(r"(?i)bearer\s+(?P<v>[A-Za-z0-9\-._~+/]{10,}=*)")),
    ("ACCOUNT", re.compile(r"(?<![\d.])\d{12}(?![\d.])")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]


def mask(text):
    seen = {}
    counters = {}

    def replacer(kind):
        def repl(m):
            has_v = "v" in m.re.groupindex
            value = m.group("v") if has_v else m.group(0)
            if value not in seen:
                counters[kind] = counters.get(kind, 0) + 1
                seen[value] = f"{kind}_{counters[kind]}"
            if not has_v:
                return seen[value]
            start, end = m.span("v")
            whole = m.group(0)
            offset = m.start(0)
            return whole[:start - offset] + seen[value] + whole[end - offset:]
        return repl

    for kind, pattern in MASK_RULES:
        text = pattern.sub(replacer(kind), text)
    return text, counters


# ---------- model call ----------
def call_llm(prompt, max_tokens=800):
    # returns the model reply parsed as json
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }).encode()
    data = http(GROQ_URL, {
        "Authorization": f"Bearer {param(KEY_PARAM)}",
        "Content-Type": "application/json",
        "User-Agent": "pipeline-diagnoser-api",
    }, data=body, timeout=25)
    return json.loads(data["choices"][0]["message"]["content"])


# ---------- azure devops: find the failed run and read its logs (read only) ----------
def ado_request(path, raw=False, payload=None):
    cfg = {k: param(v) for k, v in ADO_PARAMS.items()}
    url = f"https://dev.azure.com/{cfg['org']}/{cfg['project']}/_apis/{path}"
    url += ("&" if "?" in url else "?") + "api-version=7.1"
    auth = base64.b64encode(f":{cfg['token']}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}
    data = json.dumps(payload).encode() if payload is not None else None
    return http(url, headers, data=data, method="POST" if data else "GET", raw=raw)


def ado_get(path, raw=False):
    return ado_request(path, raw=raw)


def latest_failed_run(pipeline_name):
    defs = ado_get("build/definitions?name=" + urllib.parse.quote(pipeline_name))["value"]
    if not defs:
        raise LookupError(f"No pipeline named {pipeline_name}")
    runs = ado_get(f"build/builds?definitions={defs[0]['id']}&resultFilter=failed&$top=1")["value"]
    if not runs:
        raise LookupError(f"No failed runs for {pipeline_name}")
    return runs[0]["id"]


def fetch_failed_log(run_id):
    build = ado_get(f"build/builds/{run_id}")
    records = ado_get(f"build/builds/{run_id}/timeline")["records"]
    failed = [r for r in records if r.get("type") == "Task" and r.get("result") == "failed" and r.get("log")]
    if not failed:
        raise LookupError(f"Run {run_id} has no failed steps (result: {build.get('result')})")

    parts = []
    for r in failed:
        lines = ado_get(f"build/builds/{run_id}/logs/{r['log']['id']}", raw=True).splitlines()
        # drop the timestamp at the start of each line, keep the end of the log
        lines = [re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s?", "", l) for l in lines][-LOG_TAIL_LINES:]
        parts.append(f"===== step: {r['name']} =====\n" + "\n".join(lines))

    run = {
        "id": run_id,
        "pipeline": build["definition"]["name"],
        "pipelineId": build["definition"]["id"],
        "result": build.get("result"),
        "failedSteps": [r["name"] for r in failed],
        "repo": build["repository"]["id"],          # e.g. owner/name for github repos
        "commit": build.get("sourceVersion"),
        "branch": build.get("sourceBranch", "refs/heads/main").replace("refs/heads/", ""),
    }
    return "\n\n".join(parts), run


def start_run(pipeline_id, branch="main"):
    # a plain run on a branch: the pipeline picks the environment from the branch
    # (main -> prod, staging -> staging, any other branch -> dev)
    payload = {"resources": {"repositories": {"self": {"refName": f"refs/heads/{branch}"}}}}
    return ado_request(f"pipelines/{pipeline_id}/runs", payload=payload)["id"]


def run_status(run_id):
    build = ado_get(f"build/builds/{run_id}")
    return build["status"], build.get("result")


# ---------- github: read files at a commit, commit one file ----------
def gh(path, payload=None, method=None):
    headers = {
        "Authorization": f"Bearer {param(GITHUB_TOKEN_PARAM)}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "adar-agent",
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode() if payload is not None else None
    return http("https://api.github.com/" + path, headers, data=data, method=method)


def repo_files(repo, ref, prefixes=("cicd/", "deploy/", "src/")):
    tree = gh(f"repos/{repo}/git/trees/{ref}?recursive=1")["tree"]
    return [t["path"] for t in tree if t["type"] == "blob" and t["path"].startswith(prefixes)]


def read_file(repo, path, ref):
    f = gh(f"repos/{repo}/contents/{urllib.parse.quote(path)}?ref={ref}")
    return base64.b64decode(f["content"]).decode("utf-8"), f["sha"]


def create_branch(repo, branch, sha):
    # new branch pointing at a commit; an existing branch (a retried step) is left as it is
    try:
        gh(f"repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})
    except urllib.error.HTTPError as e:
        if e.code != 422:
            raise


def merge_branch(repo, base, head, message):
    # merge head into base with a merge commit; an empty reply means there was nothing to merge
    res = gh(f"repos/{repo}/merges", {"base": base, "head": head, "commit_message": message})
    return res.get("sha", "")


def create_pr(repo, head, base, title, body):
    # open a pull request, or return the one that is already open for the same branches
    try:
        pr = gh(f"repos/{repo}/pulls", {"title": title, "head": head, "base": base, "body": body})
    except urllib.error.HTTPError as e:
        if e.code != 422:
            raise
        owner = repo.split("/")[0]
        found = gh(f"repos/{repo}/pulls?state=open&head={owner}:{head}&base={base}")
        if not found:
            raise
        pr = found[0]
    return pr["number"], pr["html_url"]


def pr_state(repo, number):
    # "open", "closed" (without merging) or "merged"
    pr = gh(f"repos/{repo}/pulls/{number}")
    return "merged" if pr.get("merged") else pr["state"]


def commit_file(repo, path, content, message, branch="main"):
    # latest sha of the file on the branch, needed to update it
    _, sha = read_file(repo, path, branch)
    author = {"name": os.environ["COMMIT_NAME"], "email": os.environ["COMMIT_EMAIL"]}
    res = gh(f"repos/{repo}/contents/{urllib.parse.quote(path)}", {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "sha": sha,
        "branch": branch,
        "author": author,
        "committer": author,
    }, method="PUT")
    return res["commit"]["sha"]
