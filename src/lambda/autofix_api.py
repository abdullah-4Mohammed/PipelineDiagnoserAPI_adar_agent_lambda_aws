"""Auto-fix API: start a fix, read its status, approve or reject it (page or email link)."""
import json
import os
import time
import uuid

import boto3

from shared import latest_failed_run

table = boto3.resource("dynamodb").Table(os.environ["FIX_TABLE"])
sfn = boto3.client("stepfunctions")
STATE_MACHINE = os.environ["STATE_MACHINE_ARN"]
# fields the page is allowed to see (task token and secret stay private)
PUBLIC = ["id", "status", "runId", "attempt", "pipeline", "failedSteps", "file", "rootCause",
          "summary", "confidence", "diff", "commit", "newRunId", "result", "reason", "history"]


def response(status, body, html=False):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "text/html" if html else "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": body if html else json.dumps(body, default=str),
    }


def start(payload):
    run_id = payload.get("runId") or latest_failed_run(str(payload.get("pipeline", "")))
    fix_id = uuid.uuid4().hex[:12]
    table.put_item(Item={"id": fix_id, "status": "started", "runId": int(run_id), "created": int(time.time())})
    sfn.start_execution(stateMachineArn=STATE_MACHINE, name=f"fix-{fix_id}",
                        input=json.dumps({"fixId": fix_id, "runId": int(run_id), "attempt": 1}))
    return response(202, {"id": fix_id, "status": "started"})


def decide(fix_id, decision):
    item = table.get_item(Key={"id": fix_id}).get("Item")
    if not item or item.get("status") != "waiting_approval":
        return False
    sfn.send_task_success(taskToken=item["taskToken"], output=json.dumps({"decision": decision}))
    table.update_item(Key={"id": fix_id}, UpdateExpression="SET #s = :s",
                      ExpressionAttributeNames={"#s": "status"},
                      ExpressionAttributeValues={":s": "approved" if decision == "approve" else "rejecting"})
    return True


def handler(event, context):
    method, path = event["httpMethod"], event["resource"]
    params = event.get("pathParameters") or {}
    query = event.get("queryStringParameters") or {}

    try:
        if method == "POST" and path == "/autofix":
            return start(json.loads(event.get("body") or "{}"))

        fix_id = params.get("id", "")
        if method == "GET" and path == "/autofix/{id}":
            item = table.get_item(Key={"id": fix_id}).get("Item")
            if not item:
                return response(404, {"error": "Fix not found"})
            return response(200, {k: item[k] for k in PUBLIC if k in item})

        if method == "POST" and path == "/autofix/{id}/decision":
            decision = json.loads(event.get("body") or "{}").get("decision")
            if decision not in ("approve", "reject"):
                return response(400, {"error": "decision must be approve or reject"})
            ok = decide(fix_id, decision)
            return response(200 if ok else 409, {"ok": ok} if ok else {"error": "Fix is not waiting for approval"})

        if method == "GET" and path == "/autofix/{id}/email-decision":
            # email links: protected by a one-time secret instead of the api key
            item = table.get_item(Key={"id": fix_id}).get("Item") or {}
            decision = query.get("decision")
            if not item.get("secret") or query.get("secret") != item["secret"] or decision not in ("approve", "reject"):
                return response(403, "<h3>Link is not valid.</h3>", html=True)
            ok = decide(fix_id, decision)
            msg = f"Fix {decision}d. You can close this page." if ok else "This fix is no longer waiting for approval."
            return response(200, f"<h3>{msg}</h3>", html=True)
    except LookupError as e:
        return response(404, {"error": str(e)})

    return response(404, {"error": "Not found"})
