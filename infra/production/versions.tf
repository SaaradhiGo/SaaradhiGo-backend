# Production on AWS Mumbai (ap-south-1), as designed in SaaradhiGo-docs/system-design.md:
# ALB (TLS, WAF) -> EC2 auto-scaling groups (app: HTTP + websockets on uvicorn; worker: Celery
# with beat) -> RDS Postgres Multi-AZ, ElastiCache Redis, S3 for media and KYC, SSM Parameter Store
# for secrets, SES for email. Applied ONLY by CI on main (.github/workflows/deploy.yml, job
# `infra`), through the GitHub OIDC role in infra/bootstrap. Never applied by hand.
terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
  # State bucket and lock table are created by CI before `init` (deploy.yml); names are fixed.
  backend "s3" {
    bucket         = "saaradhigo-prod-tfstate"
    key            = "production/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "saaradhigo-prod-tflock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "saaradhigo", environment = "production", managed_by = "terraform" }
  }
}
