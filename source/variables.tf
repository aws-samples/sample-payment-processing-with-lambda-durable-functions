variable "region" {
  type        = string
  default     = "eu-west-1"
  description = "the region of deployment"
}

variable "event_rule_name" {
  type        = string
  description = "Name of the Event Rule"
  default     = "ec2-running"
}

variable "stream_arn" {
  type        = string
  default     = ""
  description = "The DYnaamoDB Stream ARN"
}

variable "event_bridge_name" {
  type        = string
  description = "Name of the Event Bus"
  default     = "payments"
}

variable "lambda_arn" {
  type        = string
  description = "ARN of the Lambda"
  default     = ""
}

variable "function_author_role_arn" {
  type        = string
  description = "ARN of the IAM role/user that creates or updates the durable Lambda function (function author). Defaults to iamadmin user."
  default     = null
}

variable "durable_operator_role_arn" {
  type        = string
  description = "ARN of the IAM role for durable execution operators. Set to null to omit the operator policy statement."
  default     = null
}