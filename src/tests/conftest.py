# unit tests run without aws: env vars are fakes, boto3 clients are never called for real
import os
import sys

os.environ.update({
    "AWS_DEFAULT_REGION": "eu-west-2",
    "MODEL_ID": "test-model",
    "API_KEY_PARAM": "/test/api-key",
    "ADO_TOKEN_PARAM": "/test/ado-token",
    "ADO_ORG_PARAM": "/test/ado-org",
    "ADO_PROJECT_PARAM": "/test/ado-project",
    "GITHUB_TOKEN_PARAM": "/test/github-token",
    "FIX_TABLE": "test-fixes",
    "STATE_MACHINE_ARN": "arn:aws:states:eu-west-2:000000000000:stateMachine:test",
    "COMMIT_NAME": "Test",
    "COMMIT_EMAIL": "test@example.com",
    "COAUTHOR": "Tester <tester@example.com>",
})
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))
