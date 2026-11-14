variable "region" {
  type    = string
  default = "ap-south-1"
}

# 14 Nov (owner ruling): one codebase, two regional deployments, each with its own database and
# storage so each country's data stays in its region and with its own company:
#   in  AWS Mumbai (ap-south-1)  DeepTrics Software Solutions Pvt Ltd, Razorpay
#   us  AWS Oregon (us-west-2)   DeepTrics LLC, Stripe
# India keeps every name it had (`saaradhigo-prod`, /saaradhigo/production/...), so nothing there
# is recreated. Select with -var-file=regions/<market>.tfvars and the state key in deploy.yml.
variable "market" {
  description = "in | us: which regional deployment this state describes."
  type        = string
  default     = "in"
  validation {
    condition     = contains(["in", "us"], var.market)
    error_message = "market must be in or us."
  }
}

variable "host_suffix" {
  description = "Appended to the www and api host names: '' for India (www, api), '-us' for the USA (www-us, api-us). The owner confirms the US host names (docs/config-required.md)."
  type        = string
  default     = ""
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "domain" {
  type    = string
  default = "saaradhigo.com"
}

variable "image_tag" {
  description = "The commit SHA CI built and pushed to ECR for this deploy."
  type        = string
  default     = "latest"
}

variable "web_image_tag" {
  description = "The web image the web repo last pushed ('none' before its first production push: no web instances)."
  type        = string
  default     = "none"
}

variable "certificate_validated" {
  description = "False until Srini has added the ACM validation CNAMEs at GoDaddy (docs/config-required.md). While false, the ALB serves HTTP only and HTTPS is not created."
  type        = bool
  default     = false
}

variable "alert_email" {
  description = "Where CloudWatch alarms are emailed. Empty: alarms are created without a subscriber."
  type        = string
  default     = ""
}

variable "app_instance_type" {
  type    = string
  default = "t3.medium"
}

variable "worker_instance_type" {
  type    = string
  default = "t3.small"
}

variable "web_instance_type" {
  type    = string
  default = "t3.small"
}

variable "db_instance_class" {
  type    = string
  default = "db.t3.medium"
}
