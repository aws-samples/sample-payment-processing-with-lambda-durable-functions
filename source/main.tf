provider "aws" {
  region = var.region
}

provider "random" {}
data "aws_caller_identity" "current" {}

locals {
  tags = {
    Name        = "payments"
    Environment = "PROD"
  }
}


#Part-1::: In this part set up eMock Auth into Dynamo DB table and set up a DB stream
module "mock_lambda" {
  source       = "./lambda_function"
  lambda_name  = "visa-mock"
  project_name = "payments"
  timeout      = 120
  memory_size  = 2048
  policies     = []
}

module "dynamodb" {
  source = "./dynamodb"

}

module "event_bridge" {
  source            = "./event_bridge"
  event_bridge_name = var.event_bridge_name
  kms_key_id        = module.kms.key_arn
  posting_queue_arn = module.posting_queue.arn
  posted_queue_arn  = module.posted_queue.arn
  enrich_lambda_arn = module.enrich_lambda.arn
  business_rules_lambda_arn = module.business_rules_lambda.qualified_arn
  #bucket_name       = "${var.root_bucket_name}-${random_string.this.result}"

}

module "dedup_ddb_table" {
  source = "./dynamodb"
  name   = "transaction_dupcheck_log"
  attributes = [
    {
      name = "key"
      type = "S"
    }
  ]
  hash_key       = "key"
  stream_enabled = false
}

module "dedup_lambda" {
  source       = "./lambda_function"
  lambda_name  = "dedup"
  project_name = "payments"
  timeout      = 120
  memory_size  = 2048
  policies     = []
  environment_variables = {
    WINDOW_DURATION_SECONDS = 300
  }
}

module "event-pipes" {
  source                   = "./event-pipes"
  stream_arn               = module.dynamodb.stream_arn
  eb_arn                   = module.event_bridge.arn
  lambda_arn               = module.dedup_lambda.arn
  kms_key_id               = module.kms.key_arn
  target_event_detail_type = "TransactionAuthorized"
  target_event_source      = "octank.payments.posting.visaIngest"
}

module "business_rules_lambda" {
  source       = "./lambda_function"
  lambda_name  = "business_rules"
  project_name = "payments"
  timeout      = 120
  memory_size  = 2048
  policies = [
    {
      Action = [
        "events:PutEvents",
      ]
      Effect   = "Allow"
      Resource = module.event_bridge.arn
    },
    {
      Action = [
        "sns:Publish",
      ]
      Effect   = "Allow"
      Resource = module.sns.arn
    }
  ]
  environment_variables = {
    EVENT_BUS_NAME = var.event_bridge_name
    SNS_TOPIC_ARN  = module.sns.arn
  }
  durable_config = {
    execution_timeout = 180
    retention_period  = 7
  }
}

module "enrich_lambda" {
  source       = "./lambda_function"
  lambda_name  = "enrich"
  project_name = "payments"
  timeout      = 120
  memory_size  = 2048
  policies = [
    {
      Action = [
        "events:PutEvents",
      ]
      Effect   = "Allow"
      Resource = module.event_bridge.arn
    }
  ]
  environment_variables = {
    EVENT_BUS_NAME = var.event_bridge_name
  }
}

module "posting_lambda" {
  source       = "./lambda_function"
  lambda_name  = "posting"
  project_name = "payments"
  timeout      = 120
  memory_size  = 2048
  policies = [
    {
      Action = [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ]
      Effect   = "Allow"
      Resource = module.posting_queue.arn
    },
    {
      Action = [
        "events:PutEvents",
      ]
      Effect   = "Allow"
      Resource = module.event_bridge.arn
    },
  ]
  event_source_arns = {
    posting_queue = module.posting_queue.arn
  }
  environment_variables = {
    EVENT_BUS_NAME = var.event_bridge_name
  }

}

module "posting_dlq" {
  source       = "./sqs"
  project_name = "Posting"
  is_dlq       = true
  name         = "DLQ.fifo"
}

module "posting_queue" {
  source                = "./sqs"
  project_name          = "Posting"
  name                  = "Queue.fifo"
  dead_letter_queue_arn = module.posting_dlq.arn
  max_receive_count     = 1
  publisher_arns        = [module.event_bridge.posting_rule_arn]
}

module "posted_dlq" {
  source       = "./sqs"
  project_name = "Posted"
  is_dlq       = true
  name         = "PostedDLQ.fifo"
}

module "posted_queue" {
  source                = "./sqs"
  project_name          = "Posted"
  name                  = "PostedQueue.fifo"
  dead_letter_queue_arn = module.posted_dlq.arn
  max_receive_count     = 1
  publisher_arns        = [module.event_bridge.posted_rule_arn]
}

module "sns" {

  source = "./sns"

}

#ForeignTransaction Lambda
module "fx_lambda" {
  source       = "./lambda_function"
  lambda_name  = "fxchecker"
  project_name = "payments"
  timeout      = 120
  memory_size  = 2048
  policies     = []
}

module "kms" {
  source      = "terraform-aws-modules/kms/aws"
  version     = "~> 1.0"
  description = "Securing SFN and EventBridge with KMS Keys"

  # Aliases
  aliases                 = ["realtimepayments"]
  aliases_use_name_prefix = true

  #key_owners = [data.aws_caller_identity.current.arn]
  policy = jsonencode({
    Version = "2012-10-17",
    Id      = "default",
    Statement = [
      {
        Sid    = "Enable IAM User Permissions"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowEventBridgeAndPipesToUseKey",
        Effect = "Allow",
        Principal = {
          Service = ["events.amazonaws.com", "pipes.amazonaws.com"]
        },
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = "*" 
      },
      {
        Sid    = "AllowLambdaRolesToDecrypt",
        Effect = "Allow",
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        },
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey"
        ]
        Resource = "*",
        Condition = {
          StringLike = {
            "aws:PrincipalArn" = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/payments-*-role"
          }
        }
      }
    ]
  })
  
  tags = local.tags
}

resource "random_string" "this" {
  length = 4
  special = false
}

#--------------------------------------------------------------
# Adding guidance solution ID via AWS CloudFormation resource
#--------------------------------------------------------------
resource "aws_cloudformation_stack" "guidance_deployment_metrics" {
    name = "tracking-stack"
    template_body = <<STACK
    {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Guidance for Building Payment Systems Using Event-Driven Architecture (SO9470)",
        "Resources": {
            "EmptyResource": {
                "Type": "AWS::CloudFormation::WaitConditionHandle"
            }
        }
    }
    STACK
}
