import json
import urllib.error

from shared import MODEL, call_llm, fetch_failed_log, latest_failed_run, mask

# errors are usually at the end of a log, so keep the last part only
MAX_CHARS = 20000


# ---------- model prompt ----------
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
    return call_llm(PROMPT.format(source=source, log=log))


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
