output "site_url" { value = "https://${var.domain_name}" }

output "distribution_domain" {
  description = "CNAME target for the photos record at the registrar."
  value       = module.cdn.distribution_domain
}

output "distribution_id" { value = module.cdn.distribution_id }

output "acm_validation_record" {
  description = "Add this CNAME at the registrar, then the cert issues."
  value       = module.cdn.acm_validation
}

output "login_domain" {
  value = "${module.auth.login_domain}.auth.${var.region}.amazoncognito.com"
}

output "user_pool_id" { value = module.auth.user_pool_id }
