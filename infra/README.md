# Infrastructure (deploy-ready, not deployed)

Two equivalent targets for the same image. Both are checked in CI with `terraform validate`,
tflint and checkov; neither has been applied. Every checkov skip carries its reason inline.

| | Azure (`azure/`) | AWS (`aws/`) |
|---|---|---|
| Compute | Container Apps: `api` (external ingress), `worker` (no ingress) | ECS Fargate services: `api` behind an HTTPS ALB, `worker` |
| Database | Postgres Flexible Server, VNet-injected, no public endpoint | RDS Postgres in private subnets, KMS-encrypted |
| Secrets | Key Vault (RBAC), read by a user-assigned identity | Secrets Manager under a customer-managed KMS key |
| Network | New VNet, NSGs, private DNS zone | Existing VPC passed in (`vpc_id`, subnet ids) |
| Logs | Log Analytics | CloudWatch Logs, 365 days, KMS |

Secret values (webhook secret, operator token, HubSpot token, proxy token) are placeholders in
Terraform and are set out of band after apply, so they never sit in state. The database URL is
generated and stored by Terraform.

Run migrations once per release before rolling the services:
`alembic upgrade head` as a one-off task / job with the same image and `DATABASE_URL`.

The model path in both is `LLM_MODE=proxy`; for production, add an adapter for the Anthropic API,
Bedrock or Azure AI Foundry (ADR 5) and the proxy is no longer needed.
