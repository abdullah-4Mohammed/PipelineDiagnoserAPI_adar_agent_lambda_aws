# account id and region, used to build the ssm parameter arn
data "aws_caller_identity" "current" {}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/../../src/lambda/lambda.py"
  output_path = "${path.module}/../../src/lambda/lambda.zip"
}

# lambda function (python) as the backend for the api gateway
resource "aws_lambda_function" "lam" {
  function_name = "${local.resourceName}-lam"
  handler = "lambda.handler"
  runtime = "python3.12"
  role = aws_iam_role.role.arn
  filename = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout = 28      # just under the api gateway 29s limit
  memory_size = 256

  environment {
    variables = {
      MODEL_ID      = var.modelId
      API_KEY_PARAM = var.apiKeyParam
      ADO_TOKEN_PARAM   = var.adoTokenParam
      ADO_ORG_PARAM     = var.adoOrgParam
      ADO_PROJECT_PARAM = var.adoProjectParam
    }
  }
}

# add api gateway
resource "aws_api_gateway_rest_api" "api" {
  name = "${local.resourceName}-api-gw"
}

# path: /diagnose
resource "aws_api_gateway_resource" "diagnose" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id = aws_api_gateway_rest_api.api.root_resource_id
  path_part = "diagnose"
}

# POST because the text is sent in the request body,
# api key required so only callers with the key can use it (and the quota applies)
resource "aws_api_gateway_method" "api_method" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.diagnose.id
  http_method = "POST"
  authorization = "NONE"
  api_key_required = true
}

#  this code block creates a new API Gateway integration that responds to
#   the specified HTTP method on the specified resource, calls the specified 
#   Lambda function when invoked, and uses a Lambda proxy integration.
#   (lambda is always invoked with POST, whatever the client method is)

resource "aws_api_gateway_integration" "api_integration" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.diagnose.id
  http_method = aws_api_gateway_method.api_method.http_method
  integration_http_method = "POST"
  type = "AWS_PROXY"
  uri = aws_lambda_function.lam.invoke_arn
}

resource "aws_api_gateway_deployment" "api_deployment" {
  depends_on = [aws_api_gateway_integration.api_integration]
  rest_api_id = aws_api_gateway_rest_api.api.id

  # redeploy the api when its routes change
  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.diagnose.id,
      aws_api_gateway_method.api_method.id,
      aws_api_gateway_integration.api_integration.id,
      aws_api_gateway_integration.options.id,
      aws_api_gateway_integration_response.options.id,
      aws_api_gateway_gateway_response.cors_4xx.id,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "api_stage" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  stage_name = "dev"
  deployment_id = aws_api_gateway_deployment.api_deployment.id
}

# api key + usage plan: caps calls per day and throttles bursts,
# so the model provider limits are never hammered
resource "aws_api_gateway_api_key" "key" {
  name = "${local.resourceName}-key"
}

resource "aws_api_gateway_usage_plan" "plan" {
  name = "${local.resourceName}-plan"

  api_stages {
    api_id = aws_api_gateway_rest_api.api.id
    stage  = aws_api_gateway_stage.api_stage.stage_name
  }

  quota_settings {
    limit  = var.dailyQuota
    period = "DAY"
  }

  throttle_settings {
    rate_limit  = 1
    burst_limit = 2
  }
}

resource "aws_api_gateway_usage_plan_key" "plan_key" {
  key_id        = aws_api_gateway_api_key.key.id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.plan.id
}

resource "aws_lambda_permission" "api_lambda_permission" {
  statement_id = "AllowAPIGatewayInvoke"
  action = "lambda:InvokeFunction"
  function_name = aws_lambda_function.lam.function_name
  principal = "apigateway.amazonaws.com"
  source_arn = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}



# Create the IAM role
resource "aws_iam_role" "role" {
  name = "${local.resourceName}-role"
  assume_role_policy = <<EOT
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": [
          "lambda.amazonaws.com"
        ]
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOT
}

# Create the policy: write logs + read only the parameters it needs
resource "aws_iam_policy" "policy" {
  name        = "${local.resourceName}-policy"
  description = "Policy for Lambda CloudWatch Logs and the model api key"

  policy = <<EOT
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter"
      ],
      "Resource": [
        "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.apiKeyParam}",
        "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.adoTokenParam}",
        "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.adoOrgParam}",
        "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.adoProjectParam}"
      ]
    }
  ]
}
EOT
}

# Attach the policy to the role
resource "aws_iam_role_policy_attachment" "policy-attachment" {
  role = aws_iam_role.role.name
  policy_arn = aws_iam_policy.policy.arn
}
