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
# lines kept from the end of each failed step log
LOG_TAIL_LINES = 150

# errors are usually at the end of a log, so keep the last part only
MAX_CHARS = 20000

ssm = boto3.client("ssm")
_api_key = None
_ado = None


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
def get_api_key():
    # read once per container, then reuse on warm invocations
    global _api_key
    if _api_key is None:
        _api_key = ssm.get_parameter(Name=KEY_PARAM, WithDecryption=True)["Parameter"]["Value"]
    return _api_key


PROMPT = """You are a senior Cloud/DevOps engineer. Diagnose this failed {source} log.
Placeholders like ACCOUNT_1, IP_1, SECRET_1 are masked values, keep them as they are.
Reply only with JSON:
{{
  "rootCause": "one or two sentences",
  "failedStep": "the step, resource or file that failed",
  "fix": ["concrete step 1", "step 2"],
  "codeSnippet": "a code/config change if one helps, else empty string",
  "confidence": "high | medium | low"
}}

Log:
{log}"""


def diagnose(log, source):
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT.format(source=source, log=log)}],
        "temperature": 0.1,
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
    }).encode()

    req = urllib.request.Request(GROQ_URL, data=body, headers={
        "Authorization": f"Bearer {get_api_key()}",
        "Content-Type": "application/json",
        "User-Agent": "pipeline-diagnoser-api",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.loads(r.read())
    return json.loads(data["choices"][0]["message"]["content"])


# ---------- azure devops: find the failed run and read its logs (read only) ----------
def ado_config():
    # read once per container, then reuse on warm invocations
    global _ado
    if _ado is None:
        _ado = {k: ssm.get_parameter(Name=v, WithDecryption=True)["Parameter"]["Value"] for k, v in ADO_PARAMS.items()}
    return _ado


def ado_get(path, raw=False):
    cfg = ado_config()
    url = f"https://dev.azure.com/{cfg['org']}/{cfg['project']}/_apis/{path}"
    url += ("&" if "?" in url else "?") + "api-version=7.1"
    auth = base64.b64encode(f":{cfg['token']}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read().decode("utf-8", "replace")
    return body if raw else json.loads(body)


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
        "result": build.get("result"),
        "failedSteps": [r["name"] for r in failed],
    }
    return "\n\n".join(parts), run


# ---------- api ----------
def response(status, body):
    # API Gateway proxy integration expects this shape
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",  # lets the web page read the response
        },
        "body": json.dumps(body),
    }


def handler(event, context):
    # body, one of:
    #   {"log": "...", "source": "terraform | pipeline | kubernetes"}
    #   {"runId": 652}                     -> reads the failed step logs from azure devops
    #   {"pipeline": "infra-my-pipeline"}  -> uses the latest failed run of that pipeline
    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return response(400, {"error": "Body must be valid JSON"})
    if not isinstance(payload, dict):
        payload = {}

    run = None
    if payload.get("runId") or payload.get("pipeline"):
        try:
            run_id = int(payload["runId"]) if payload.get("runId") else latest_failed_run(str(payload["pipeline"]))
            log, run = fetch_failed_log(run_id)
        except (ValueError, TypeError):
            return response(400, {"error": "runId must be a number"})
        except LookupError as e:
            return response(404, {"error": str(e)})
        except urllib.error.HTTPError as e:
            msg = "Run not found in Azure DevOps" if e.code == 404 else f"Azure DevOps returned {e.code}"
            return response(404 if e.code == 404 else 502, {"error": msg})
        source = "azure devops pipeline"
    else:
        log = payload.get("log")
        source = str(payload.get("source", "pipeline"))[:30]
        if not isinstance(log, str) or not log.strip():
            return response(400, {"error": "Send {\"log\": \"...\"} or {\"runId\": 123} or {\"pipeline\": \"name\"}"})

    truncated = len(log) > MAX_CHARS
    masked, counts = mask(log[-MAX_CHARS:])

    try:
        result = diagnose(masked, source)
    except urllib.error.HTTPError as e:
        # 429 = model provider rate limit
        status = 429 if e.code == 429 else 502
        return response(status, {"error": f"Model provider returned {e.code}"})
    except Exception:
        return response(502, {"error": "Could not get a diagnosis, try again"})

    result["model"] = MODEL
    result["masked"] = counts          # what was hidden before sending, e.g. {"ACCOUNT": 1}
    result["logTruncated"] = truncated
    if run:
        result["run"] = run
    return response(200, result)
