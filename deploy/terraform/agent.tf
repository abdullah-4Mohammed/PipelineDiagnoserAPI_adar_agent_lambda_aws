# ---------- auto-fix agent: read -> diagnose -> propose -> approve -> push -> watch until green ----------

# fix records + audit history (one item per fix, pay per request = pennies)
resource "aws_dynamodb_table" "fixes" {
  name         = "${local.resourceName}-fixes"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }
}

# approval requests and results go to email
data "aws_ssm_parameter" "notify_email" {
  name = var.notifyEmailParam
}

resource "aws_sns_topic" "notify" {
  name = "${local.resourceName}-notify"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.notify.arn
  protocol  = "email"
  endpoint  = data.aws_ssm_parameter.notify_email.value
}

# ---------- agent step lambdas (one function per step, same code zip) ----------
locals {
  agentSteps = {
    propose         = { handler = "agent.propose", timeout = 120 }
    requestApproval = { handler = "agent.request_approval", timeout = 30 }
    push            = { handler = "agent.push", timeout = 60 }
    start           = { handler = "agent.start", timeout = 30 }
    check           = { handler = "agent.check", timeout = 30 }
    finish          = { handler = "agent.finish", timeout = 30 }
  }

  agentEnv = merge(local.lambdaEnv, {
    FIX_TABLE     = aws_dynamodb_table.fixes.name
    NOTIFY_TOPIC  = aws_sns_topic.notify.arn
    API_BASE_URL  = "https://${aws_api_gateway_rest_api.api.id}.execute-api.${var.region}.amazonaws.com/dev" # stage name written as text to avoid a dependency cycle
    COMMIT_NAME   = var.commitName
    COMMIT_EMAIL  = var.commitEmail
    COAUTHOR      = var.coAuthor
  })
}

resource "aws_lambda_function" "agent" {
  for_each         = local.agentSteps
  function_name    = "${local.resourceName}-agent-${each.key}"
  handler          = each.value.handler
  timeout          = each.value.timeout
  runtime          = "python3.12"
  memory_size      = 256
  role             = aws_iam_role.agent.arn
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  environment {
    variables = local.agentEnv
  }
}

# ---------- workflow (step functions) ----------
resource "aws_sfn_state_machine" "autofix" {
  name     = "${local.resourceName}-autofix"
  role_arn = aws_iam_role.sfn.arn
  definition = templatefile("${path.module}/statemachine/autofix.asl.json", {
    propose         = aws_lambda_function.agent["propose"].arn
    requestApproval = aws_lambda_function.agent["requestApproval"].arn
    push            = aws_lambda_function.agent["push"].arn
    start           = aws_lambda_function.agent["start"].arn
    check           = aws_lambda_function.agent["check"].arn
    finish          = aws_lambda_function.agent["finish"].arn
    maxAttempts     = var.maxAttempts
  })
}

# ---------- auto-fix api lambda (start / status / approve / reject) ----------
resource "aws_lambda_function" "autofix_api" {
  function_name    = "${local.resourceName}-autofix-api"
  handler          = "autofix_api.handler"
  runtime          = "python3.12"
  timeout          = 28
  memory_size      = 256
  role             = aws_iam_role.autofix_api.arn
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  environment {
    variables = merge(local.lambdaEnv, {
      FIX_TABLE         = aws_dynamodb_table.fixes.name
      STATE_MACHINE_ARN = aws_sfn_state_machine.autofix.arn
    })
  }
}

resource "aws_lambda_permission" "autofix_api" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.autofix_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}

# ---------- api routes ----------
# /autofix                      POST  start a fix            (api key)
# /autofix/{id}                 GET   status, diff           (api key)
# /autofix/{id}/decision        POST  approve / reject       (api key)
# /autofix/{id}/email-decision  GET   email approve / reject (one-time secret in the link)
resource "aws_api_gateway_resource" "autofix" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "autofix"
}

resource "aws_api_gateway_resource" "autofix_id" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.autofix.id
  path_part   = "{id}"
}

resource "aws_api_gateway_resource" "autofix_decision" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.autofix_id.id
  path_part   = "decision"
}

resource "aws_api_gateway_resource" "autofix_email" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_resource.autofix_id.id
  path_part   = "email-decision"
}

