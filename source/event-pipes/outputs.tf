output "role_arn" {
  description = "ARN of the IAM role used by EventBridge Pipes"
  value       = aws_iam_role.this.arn
}
