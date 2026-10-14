# full url to call the api
output "api_url" {
  value = "${aws_api_gateway_stage.api_stage.invoke_url}/${aws_api_gateway_resource.diagnose.path_part}"
}

# api key id (the key value itself is read from the api gateway console)
output "api_key_id" {
  value = aws_api_gateway_api_key.key.id
}

# web page url
output "web_url" {
  value = "https://${aws_cloudfront_distribution.web.domain_name}"
}

# used by the web deploy job (s3 sync + cloudfront invalidation)
output "web_bucket" {
  value = aws_s3_bucket.web.id
}

output "web_distribution_id" {
  value = aws_cloudfront_distribution.web.id
}

# auto-fix workflow (for checking runs in the step functions console)
output "autofix_state_machine" {
  value = aws_sfn_state_machine.autofix.name
}
