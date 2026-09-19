locals {
  layers_dir = "${path.root}/../layers"
}

module "storage" {
  source      = "./modules/storage"
  bucket_name = var.bucket_name
}

module "media" {
  source         = "./modules/media"
  bucket_id      = module.storage.id
  bucket_arn     = module.storage.arn
  layers_dir     = local.layers_dir
  source_dir     = "${path.root}/../lambdas/media_processor"
  api_source_dir = "${path.root}/../lambdas/api"
  hmac_param     = var.hmac_param
  region         = var.region
}

module "auth" {
  source                = "./modules/auth"
  domain_name           = var.domain_name
  cognito_domain_prefix = var.cognito_domain_prefix
  region                = var.region
  key_pair_id           = module.cdn.key_pair_id
  private_key_param     = var.private_key_param
  hmac_param            = var.hmac_param
  session_ttl           = var.session_ttl
  layers_dir            = local.layers_dir
  source_dir            = "${path.root}/../lambdas/auth"
}

module "cdn" {
  source                      = "./modules/cdn"
  domain_name                 = var.domain_name
  bucket_id                   = module.storage.id
  bucket_regional_domain_name = module.storage.regional_domain_name
  auth_function_url_domain    = module.auth.function_url_domain
  api_function_url_domain     = module.media.api_function_url_domain
  public_key_pem              = file("${path.root}/keys/cloudfront_public_key.pem")
}

# Bucket policy and the CloudFront->Lambda grant live here rather than inside the
# modules: both need the distribution ARN, which would otherwise close a cycle.
data "aws_iam_policy_document" "bucket" {
  statement {
    sid     = "AllowCloudFrontRead"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    resources = ["${module.storage.arn}/*"]
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = compact([module.cdn.distribution_arn, var.legacy_distribution_arn])
    }
  }
}

resource "aws_s3_bucket_policy" "gallery" {
  bucket = module.storage.id
  policy = data.aws_iam_policy_document.bucket.json
}

# OAC needs both actions granted: InvokeFunctionUrl alone yields
# AccessDeniedException from the function URL before the handler ever runs.
# Only the URL action accepts function_url_auth_type.
resource "aws_lambda_permission" "cloudfront_auth_url" {
  statement_id           = "AllowCloudFrontInvokeUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = module.auth.function_name
  principal              = "cloudfront.amazonaws.com"
  source_arn             = module.cdn.distribution_arn
  function_url_auth_type = "AWS_IAM"
}

resource "aws_lambda_permission" "cloudfront_auth_invoke" {
  statement_id  = "AllowCloudFrontInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.auth.function_name
  principal     = "cloudfront.amazonaws.com"
  source_arn    = module.cdn.distribution_arn
}

resource "aws_lambda_permission" "cloudfront_api_url" {
  statement_id           = "AllowCloudFrontInvokeUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = module.media.api_function_name
  principal              = "cloudfront.amazonaws.com"
  source_arn             = module.cdn.distribution_arn
  function_url_auth_type = "AWS_IAM"
}

resource "aws_lambda_permission" "cloudfront_api_invoke" {
  statement_id  = "AllowCloudFrontInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.media.api_function_name
  principal     = "cloudfront.amazonaws.com"
  source_arn    = module.cdn.distribution_arn
}

resource "aws_s3_bucket_notification" "media" {
  bucket = module.storage.id

  lambda_function {
    lambda_function_arn = module.media.function_arn
    events              = ["s3:ObjectCreated:*", "s3:ObjectRemoved:*"]
    filter_prefix       = "media/"
  }

  depends_on = [module.media]
}
