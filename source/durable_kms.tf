#------------------------------------------------------------------------------
# Dedicated KMS CMK for Lambda Durable Function Encryption
#
# This key encrypts durable execution data (checkpoints, inputs, results)
# for the business-rules durable Lambda function. It is separate from the
# shared infrastructure key (module.kms) per AWS best practice:
# https://docs.aws.amazon.com/lambda/latest/dg/durable-encryption.html
#
# IMPORTANT: After terraform apply, manually attach this key to the durable
# function via Lambda console:
#   Configuration → Durable execution → Edit → Encryption → Use a customer
#   managed key → select the key aliased "durable-function-encryption"
#------------------------------------------------------------------------------

locals {
  durable_function_name = module.business_rules_lambda.name
  durable_function_arn  = "arn:aws:lambda:${var.region}:${data.aws_caller_identity.current.account_id}:function:${local.durable_function_name}"
  durable_role_arn      = module.business_rules_lambda.role_arn

  # Function author defaults to the deployer (iamadmin) if not explicitly set
  function_author_arn = coalesce(
    var.function_author_role_arn,
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:user/iamadmin"
  )

  # Operator role - only included in policy if provided
  durable_operator_arn = var.durable_operator_role_arn
}

module "durable_kms" {
  source      = "terraform-aws-modules/kms/aws"
  version     = "~> 1.0"
  description = "CMK for Lambda durable function execution data encryption"

  enable_key_rotation = true

  aliases                 = ["durable-function-encryption"]
  aliases_use_name_prefix = true

  policy = jsonencode({
    Version = "2012-10-17"
    Id      = "durable-function-key-policy"
    Statement = concat(
      [
        # Statement 1: Enable IAM User Permissions
        # Grants the account root unconditional access to manage the key.
        {
          Sid    = "EnableIAMUserPermissions"
          Effect = "Allow"
          Principal = {
            AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
          }
          Action   = "kms:*"
          Resource = "*"
        },

        # Statement 2: Allow Lambda service to use this key for durable functions
        # Scoped with SourceAccount, SourceArn, and EncryptionContext to prevent
        # confused deputy attacks.
        {
          Sid    = "AllowLambdaServiceDurableFunctions"
          Effect = "Allow"
          Principal = {
            Service = "lambda.amazonaws.com"
          }
          Action = [
            "kms:GenerateDataKey",
            "kms:Decrypt"
          ]
          Resource = "*"
          Condition = {
            StringEquals = {
              "aws:SourceAccount"                            = data.aws_caller_identity.current.account_id
              "aws:SourceArn"                                = local.durable_function_arn
              "kms:EncryptionContext:aws:lambda:FunctionArn" = local.durable_function_arn
            }
          }
        },

        # Statement 3: Allow the function execution role to decrypt durable execution data
        # The execution role needs Decrypt to read state and progress the execution.
        {
          Sid    = "AllowExecutionRoleDecrypt"
          Effect = "Allow"
          Principal = {
            AWS = local.durable_role_arn
          }
          Action   = "kms:Decrypt"
          Resource = "*"
          Condition = {
            StringEquals = {
              "kms:ViaService"                              = "lambda.${var.region}.amazonaws.com"
              "kms:EncryptionContext:aws:lambda:FunctionArn" = local.durable_function_arn
            }
          }
        },

        # Statement 4: Allow the function author to describe this key
        # Lambda validates the key is symmetric and enabled during CreateFunction
        # or UpdateFunctionConfiguration.
        {
          Sid    = "AllowFunctionAuthorDescribeKey"
          Effect = "Allow"
          Principal = {
            AWS = local.function_author_arn
          }
          Action   = "kms:DescribeKey"
          Resource = "*"
          Condition = {
            StringEquals = {
              "kms:ViaService" = "lambda.${var.region}.amazonaws.com"
            }
          }
        },

        # Statement 5: Allow the function author to validate this key for the function
        # Lambda validates key permissions during CreateFunction/UpdateFunctionConfiguration.
        {
          Sid    = "AllowFunctionAuthorValidateKey"
          Effect = "Allow"
          Principal = {
            AWS = local.function_author_arn
          }
          Action = [
            "kms:GenerateDataKey",
            "kms:Decrypt"
          ]
          Resource = "*"
          Condition = {
            StringEquals = {
              "kms:ViaService"                              = "lambda.${var.region}.amazonaws.com"
              "kms:EncryptionContext:aws:lambda:FunctionArn" = local.durable_function_arn
            }
          }
        }
      ],

      # Statement 6 (conditional): Allow durable execution operators
      # Only included if an operator role is specified.
      local.durable_operator_arn != null ? [
        {
          Sid    = "AllowDurableExecutionOperators"
          Effect = "Allow"
          Principal = {
            AWS = local.durable_operator_arn
          }
          Action   = "kms:Decrypt"
          Resource = "*"
          Condition = {
            StringEquals = {
              "kms:ViaService"                              = "lambda.${var.region}.amazonaws.com"
              "kms:EncryptionContext:aws:lambda:FunctionArn" = local.durable_function_arn
            }
          }
        }
      ] : []
    )
  })

  tags = merge(local.tags, {
    Purpose = "DurableFunctionEncryption"
  })
}

#------------------------------------------------------------------------------
# Outputs for manual console configuration
#------------------------------------------------------------------------------

output "durable_kms_key_arn" {
  value       = module.durable_kms.key_arn
  description = "ARN of the CMK to attach to the durable function via console"
}

output "durable_kms_key_alias" {
  value       = "durable-function-encryption"
  description = "Alias of the CMK - search for this in the Lambda console encryption dropdown"
}
