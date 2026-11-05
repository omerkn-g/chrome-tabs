variable "project_id" {
  description = "Google Cloud Project ID hosting the TabFlow ADK Agent"
  type        = string
}

variable "region" {
  description = "Google Cloud region for Cloud Run and Vertex AI Agent Engine"
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run v2 service name"
  type        = string
  default     = "tabflow-work-session-agent"
}

variable "container_image" {
  description = "Artifact Registry container image URI for TabFlow"
  type        = string
  default     = "us-central1-docker.pkg.dev/tabflow-agent-prod/agents/tabflow:latest"
}

variable "gemini_secret_id" {
  description = "Google Cloud Secret Manager ID storing runtime API credentials"
  type        = string
  default     = "tabflow-gemini-api-key"
}

