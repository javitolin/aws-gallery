variable "domain_name" { type = string }
variable "cognito_domain_prefix" { type = string }
variable "region" { type = string }
variable "key_pair_id" { type = string }
variable "private_key_param" { type = string }
variable "hmac_param" { type = string }
variable "session_ttl" { type = number }
variable "layers_dir" { type = string }
variable "source_dir" { type = string }

locals {
  site_url = "https://${var.domain_name}"
}

resource "aws_cognito_user_pool" "gallery" {
  name = "gallery"
  # Passkeys as a first-class sign-in factor and managed login v2 both require ESSENTIALS.
  user_pool_tier           = "ESSENTIALS"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OFF"

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = false
  }

  web_authn_configuration {
    relying_party_id  = var.domain_name
    user_verification = "required"
  }

  sign_in_policy {
    allowed_first_auth_factors = ["PASSWORD", "WEB_AUTHN"]
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
    string_attribute_constraints {
      min_length = 5
      max_length = 256
    }
  }
}

resource "aws_cognito_user_pool_domain" "gallery" {
  domain                = var.cognito_domain_prefix
  user_pool_id          = aws_cognito_user_pool.gallery.id
  managed_login_version = 2
}

resource "aws_cognito_managed_login_branding" "gallery" {
  user_pool_id                = aws_cognito_user_pool.gallery.id
  client_id                   = aws_cognito_user_pool_client.gallery.id
  use_cognito_provided_values = true
}

resource "aws_cognito_user_pool_client" "gallery" {
  name         = "gallery-web"
  user_pool_id = aws_cognito_user_pool.gallery.id

  # Public client + PKCE: no secret to store, rotate, or leak into state.
  generate_secret                      = false
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["${local.site_url}/auth/callback"]
  logout_urls                          = ["${local.site_url}/"]
  explicit_auth_flows                  = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_AUTH"]
}

resource "aws_lambda_layer_version" "deps" {
  layer_name               = "gallery-auth-deps"
  filename                 = "${var.layers_dir}/authdeps.zip"
  source_code_hash         = filebase64sha256("${var.layers_dir}/authdeps.zip")
  compatible_runtimes      = ["python3.13"]
  compatible_architectures = ["arm64"]
}

data "archive_file" "auth" {
  type        = "zip"
  source_dir  = var.source_dir
  output_path = "${path.module}/.build/auth.zip"
}

resource "aws_iam_role" "auth" {
  name = "gallery-auth"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

data "aws_caller_identity" "current" {}

resource "aws_iam_role_policy" "auth" {
  name = "signing-key"
  role = aws_iam_role.auth.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = "ssm:GetParameter"
        Resource = [
          "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.private_key_param}",
          "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.hmac_param}",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "auth" {
  name              = "/aws/lambda/gallery-auth"
  retention_in_days = 14
}

resource "aws_lambda_function" "auth" {
  function_name    = "gallery-auth"
  role             = aws_iam_role.auth.arn
  filename         = data.archive_file.auth.output_path
  source_code_hash = data.archive_file.auth.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]
  memory_size      = 512
  timeout          = 15
  layers           = [aws_lambda_layer_version.deps.arn]

  environment {
    variables = {
      COGNITO_DOMAIN    = "${aws_cognito_user_pool_domain.gallery.domain}.auth.${var.region}.amazoncognito.com"
      CLIENT_ID         = aws_cognito_user_pool_client.gallery.id
      USER_POOL_ID      = aws_cognito_user_pool.gallery.id
      REGION            = var.region
      SITE_URL          = local.site_url
      KEY_PAIR_ID       = var.key_pair_id
      PRIVATE_KEY_PARAM = var.private_key_param
      HMAC_PARAM        = var.hmac_param
      SESSION_TTL       = tostring(var.session_ttl)
    }
  }

  depends_on = [aws_cloudwatch_log_group.auth]
}

resource "aws_lambda_function_url" "auth" {
  function_name      = aws_lambda_function.auth.function_name
  authorization_type = "AWS_IAM"
}

output "function_name" { value = aws_lambda_function.auth.function_name }
output "function_arn" { value = aws_lambda_function.auth.arn }
output "function_url_domain" {
  value = replace(replace(aws_lambda_function_url.auth.function_url, "https://", ""), "/", "")
}
output "login_domain" { value = aws_cognito_user_pool_domain.gallery.domain }
output "user_pool_id" { value = aws_cognito_user_pool.gallery.id }
