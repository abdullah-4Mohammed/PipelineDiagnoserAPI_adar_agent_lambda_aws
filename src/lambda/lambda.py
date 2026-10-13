import json
import os
import re
import urllib.request
import urllib.error

import boto3

# groq exposes an openai compatible chat completions endpoint
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = os.environ["MODEL_ID"]
KEY_PARAM = os.environ["API_KEY_PARAM"]

# errors are usually at the end of a log, so keep the last part only
MAX_CHARS = 20000

ssm = boto3.client("ssm")
_api_key = None


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
    # body: {"log": "...", "source": "terraform | pipeline | kubernetes"}
    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return response(400, {"error": "Body must be valid JSON"})
    if not isinstance(payload, dict):
        payload = {}

    log = payload.get("log")
    source = str(payload.get("source", "pipeline"))[:30]
    if not isinstance(log, str) or not log.strip():
        return response(400, {"error": "Send JSON like {\"log\": \"failed log text\", \"source\": \"terraform\"}"})

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
    return response(200, result)
