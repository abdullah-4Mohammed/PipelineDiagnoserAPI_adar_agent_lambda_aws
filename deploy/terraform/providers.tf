terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
      version = "~> 5.42.0" #"4.22.0"
    }
  }
  # values are passed by the pipeline at init (bucket, key, region),
  # terraform does not allow variables or locals inside the backend block
  backend "s3" {}
}

provider "aws" {
  region = "${local.region}"
}
