# Three auto-scaling groups in the private subnets, all pulling images from ECR by commit SHA:
#   app     the Django app (HTTP and websockets on uvicorn), behind api.saaradhigo.com
#   worker  Celery with beat embedded: exactly ONE instance, or every scheduled task double-fires
#   web     the Next.js site, behind www.saaradhigo.com
# Configuration comes from SSM Parameter Store at boot: <ssm_prefix>/<role>/<NAME>
# (/saaradhigo/production for India, /saaradhigo/production-us for the USA)
# (secrets are SecureString; Srini or CI puts the vendor keys there, docs/config-required.md).
# A deploy pushes new images and starts an instance refresh; nothing is changed on a host by hand.

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

data "aws_caller_identity" "me" {}

locals {
  registry = "${data.aws_caller_identity.me.account_id}.dkr.ecr.${var.region}.amazonaws.com"
  roles = {
    app = {
      image = "backend", instance_type = var.app_instance_type, min = 2, max = 4, desired = 2,
      port  = 8000, target_groups = [aws_lb_target_group.app.arn],
      command = "sh -c 'python manage.py collectstatic --noinput && uvicorn base.asgi:application --host 0.0.0.0 --port 8000 --workers 4 --proxy-headers --forwarded-allow-ips=* --ws-ping-interval 20'"
    }
    worker = {
      image = "backend", instance_type = var.worker_instance_type, min = 1, max = 1, desired = 1,
      port  = 0, target_groups = [],
      command = "celery -A base worker -B -l INFO --concurrency 4"
    }
    web = {
      image = "web", instance_type = var.web_instance_type,
      min = var.web_image_tag == "none" ? 0 : 2, max = 4, desired = var.web_image_tag == "none" ? 0 : 2,
      port  = 3000, target_groups = [aws_lb_target_group.web.arn],
      command = ""
    }
  }
}

# Non-secret values Terraform knows; secrets are added beside them as SecureString.
resource "aws_ssm_parameter" "backend" {
  for_each = {
    ENVIRONMENT            = "production"
    DB_HOST                = aws_db_instance.main.address
    DB_NAME                = aws_db_instance.main.db_name
    DB_USER                = aws_db_instance.main.username
    DB_SSLMODE             = "require"
    REDIS_URL              = "redis://${aws_elasticache_replication_group.main.primary_endpoint_address}:6379"
    AWS_S3_BUCKET_NAME     = aws_s3_bucket.media.bucket
    AWS_S3_REGION          = var.region
    AWS_REGION             = var.region
    ALLOWED_HOSTS          = local.api_host
    CSRF_TRUSTED_ORIGINS   = "https://${local.www_host},https://${local.api_host}"
    CORS_ALLOWED_ORIGINS   = "https://${local.www_host}"
    FRONTEND_URL           = "https://${local.www_host}"
    NOTIFY_WEBHOOK_BASE_URL = "https://${local.api_host}"
    # 14 Nov: which market this regional deployment serves (riders and drivers are routed by it).
    DEPLOY_MARKET          = upper(var.market)
  }
  name  = "${local.ssm_prefix}/backend/${each.key}"
  type  = "String"
  value = each.value
}

resource "aws_ssm_parameter" "web" {
  for_each = {
    NEXT_PUBLIC_SITE_URL     = "https://${local.www_host}"
    NEXT_PUBLIC_API_BASE_URL = "https://${local.api_host}/api/v1"
    NEXT_PUBLIC_WS_BASE_URL  = "wss://${local.api_host}"
  }
  name  = "${local.ssm_prefix}/web/${each.key}"
  type  = "String"
  value = each.value
}

resource "aws_iam_role" "instance" {
  name = "${local.name}-instance"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "cloudwatch" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

resource "aws_iam_role_policy" "instance" {
  name = "${local.name}-instance"
  role = aws_iam_role.instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
        Resource = [for r in aws_ecr_repository.repo : r.arn] },
      { Effect = "Allow", Action = ["ssm:GetParametersByPath", "ssm:GetParameter"],
        Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.me.account_id}:parameter${local.ssm_prefix}/*" },
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"],
        Resource = aws_db_instance.main.master_user_secret[0].secret_arn },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
        Resource = [aws_s3_bucket.media.arn, "${aws_s3_bucket.media.arn}/*"] },
      { Effect = "Allow", Action = ["ses:SendEmail", "ses:SendRawEmail"], Resource = "*" },
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "*" },
    ]
  })
}

resource "aws_iam_instance_profile" "instance" {
  name = "${local.name}-instance"
  role = aws_iam_role.instance.name
}

resource "aws_launch_template" "role" {
  for_each      = local.roles
  name          = "${local.name}-${each.key}"
  image_id      = data.aws_ssm_parameter.al2023.value
  instance_type = each.value.instance_type
  iam_instance_profile {
    arn = aws_iam_instance_profile.instance.arn
  }
  vpc_security_group_ids = [aws_security_group.app.id]
  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size = 30
      volume_type = "gp3"
      encrypted   = true
    }
  }
  user_data = base64encode(templatefile("${path.module}/user_data.sh.tftpl", {
    region     = var.region
    registry   = local.registry
    image_repo = "${local.registry}/saaradhigo/${each.value.image}"
    # The backend runs this deploy's commit; the web reads its tag from SSM at boot, which the web
    # repo's production workflow sets before it refreshes the web instances.
    image_tag  = each.value.image == "web" ? "" : var.image_tag
    role       = each.key
    param_path = each.value.image == "web" ? "${local.ssm_prefix}/web/" : "${local.ssm_prefix}/backend/"
    db_secret  = each.value.image == "web" ? "" : aws_db_instance.main.master_user_secret[0].secret_arn
    port       = each.value.port
    command    = each.value.command
  }))
  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${local.name}-${each.key}", role = each.key }
  }
}

resource "aws_autoscaling_group" "role" {
  for_each            = local.roles
  name                = "${local.name}-${each.key}"
  min_size            = each.value.min
  max_size            = each.value.max
  desired_capacity    = each.value.desired
  vpc_zone_identifier = aws_subnet.private[*].id
  target_group_arns   = each.value.target_groups
  health_check_type   = length(each.value.target_groups) > 0 ? "ELB" : "EC2"
  health_check_grace_period = 180
  launch_template {
    id      = aws_launch_template.role[each.key].id
    version = aws_launch_template.role[each.key].latest_version
  }
  # A new image tag changes the launch template; the refresh replaces instances one at a time.
  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = each.key == "worker" ? 0 : 50
    }
  }
  tag {
    key                 = "Name"
    value               = "${local.name}-${each.key}"
    propagate_at_launch = true
  }
}

resource "aws_autoscaling_policy" "cpu" {
  for_each               = { for k, v in local.roles : k => v if v.max > v.min }
  name                   = "${local.name}-${each.key}-cpu"
  autoscaling_group_name = aws_autoscaling_group.role[each.key].name
  policy_type            = "TargetTrackingScaling"
  target_tracking_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ASGAverageCPUUtilization"
    }
    target_value = 60
  }
}
