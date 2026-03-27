output "arn" {
  value       = aws_lambda_function.this.arn
  description = "Lambda function ARN"
}

output "qualified_arn" {
  value       = local.invocation_arn
  description = "Alias ARN for durable functions, unqualified ARN otherwise. Use this for invocations."
}

output "name" {
  value       = local.function_name
  description = "Lambda function name"
}