variable "region" {
  description = "AWS region for all regional resources. CloudFront certs must be us-east-1."
  type        = string
  default     = "us-east-1"
}

variable "bucket_name" {
  description = "Existing bucket holding the media. Imported, never created."
  type        = string
}

variable "domain_name" {
  description = "Hostname the gallery is served on. Also the WebAuthn relying-party ID."
  type        = string
}

variable "cognito_domain_prefix" {
  description = "Prefix for the free Cognito-hosted login domain."
  type        = string
}

variable "private_key_param" {
  description = "SSM SecureString holding the private key. Written out of band so it stays out of state."
  type        = string
  default     = "/gallery/cloudfront/private-key"
}

variable "session_ttl" {
  description = "Signed-cookie lifetime in seconds."
  type        = number
  default     = 604800
}

variable "legacy_distribution_arn" {
  description = "Old distribution, kept in the bucket policy until cutover. Empty string once retired."
  type        = string
  default     = ""
}

variable "hmac_param" {
  description = "SSM SecureString signing the identity cookie. Written out of band."
  type        = string
  default     = "/gallery/session/hmac-key"
}
