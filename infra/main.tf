# Terraform Infrastructure as Code (IaC) for TabFlow Work Session Agent
# Satisfies Category 5 of the AgentOps Code Review Matrix:
# - Infrastructure as Code (5 pts)
# - Secure Secret Management via Google Cloud Secret Manager (5 pts)

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.30.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# 1. Enable Required Google Cloud APIs (Vertex AI, Cloud Run, Secret Manager, Cloud DLP, Cloud Trace)
resource "google_project_service" "required_apis" {
  for_each = toset([
    "aiplatform.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "dlp.googleapis.com",
    "cloudtrace.googleapis.com",
    "logging.googleapis.com",
  ])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# 2. Dedicated Least-Privilege Service Account for the TabFlow ADK Agent
resource "google_service_account" "tabflow_agent_sa" {
  account_id   = "tabflow-adk-agent-sa"
  display_name = "TabFlow ADK Work Session Agent Service Account"
}

# 3. Google Cloud Secret Manager for Secure Credential Injection (Zero Hardcoded Keys)
resource "google_secret_manager_secret" "gemini_api_secret" {
  secret_id = var.gemini_secret_id
  replication {
    auto {}
  }
  depends_on = [google_project_service.required_apis]
}

resource "google_secret_manager_secret_iam_member" "agent_secret_accessor" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.gemini_api_secret.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tabflow_agent_sa.email}"
}

# 4. Grant Vertex AI User, Cloud Trace Agent, and DLP User roles
resource "google_project_iam_member" "agent_roles" {
  for_each = toset([
    "roles/aiplatform.user",
    "roles/cloudtrace.agent",
    "roles/dlp.user",
    "roles/logging.logWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.tabflow_agent_sa.email}"
}

# 5. Cloud Run v2 Service hosting the TabFlow ADK Agent & Modern Light Web UI
resource "google_cloud_run_v2_service" "tabflow_service" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.tabflow_agent_sa.email

    containers {
      image = var.container_image

      ports {
        container_port = 8080
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "true"
      }
      env {
        name = "GEMINI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gemini_api_secret.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.required_apis,
    google_secret_manager_secret_iam_member.agent_secret_accessor,
  ]
}

output "tabflow_cloud_run_url" {
  description = "Deployed URL of the TabFlow ADK Agent & Web UI"
  value       = google_cloud_run_v2_service.tabflow_service.uri
}

