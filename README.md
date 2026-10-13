# PipelineDiagnoserAPI_adar_agent_lambda_aws

A serverless API that diagnoses failed CI/CD, Terraform and Kubernetes logs. Send it a failed log and it returns the root cause, the failed step, suggested fix steps and a confidence level. Sensitive values (account IDs, keys, secrets, IPs, emails) are masked before the log is analysed.

## Components
- **Web:** static web page on Amazon S3 (private) served by CloudFront
- **API:** Amazon API Gateway (REST), `POST /diagnose`, protected by an API key with a daily usage plan
- **Logic:** AWS Lambda (Python): log masker + a hosted LLM (gpt-oss-120b)

## Built with
- Terraform (AWS: S3, CloudFront, API Gateway, Lambda, IAM)
- Azure DevOps Pipelines (CI/CD)

## License
See [LICENSE](LICENSE).
