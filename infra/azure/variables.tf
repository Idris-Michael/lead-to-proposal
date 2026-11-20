variable "subscription_id" {
  description = "Azure subscription to deploy into."
  type        = string
}

variable "location" {
  description = "Azure region."
  type        = string
  default     = "uksouth"
}

variable "name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "ltp"
}

variable "image" {
  description = "Container image, e.g. myregistry.azurecr.io/lead-to-proposal:1.0.0"
  type        = string
}

variable "operator_ip_ranges" {
  description = "CIDR ranges allowed to reach Key Vault's data plane (deployment pipeline, operators)."
  type        = list(string)
  default     = []
}

variable "worker_replicas" {
  description = "Worker replicas; Postgres SKIP LOCKED lets them run in parallel."
  type        = number
  default     = 2
}

variable "hubspot_mode" {
  description = "mock or live."
  type        = string
  default     = "live"
  validation {
    condition     = contains(["mock", "live"], var.hubspot_mode)
    error_message = "hubspot_mode must be mock or live."
  }
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = { project = "lead-to-proposal", environment = "pilot" }
}
