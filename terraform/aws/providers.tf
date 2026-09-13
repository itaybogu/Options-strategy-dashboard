terraform {
  required_version = ">= 1.5.0"

  # S3 Remote Backend Configuration
  backend "s3" {
    bucket         = "options-scanner-tfstate-023133286953" # Must be globally unique
    key            = "aws/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "terraform-state-locks"
    encrypt        = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}