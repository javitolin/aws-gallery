terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }

  # Deliberately NOT the media bucket: that one is served by CloudFront, and
  # state sitting in it was publicly readable until the default behaviour was
  # scoped to site/.
  # Partial config: bucket and key come from backend.hcl, which is gitignored.
  #   terraform init -backend-config=backend.hcl
  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "photo-gallery"
      ManagedBy = "terraform"
    }
  }
}
