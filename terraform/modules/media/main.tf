variable "bucket_id" { type = string }
variable "bucket_arn" { type = string }
variable "layers_dir" { type = string }
variable "source_dir" { type = string }
variable "api_source_dir" { type = string }
variable "hmac_param" { type = string }
variable "region" { type = string }

resource "aws_lambda_layer_version" "ffmpeg" {
  layer_name               = "gallery-ffmpeg"
  filename                 = "${var.layers_dir}/ffmpeg.zip"
  source_code_hash         = filebase64sha256("${var.layers_dir}/ffmpeg.zip")
  compatible_architectures = ["arm64"]
  description              = "static ffmpeg + ffprobe at /opt/bin"
}

resource "aws_lambda_layer_version" "models" {
  layer_name               = "gallery-models"
  filename                 = "${var.layers_dir}/models.zip"
  source_code_hash         = filebase64sha256("${var.layers_dir}/models.zip")
  compatible_runtimes      = ["python3.13"]
  compatible_architectures = ["arm64"]
  description              = "pydantic, shared by the processor and the write API"
}

resource "aws_lambda_layer_version" "imaging" {
  layer_name               = "gallery-imaging"
  filename                 = "${var.layers_dir}/pyimaging.zip"
  source_code_hash         = filebase64sha256("${var.layers_dir}/pyimaging.zip")
  compatible_runtimes      = ["python3.13"]
  compatible_architectures = ["arm64"]
}

data "archive_file" "processor" {
  type        = "zip"
  source_dir  = var.source_dir
  output_path = "${path.module}/.build/media-processor.zip"
}

resource "aws_iam_role" "processor" {
  name = "gallery-media-processor"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "processor" {
  name = "media-access"
  role = aws_iam_role.processor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${var.bucket_arn}/*"
      },
      {
        # Delete is scoped to the derived artefacts only. The code already
        # limits itself to these, but a bug in the key helpers must not be able
        # to reach an original.
        Effect = "Allow"
        Action = "s3:DeleteObject"
        Resource = [
          "${var.bucket_arn}/thumbs/*",
          "${var.bucket_arn}/meta/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = var.bucket_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "processor" {
  name              = "/aws/lambda/gallery-media-processor"
  retention_in_days = 14
}

resource "aws_lambda_function" "processor" {
  function_name    = "gallery-media-processor"
  role             = aws_iam_role.processor.arn
  filename         = data.archive_file.processor.output_path
  source_code_hash = data.archive_file.processor.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]
  memory_size      = 2048
  timeout          = 300
  layers = [
    aws_lambda_layer_version.ffmpeg.arn,
    aws_lambda_layer_version.imaging.arn,
    aws_lambda_layer_version.models.arn,
  ]

  # No reserved concurrency: the handler no longer rebuilds the manifest per
  # object, so a bulk upload is linear and needs no cap. Capping it throttled
  # ~15k events in two hours and thumbnails never appeared.

  ephemeral_storage {
    size = 5120
  }

  environment {
    variables = {
      BUCKET = var.bucket_id
    }
  }

  depends_on = [aws_cloudwatch_log_group.processor]
}

resource "aws_lambda_permission" "s3" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.processor.arn
  principal     = "s3.amazonaws.com"
  source_arn    = var.bucket_arn
}

output "function_arn" { value = aws_lambda_function.processor.arn }
output "function_name" { value = aws_lambda_function.processor.function_name }
output "permission_id" { value = aws_lambda_permission.s3.id }


# --- write API -------------------------------------------------------------
# Reached only through a CloudFront behaviour that carries the same trusted key
# group as the media, so CloudFront rejects an unauthenticated caller before
# the function is ever invoked. There is deliberately no auth code inside.

data "archive_file" "api" {
  type        = "zip"
  output_path = "${path.module}/.build/api.zip"

  source {
    content  = file("${var.api_source_dir}/handler.py")
    filename = "handler.py"
  }
  source {
    content  = file("${var.source_dir}/manifest.py")
    filename = "manifest.py"
  }
  source {
    content  = file("${var.source_dir}/media_kinds.py")
    filename = "media_kinds.py"
  }
  source {
    content  = file("${var.source_dir}/models.py")
    filename = "models.py"
  }
  source {
    content  = file("${var.api_source_dir}/schemas.py")
    filename = "schemas.py"
  }
  source {
    content  = file("${var.api_source_dir}/store.py")
    filename = "store.py"
  }
  source {
    content  = file("${var.api_source_dir}/mutations.py")
    filename = "mutations.py"
  }
}

resource "aws_iam_role" "api" {
  name = "gallery-api"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "api" {
  name = "archive-access"
  role = aws_iam_role.api.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        # No DeleteObject: archiving must never be able to remove anything.
        Action   = ["s3:GetObject", "s3:PutObject", "s3:PutObjectTagging", "s3:GetObjectTagging"]
        Resource = "${var.bucket_arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = var.bucket_arn
      },
      {
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.api.account_id}:parameter${var.hmac_param}"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

data "aws_caller_identity" "api" {}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/gallery-api"
  retention_in_days = 14
}

resource "aws_lambda_function" "api" {
  function_name    = "gallery-api"
  role             = aws_iam_role.api.arn
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]
  memory_size      = 1024
  # Renaming a category touches every sidecar, so this is a bulk operation.
  timeout = 300
  layers  = [aws_lambda_layer_version.models.arn]

  environment {
    variables = {
      BUCKET     = var.bucket_id
      HMAC_PARAM = var.hmac_param
    }
  }

  depends_on = [aws_cloudwatch_log_group.api]
}

resource "aws_lambda_function_url" "api" {
  function_name      = aws_lambda_function.api.function_name
  authorization_type = "AWS_IAM"
}

output "api_function_name" { value = aws_lambda_function.api.function_name }
output "api_function_url_domain" {
  value = replace(replace(aws_lambda_function_url.api.function_url, "https://", ""), "/", "")
}
