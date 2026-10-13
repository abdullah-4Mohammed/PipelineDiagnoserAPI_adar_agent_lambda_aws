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

locals {
  resourceName = "Az-aws-${var.serviceName}-${var.environment}-${var.regionShortName}"
  key = "tf/${var.environment}.tfstate"
  region = "${var.region}"
  backendBucket = "${var.backendBucket}"
}
