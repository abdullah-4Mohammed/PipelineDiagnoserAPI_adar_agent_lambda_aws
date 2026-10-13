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
  value = "http://${aws_s3_bucket_website_configuration.web.website_endpoint}"
}
