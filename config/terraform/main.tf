terraform {
  required_version = ">= 1.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

# Configure the Google Cloud Provider
provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# Variables
variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP Region"
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP Zone"
  type        = string
  default     = "us-central1-a"
}

variable "telegram_bot_token" {
  description = "Telegram Bot Token"
  type        = string
  sensitive   = true
}

variable "stripe_secret_key" {
  description = "Stripe Secret Key"
  type        = string
  sensitive   = true
  default     = ""
}

variable "stripe_publishable_key" {
  description = "Stripe Publishable Key"
  type        = string
  sensitive   = true
  default     = ""
}

variable "stripe_webhook_secret" {
  description = "Stripe Webhook Secret"
  type        = string
  sensitive   = true
  default     = ""
}

variable "stripe_price_id" {
  description = "Stripe Price ID"
  type        = string
  default     = ""
}

variable "vip_chat_id" {
  description = "VIP Chat ID (optional)"
  type        = string
  default     = ""
}

# Enable required APIs
resource "google_project_service" "required_apis" {
  for_each = toset([
    "firestore.googleapis.com",
    "secretmanager.googleapis.com",
    "logging.googleapis.com",
    "cloudbuild.googleapis.com",
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "containerregistry.googleapis.com"
  ])
  
  project = var.project_id
  service = each.value
  
  disable_dependent_services = false
  disable_on_destroy        = false
}

# Firestore Database
resource "google_firestore_database" "database" {
  project     = var.project_id
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  depends_on = [google_project_service.required_apis]
}

# Secret Manager secrets
resource "google_secret_manager_secret" "telegram_bot_token" {
  secret_id = "telegram-bot-token"
  
  replication {
    auto {}
  }
  
  depends_on = [google_project_service.required_apis]
}

resource "google_secret_manager_secret_version" "telegram_bot_token" {
  secret      = google_secret_manager_secret.telegram_bot_token.id
  secret_data = var.telegram_bot_token
}

resource "google_secret_manager_secret" "stripe_secret_key" {
  count = var.stripe_secret_key != "" ? 1 : 0
  
  secret_id = "stripe-secret-key"
  
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "stripe_secret_key" {
  count = var.stripe_secret_key != "" ? 1 : 0
  
  secret      = google_secret_manager_secret.stripe_secret_key[0].id
  secret_data = var.stripe_secret_key
}

resource "google_secret_manager_secret" "stripe_publishable_key" {
  count = var.stripe_publishable_key != "" ? 1 : 0
  
  secret_id = "stripe-publishable-key"
  
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "stripe_publishable_key" {
  count = var.stripe_publishable_key != "" ? 1 : 0
  
  secret      = google_secret_manager_secret.stripe_publishable_key[0].id
  secret_data = var.stripe_publishable_key
}

resource "google_secret_manager_secret" "stripe_webhook_secret" {
  count = var.stripe_webhook_secret != "" ? 1 : 0
  
  secret_id = "stripe-webhook-secret"
  
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "stripe_webhook_secret" {
  count = var.stripe_webhook_secret != "" ? 1 : 0
  
  secret      = google_secret_manager_secret.stripe_webhook_secret[0].id
  secret_data = var.stripe_webhook_secret
}

resource "google_secret_manager_secret" "stripe_price_id" {
  count = var.stripe_price_id != "" ? 1 : 0
  
  secret_id = "stripe-price-id"
  
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "stripe_price_id" {
  count = var.stripe_price_id != "" ? 1 : 0
  
  secret      = google_secret_manager_secret.stripe_price_id[0].id
  secret_data = var.stripe_price_id
}

resource "google_secret_manager_secret" "vip_chat_id" {
  count = var.vip_chat_id != "" ? 1 : 0
  
  secret_id = "vip-chat-id"
  
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "vip_chat_id" {
  count = var.vip_chat_id != "" ? 1 : 0
  
  secret      = google_secret_manager_secret.vip_chat_id[0].id
  secret_data = var.vip_chat_id
}

# Service account for Cloud Run
resource "google_service_account" "cloud_run_sa" {
  account_id   = "telegram-bot-runner"
  display_name = "Telegram Bot Cloud Run Service Account"
}

# IAM permissions for service account
resource "google_project_iam_member" "firestore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.cloud_run_sa.email}"
}

resource "google_project_iam_member" "secret_manager_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.cloud_run_sa.email}"
}

resource "google_project_iam_member" "logging_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.cloud_run_sa.email}"
}

# Cloud Scheduler job for expired subscription checks
resource "google_cloud_scheduler_job" "expired_subscriptions_check" {
  name      = "check-expired-subscriptions"
  region    = var.region
  schedule  = "0 */6 * * *"  # Every 6 hours
  time_zone = "UTC"

  http_target {
    http_method = "POST"
    uri         = "https://telegram-bot-${random_id.suffix.hex}-uc.a.run.app/check-expired"
    
    oidc_token {
      service_account_email = google_service_account.cloud_run_sa.email
    }
  }
  
  depends_on = [google_project_service.required_apis]
}

resource "random_id" "suffix" {
  byte_length = 4
}

# Outputs
output "project_id" {
  description = "GCP Project ID"
  value       = var.project_id
}

output "firestore_database" {
  description = "Firestore Database ID"
  value       = google_firestore_database.database.name
}

output "service_account_email" {
  description = "Service Account Email"
  value       = google_service_account.cloud_run_sa.email
} 