locals {
  autofixRoutes = {
    start    = { resource = aws_api_gateway_resource.autofix.id, method = "POST", key = true }
    status   = { resource = aws_api_gateway_resource.autofix_id.id, method = "GET", key = true }
    decision = { resource = aws_api_gateway_resource.autofix_decision.id, method = "POST", key = true }
    email    = { resource = aws_api_gateway_resource.autofix_email.id, method = "GET", key = false }
  }
  # resources called from the browser page need a CORS preflight
  autofixCors = {
    start    = aws_api_gateway_resource.autofix.id
    status   = aws_api_gateway_resource.autofix_id.id
    decision = aws_api_gateway_resource.autofix_decision.id
  }
}

resource "aws_api_gateway_method" "autofix" {
  for_each         = local.autofixRoutes
  rest_api_id      = aws_api_gateway_rest_api.api.id
  resource_id      = each.value.resource
  http_method      = each.value.method
  authorization    = "NONE"
  api_key_required = each.value.key
}

resource "aws_api_gateway_integration" "autofix" {
  for_each                = local.autofixRoutes
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = each.value.resource
  http_method             = aws_api_gateway_method.autofix[each.key].http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.autofix_api.invoke_arn
}

resource "aws_api_gateway_method" "autofix_options" {
  for_each      = local.autofixCors
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = each.value
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "autofix_options" {
  for_each    = local.autofixCors
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = each.value
  http_method = aws_api_gateway_method.autofix_options[each.key].http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "autofix_options" {
  for_each    = local.autofixCors
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = each.value
  http_method = aws_api_gateway_method.autofix_options[each.key].http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
  }
}

resource "aws_api_gateway_integration_response" "autofix_options" {
  for_each    = local.autofixCors
  depends_on  = [aws_api_gateway_integration.autofix_options]
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = each.value
  http_method = aws_api_gateway_method.autofix_options[each.key].http_method
  status_code = aws_api_gateway_method_response.autofix_options[each.key].status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = "'*'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,POST,OPTIONS'"
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type,x-api-key'"
  }
}

# ---------- IAM (least privilege per role) ----------
resource "aws_iam_role" "agent" {
  name = "${local.resourceName}-agent-role"
  assume_role_policy = <<EOT
{
  "Version": "2012-10-17",
  "Statement": [{ "Effect": "Allow", "Principal": { "Service": "lambda.amazonaws.com" }, "Action": "sts:AssumeRole" }]
}
EOT
}

resource "aws_iam_role_policy" "agent" {
  name = "${local.resourceName}-agent-policy"
  role = aws_iam_role.agent.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:*:*:*" },
      { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = local.agentParamArns },
      { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.fixes.arn },
      { Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.notify.arn },
    ]
  })
}

resource "aws_iam_role" "autofix_api" {
  name = "${local.resourceName}-autofix-api-role"
  assume_role_policy = <<EOT
{
  "Version": "2012-10-17",
  "Statement": [{ "Effect": "Allow", "Principal": { "Service": "lambda.amazonaws.com" }, "Action": "sts:AssumeRole" }]
}
EOT
}

resource "aws_iam_role_policy" "autofix_api" {
  name = "${local.resourceName}-autofix-api-policy"
  role = aws_iam_role.autofix_api.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:*:*:*" },
      { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = local.agentParamArns },
      { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.fixes.arn },
      { Effect = "Allow", Action = ["states:StartExecution"], Resource = aws_sfn_state_machine.autofix.arn },
      { Effect = "Allow", Action = ["states:SendTaskSuccess"], Resource = "*" },
    ]
  })
}

resource "aws_iam_role" "sfn" {
  name = "${local.resourceName}-autofix-sfn-role"
  assume_role_policy = <<EOT
{
  "Version": "2012-10-17",
  "Statement": [{ "Effect": "Allow", "Principal": { "Service": "states.amazonaws.com" }, "Action": "sts:AssumeRole" }]
}
EOT
}

resource "aws_iam_role_policy" "sfn" {
  name = "${local.resourceName}-autofix-sfn-policy"
  role = aws_iam_role.sfn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = [for f in aws_lambda_function.agent : f.arn] },
    ]
  })
}

locals {
  paramArnPrefix = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter"
  agentParamArns = [for p in [var.apiKeyParam, var.adoTokenParam, var.adoOrgParam, var.adoProjectParam, var.githubTokenParam] : "${local.paramArnPrefix}${p}"]
}
