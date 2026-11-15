# A real `terraform plan` of both regional deployments in CI, without AWS credentials: the AWS
# provider is mocked, so this checks the stack's wiring and the per-market differences, not AWS
# itself. Run: terraform init -backend=false && terraform test (infra/production).

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["zone-a", "zone-b", "zone-c"] }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { value = "ami-0123456789abcdef0" }
  }
  mock_resource "aws_acm_certificate" {
    defaults = {
      arn = "arn:aws:acm:region:123456789012:certificate/mock"
      domain_validation_options = [{
        domain_name          = "www.saaradhigo.com", resource_record_name = "_x.www.saaradhigo.com.",
        resource_record_type = "CNAME", resource_record_value = "_y.acm-validations.aws."
      }]
    }
  }
}


run "india_ap_south_1" {
  command = plan
  variables {
    market      = "in"
    region      = "ap-south-1"
    host_suffix = ""
    vpc_cidr    = "10.40.0.0/16"
  }
  assert {
    condition     = aws_vpc.main.cidr_block == "10.40.0.0/16"
    error_message = "India VPC CIDR"
  }
  assert {
    condition     = aws_acm_certificate.main.domain_name == "www.saaradhigo.com"
    error_message = "India serves www.saaradhigo.com"
  }
  assert {
    condition     = contains(aws_acm_certificate.main.subject_alternative_names, "saaradhigo.com")
    error_message = "India's certificate covers the apex domain"
  }
  assert {
    condition     = aws_db_instance.main.identifier == "saaradhigo-prod-db" && aws_db_instance.main.multi_az && aws_db_instance.main.storage_encrypted && aws_db_instance.main.deletion_protection
    error_message = "India database: original name, Multi-AZ, encrypted, deletion protected"
  }
  assert {
    condition     = aws_autoscaling_group.role["worker"].max_size == 1
    error_message = "Exactly one Celery worker with beat"
  }
  assert {
    condition     = length(aws_lb_listener.https) == 0
    error_message = "No HTTPS listener until the certificate is validated"
  }
}

run "usa_us_west_2" {
  command = plan
  variables {
    market      = "us"
    region      = "us-west-2"
    host_suffix = "-us"
    vpc_cidr    = "10.41.0.0/16"
  }
  assert {
    condition     = aws_vpc.main.cidr_block == "10.41.0.0/16"
    error_message = "US VPC CIDR"
  }
  assert {
    condition     = aws_acm_certificate.main.domain_name == "www-us.saaradhigo.com" && !contains(aws_acm_certificate.main.subject_alternative_names, "saaradhigo.com")
    error_message = "USA serves www-us and never claims the apex"
  }
  assert {
    condition     = aws_db_instance.main.identifier == "saaradhigo-prod-us-db" && aws_s3_bucket.media.bucket != "saaradhigo-prod-media"
    error_message = "USA has its own database and storage"
  }
  assert {
    condition     = aws_ssm_parameter.backend["DEPLOY_MARKET"].value == "US"
    error_message = "USA instances are told they serve the US market"
  }
}

run "https_after_certificate_validation" {
  command = plan
  variables {
    certificate_validated = true
  }
  assert {
    condition     = length(aws_lb_listener.https) == 1 && length(aws_lb_listener_rule.api) == 1
    error_message = "HTTPS and the api host rule come up once the certificate is validated"
  }
}
