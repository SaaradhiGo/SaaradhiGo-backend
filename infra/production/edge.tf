# TLS certificate for the three hostnames. DNS is at GoDaddy, so validation is a manual step:
# the CNAMEs are an output (and listed in docs/config-required.md); once Srini has added them,
# the repository variable PRODUCTION_CERT_VALIDATED is set to true and HTTPS comes up.
resource "aws_acm_certificate" "main" {
  domain_name               = local.www_host
  subject_alternative_names = var.market == "in" ? [var.domain, local.api_host] : [local.api_host]
  validation_method         = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate_validation" "main" {
  count           = var.certificate_validated ? 1 : 0
  certificate_arn = aws_acm_certificate.main.arn
}

resource "aws_lb" "main" {
  name                       = local.name
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = aws_subnet.public[*].id
  idle_timeout               = 120 # websockets ping every 20 s
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "app" {
  name                 = "${local.name}-app"
  port                 = 8000
  protocol             = "HTTP"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 30
  health_check {
    path    = "/healthz"
    matcher = "200"
  }
}

resource "aws_lb_target_group" "web" {
  name                 = "${local.name}-web"
  port                 = 3000
  protocol             = "HTTP"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 30
  health_check {
    path    = "/robots.txt"
    matcher = "200"
  }
}

# Before the certificate is validated, port 80 serves the site (so the stack can be checked);
# afterwards it redirects to HTTPS.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = var.certificate_validated ? "redirect" : "forward"
    target_group_arn = var.certificate_validated ? null : aws_lb_target_group.web.arn
    dynamic "redirect" {
      for_each = var.certificate_validated ? [1] : []
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

resource "aws_lb_listener" "https" {
  count             = var.certificate_validated ? 1 : 0
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.main[0].certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

# api.saaradhigo.com -> the Django app (HTTP and websockets on one port).
resource "aws_lb_listener_rule" "api" {
  count        = var.certificate_validated ? 1 : 0
  listener_arn = aws_lb_listener.https[0].arn
  priority     = 10
  condition {
    host_header {
      values = [local.api_host]
    }
  }
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

# saaradhigo.com -> www.saaradhigo.com (GoDaddy forwarding does the same for the apex record).
resource "aws_lb_listener_rule" "apex" {
  count        = var.certificate_validated ? 1 : 0
  listener_arn = aws_lb_listener.https[0].arn
  priority     = 20
  condition {
    host_header {
      values = [var.domain]
    }
  }
  action {
    type = "redirect"
    redirect {
      host        = local.www_host
      status_code = "HTTP_301"
    }
  }
}

# AWS managed rules in front of everything: common exploits, known bad inputs, IP reputation.
resource "aws_wafv2_web_acl" "main" {
  name  = local.name
  scope = "REGIONAL"
  default_action {
    allow {}
  }
  dynamic "rule" {
    for_each = {
      AWSManagedRulesCommonRuleSet          = 1
      AWSManagedRulesKnownBadInputsRuleSet  = 2
      AWSManagedRulesAmazonIpReputationList = 3
    }
    content {
      name     = rule.key
      priority = rule.value
      override_action {
        none {}
      }
      statement {
        managed_rule_group_statement {
          name        = rule.key
          vendor_name = "AWS"
        }
      }
      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = rule.key
        sampled_requests_enabled   = true
      }
    }
  }
  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = local.name
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_web_acl_association" "main" {
  resource_arn = aws_lb.main.arn
  web_acl_arn  = aws_wafv2_web_acl.main.arn
}
