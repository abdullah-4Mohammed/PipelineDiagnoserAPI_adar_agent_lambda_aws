# PipelineDiagnoserAPI_adar_agent_lambda_aws

A serverless API that diagnoses failed CI/CD, Terraform and Kubernetes logs. Give it an Azure DevOps run ID or pipeline name (it reads the failed step logs itself, read only), or send any failed log, and it returns the root cause, the failed step, suggested fix steps and a confidence level. Sensitive values (account IDs, keys, secrets, IPs, emails) are masked before the log is analysed.

It can also **auto-fix** a failed Azure DevOps run: it reads the failed log and the repository, proposes a one-file change as a diff, waits for human approval on the web page, commits the approved change, then re-runs and watches the pipeline until it is green (up to 3 attempts).

## Components
- **Web:** static web page on Amazon S3 (private) served by CloudFront
- **API:** Amazon API Gateway (REST), `POST /diagnose`, protected by an API key with a daily usage plan
- **Logic:** AWS Lambda (Python): log masker + a hosted LLM (gpt-oss-120b)
- **Auto-fix workflow:** AWS Step Functions (one Lambda per step: propose, approval, push, run, check), Amazon DynamoDB (fix records and audit history)

## Built with
- Terraform (AWS: S3, CloudFront, API Gateway, Lambda, Step Functions, DynamoDB, IAM)
- Azure DevOps Pipelines (CI/CD)

## License
See [LICENSE](LICENSE).
