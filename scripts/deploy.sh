#!/bin/bash

# GCP Telegram Bot Deployment Script
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if required tools are installed
check_dependencies() {
    print_status "Checking dependencies..."
    
    if ! command -v gcloud &> /dev/null; then
        print_error "gcloud CLI is not installed. Please install it first."
        exit 1
    fi
    
    if ! command -v terraform &> /dev/null; then
        print_error "Terraform is not installed. Please install it first."
        exit 1
    fi
    
    print_status "All dependencies are installed."
}

# Get configuration
get_config() {
    if [ -z "$PROJECT_ID" ]; then
        read -p "Enter your GCP Project ID: " PROJECT_ID
    fi
    
    if [ -z "$REGION" ]; then
        REGION="us-central1"
        print_status "Using default region: $REGION"
    fi
    
    export PROJECT_ID
    export REGION
}

# Setup GCP project
setup_gcp() {
    print_status "Setting up GCP project..."
    
    # Set the project
    gcloud config set project $PROJECT_ID
    
    # Enable required APIs
    print_status "Enabling required APIs..."
    gcloud services enable cloudbuild.googleapis.com
    gcloud services enable run.googleapis.com
    gcloud services enable secretmanager.googleapis.com
    gcloud services enable firestore.googleapis.com
    gcloud services enable logging.googleapis.com
    gcloud services enable cloudscheduler.googleapis.com
    gcloud services enable containerregistry.googleapis.com
    
    print_status "GCP project setup complete."
}

# Deploy infrastructure with Terraform
deploy_infrastructure() {
    print_status "Deploying infrastructure with Terraform..."
    
    cd config/terraform
    
    # Check if terraform.tfvars exists
    if [ ! -f "terraform.tfvars" ]; then
        print_error "terraform.tfvars file not found. Please copy from terraform.tfvars.example and fill in your values."
        exit 1
    fi
    
    # Initialize Terraform
    terraform init
    
    # Plan the deployment
    terraform plan
    
    # Apply the changes
    print_status "Applying Terraform configuration..."
    terraform apply -auto-approve
    
    cd ../..
    print_status "Infrastructure deployment complete."
}

# Build and deploy the application
deploy_application() {
    print_status "Building and deploying application..."
    
    # Submit build to Cloud Build
    gcloud builds submit \
        --config=deployment/cloudbuild.yaml \
        --substitutions=_REGION=$REGION \
        .
    
    print_status "Application deployment complete."
}

# Main deployment function
main() {
    print_status "Starting GCP Telegram Bot deployment..."
    
    check_dependencies
    get_config
    setup_gcp
    deploy_infrastructure
    deploy_application
    
    print_status "Deployment complete!"
    print_status "Your Telegram bot is now running on Google Cloud Platform."
    print_warning "Don't forget to set up your Stripe webhook URL in the Stripe dashboard."
}

# Check command line arguments
case ${1:-deploy} in
    "deploy")
        main
        ;;
    "infrastructure")
        check_dependencies
        get_config
        deploy_infrastructure
        ;;
    "app")
        check_dependencies
        get_config
        deploy_application
        ;;
    "help"|"-h"|"--help")
        echo "Usage: $0 [command]"
        echo "Commands:"
        echo "  deploy        - Deploy everything (default)"
        echo "  infrastructure - Deploy only infrastructure"
        echo "  app          - Deploy only application"
        echo "  help         - Show this help message"
        ;;
    *)
        print_error "Unknown command: $1"
        echo "Use '$0 help' for usage information."
        exit 1
        ;;
esac 