variable "environment" {
  type = string
}

variable "serviceName" {
  type = string
}

variable "regionShortName" {
  type = string
}

variable "backendBucket" {
  type = string
}

variable "region" {
  type = string
}

# llm model used by the lambda
variable "modelId" {
  type = string
}

# ssm parameter (SecureString) holding the model provider api key
variable "apiKeyParam" {
  type = string
}

# ssm parameters for reading azure devops runs (token, organisation, project)
variable "adoTokenParam" {
  type = string
}

variable "adoOrgParam" {
  type = string
}

variable "adoProjectParam" {
  type = string
}

# max calls per day through the api key (usage plan)
variable "dailyQuota" {
  type = number
}

# ssm parameter (SecureString) with a github token that can read + commit repo contents
variable "githubTokenParam" {
  type = string
}

# ssm parameter (String) with the email that receives approval requests
variable "notifyEmailParam" {
  type = string
}

# commit identity used by the agent when it pushes an approved fix
variable "commitName" {
  type = string
}

variable "commitEmail" {
  type = string
}

variable "coAuthor" {
  type = string
}

# how many fix attempts before the agent stops
variable "maxAttempts" {
  type = number
}

locals {
  resourceName = "Az-aws-${var.serviceName}-${var.environment}-${var.regionShortName}"
  key = "tf/${var.environment}.tfstate"
  region = "${var.region}"
  backendBucket = "${var.backendBucket}"

  # environment shared by every lambda (they all import shared.py)
  lambdaEnv = {
    MODEL_ID           = var.modelId
    API_KEY_PARAM      = var.apiKeyParam
    ADO_TOKEN_PARAM    = var.adoTokenParam
    ADO_ORG_PARAM      = var.adoOrgParam
    ADO_PROJECT_PARAM  = var.adoProjectParam
    GITHUB_TOKEN_PARAM = var.githubTokenParam
  }
}
