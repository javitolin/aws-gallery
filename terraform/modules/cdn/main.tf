variable "domain_name" { type = string }
variable "bucket_id" { type = string }
variable "bucket_regional_domain_name" { type = string }
variable "auth_function_url_domain" { type = string }
variable "api_function_url_domain" { type = string }
variable "public_key_pem" { type = string }

resource "aws_acm_certificate" "gallery" {
  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# DNS lives at the registrar, so validation is a manual record. Terraform waits here.
resource "aws_acm_certificate_validation" "gallery" {
  certificate_arn = aws_acm_certificate.gallery.arn
}

resource "aws_cloudfront_public_key" "gallery" {
  name        = "gallery-signing-key"
  encoded_key = var.public_key_pem
  comment     = "Signs viewer session cookies"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_cloudfront_key_group" "gallery" {
  name  = "gallery-viewers"
  items = [aws_cloudfront_public_key.gallery.id]
}

resource "aws_cloudfront_origin_access_control" "s3" {
  name                              = "gallery-s3"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_origin_access_control" "lambda" {
  name                              = "gallery-auth-lambda"
  origin_access_control_origin_type = "lambda"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_function" "gate" {
  name    = "gallery-session-gate"
  runtime = "cloudfront-js-2.0"
  publish = true
  comment = "Redirects visitors without a session to the login flow"
  code    = file("${path.module}/gate.js")
}

locals {
  s3_origin     = "s3-gallery"
  site_origin   = "s3-site"
  lambda_origin = "auth-lambda"
  api_origin    = "api-lambda"
  # Managed policies: CachingOptimized, CachingDisabled, AllViewerExceptHostHeader.
  cache_optimized        = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  cache_disabled         = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
  all_viewer_except_host = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
}

resource "aws_cloudfront_distribution" "gallery" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "photo gallery"
  default_root_object = "index.html"
  aliases             = [var.domain_name]
  price_class         = "PriceClass_100"

  origin {
    origin_id                = local.s3_origin
    domain_name              = var.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.s3.id
  }

  # Same bucket, scoped to site/. A request for anything outside that prefix
  # cannot resolve through the default behaviour at all.
  origin {
    origin_id                = local.site_origin
    domain_name              = var.bucket_regional_domain_name
    origin_path              = "/site"
    origin_access_control_id = aws_cloudfront_origin_access_control.s3.id
  }

  origin {
    origin_id                = local.lambda_origin
    domain_name              = var.auth_function_url_domain
    origin_access_control_id = aws_cloudfront_origin_access_control.lambda.id

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  origin {
    origin_id                = local.api_origin
    domain_name              = var.api_function_url_domain
    origin_access_control_id = aws_cloudfront_origin_access_control.lambda.id

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  # Same key group as the media, so CloudFront rejects an unauthenticated
  # caller before the function runs and the API needs no auth code of its own.
  ordered_cache_behavior {
    path_pattern             = "/api/*"
    target_origin_id         = local.api_origin
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = local.cache_disabled
    origin_request_policy_id = local.all_viewer_except_host
    trusted_key_groups       = [aws_cloudfront_key_group.gallery.id]
  }

  # Must stay reachable without a session — it is what hands out the session.
  ordered_cache_behavior {
    path_pattern             = "/auth/*"
    target_origin_id         = local.lambda_origin
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = local.cache_disabled
    origin_request_policy_id = local.all_viewer_except_host
  }

  # Holds the filename listing, so it stays signature-protected. The frontend
  # turns the 403 into a login redirect.
  ordered_cache_behavior {
    path_pattern           = "/manifest.json"
    target_origin_id       = local.s3_origin
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = local.cache_disabled
    trusted_key_groups     = [aws_cloudfront_key_group.gallery.id]
  }

  dynamic "ordered_cache_behavior" {
    for_each = ["/media/*", "/thumbs/*", "/archive/*", "/meta/*", "/favourites/*"]
    content {
      path_pattern           = ordered_cache_behavior.value
      target_origin_id       = local.s3_origin
      viewer_protocol_policy = "redirect-to-https"
      allowed_methods        = ["GET", "HEAD"]
      cached_methods         = ["GET", "HEAD"]
      compress               = true
      cache_policy_id        = local.cache_optimized
      trusted_key_groups     = [aws_cloudfront_key_group.gallery.id]
    }
  }

  default_cache_behavior {
    target_origin_id       = local.site_origin
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = local.cache_disabled
    # Explicitly empty: omitting it leaves any previously-set key group in place.
    trusted_key_groups = []

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.gate.arn
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.gallery.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

output "distribution_arn" { value = aws_cloudfront_distribution.gallery.arn }
output "distribution_id" { value = aws_cloudfront_distribution.gallery.id }
output "distribution_domain" { value = aws_cloudfront_distribution.gallery.domain_name }
output "key_pair_id" { value = aws_cloudfront_public_key.gallery.id }
output "acm_validation" {
  value = { for o in aws_acm_certificate.gallery.domain_validation_options : o.domain_name => {
    name  = o.resource_record_name
    type  = o.resource_record_type
    value = o.resource_record_value
  } }
}
