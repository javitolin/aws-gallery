variable "bucket_name" { type = string }

resource "aws_s3_bucket" "gallery" {
  bucket = var.bucket_name

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "gallery" {
  bucket                  = aws_s3_bucket.gallery.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Added after an accidental mass delete. Without this there is no undo for any
# mistake, human or scripted, and the contents are irreplaceable.
resource "aws_s3_bucket_versioning" "gallery" {
  bucket = aws_s3_bucket.gallery.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "gallery" {
  bucket = aws_s3_bucket.gallery.id

  # Safety net: the migration script already writes archive/ objects as GLACIER_IR,
  # this catches anything dropped in later at STANDARD.
  rule {
    id     = "archive-to-glacier-ir"
    status = "Enabled"
    filter {
      prefix = "archive/"
    }
    transition {
      days          = 1
      storage_class = "GLACIER_IR"
    }
  }

  # Set by the gallery's archive action. The object is never moved or renamed;
  # only its storage class changes, and S3 does that on its daily sweep.
  rule {
    id     = "archived-to-glacier-ir"
    status = "Enabled"
    filter {
      tag {
        key   = "archived"
        value = "true"
      }
    }
    transition {
      days          = 1
      storage_class = "GLACIER_IR"
    }
  }

  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

output "id" { value = aws_s3_bucket.gallery.id }
output "arn" { value = aws_s3_bucket.gallery.arn }
output "regional_domain_name" { value = aws_s3_bucket.gallery.bucket_regional_domain_name }
