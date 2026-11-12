# Everything Srini adds at GoDaddy comes from here (CI prints it in the run summary).
output "dns_records_for_godaddy" {
  description = "Records to add in GoDaddy DNS for saaradhigo.com."
  value = concat(
    [
      { type = "CNAME", name = "www", value = aws_lb.main.dns_name, why = "the website" },
      { type = "CNAME", name = "api", value = aws_lb.main.dns_name, why = "the API and the apps" },
    ],
    [for o in aws_acm_certificate.main.domain_validation_options : {
      type = o.resource_record_type, name = trimsuffix(o.resource_record_name, ".${var.domain}."),
      value = o.resource_record_value, why = "TLS certificate validation (${o.domain_name})"
    }],
    [for t in aws_sesv2_email_identity.domain.dkim_signing_attributes[0].tokens : {
      type = "CNAME", name = "${t}._domainkey", value = "${t}.dkim.amazonses.com", why = "SES DKIM"
    }],
    [
      { type = "MX", name = "mail", value = "10 feedback-smtp.${var.region}.amazonses.com", why = "SES MAIL FROM (bounces)" },
      { type = "TXT", name = "mail", value = "v=spf1 include:amazonses.com ~all", why = "SPF for the MAIL FROM domain" },
    ],
  )
}

output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "ecr_registry" {
  value = local.registry
}

output "asg_names" {
  value = { for k, g in aws_autoscaling_group.role : k => g.name }
}

output "ses_configuration_set" {
  value = aws_sesv2_configuration_set.main.configuration_set_name
}
