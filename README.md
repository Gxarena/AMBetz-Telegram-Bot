# GCP Telegram Bot

A Telegram subscription bot built with Google Cloud Platform services. This bot manages user subscriptions with Stripe payment integration, using Firestore for data storage and Cloud Run for hosting.

## 🏗️ Architecture

This bot is built entirely using GCP services:

- **Cloud Run**: Hosts the Telegram bot application
- **Firestore**: NoSQL database for user and subscription data
- **Secret Manager**: Secure storage for API keys and secrets
- **Cloud Scheduler**: Automated expired subscription checks
- **Cloud Build**: CI/CD for automated deployments
- **Cloud Logging**: Centralized logging and monitoring
- **Cloud Storage**: Container image storage

## ✨ Features

- **Subscription Management**: Create, track, and manage user subscriptions
- **Stripe Integration**: Secure payment processing with webhook support
- **VIP Group Management**: Automatically add/remove users from Telegram groups
- **Expired Subscription Handling**: Automated cleanup of expired subscriptions
- **Cloud-Native**: Fully managed infrastructure with auto-scaling
- **Secure**: All secrets managed through Google Secret Manager
- **Monitoring**: Comprehensive logging with Cloud Logging

## 🚀 Quick Start

### Prerequisites

1. **GCP Project**: Create a new Google Cloud Platform project
2. **Billing**: Enable billing on your GCP project
3. **Telegram Bot**: Create a bot with [@BotFather](https://t.me/botfather)
4. **Stripe Account**: Set up a Stripe account with products/prices configured
5. **Tools**: Install `gcloud`, `terraform`, and `git`

### 1. Clone and Setup

```bash
git clone <your-repo>
cd gcp-telegram-bot

# Make deployment script executable
chmod +x scripts/deploy.sh
```

### 2. Configure Secrets

Copy the example Terraform variables:

```bash
cp config/terraform/terraform.tfvars.example config/terraform/terraform.tfvars
```

Edit `config/terraform/terraform.tfvars` with your values:

```hcl
# GCP Configuration
project_id = "your-gcp-project-id"
region     = "us-central1"

# Telegram Configuration
telegram_bot_token = "your-telegram-bot-token"

# Stripe Configuration
stripe_secret_key      = "sk_live_or_test_your_stripe_secret_key"
stripe_publishable_key = "pk_live_or_test_your_stripe_publishable_key"
stripe_webhook_secret  = "whsec_your_stripe_webhook_secret"
stripe_price_id        = "price_your_stripe_price_id"

# Optional: VIP Chat ID for group management
vip_chat_id = "your-telegram-group-chat-id"
```

### 3. Deploy

Run the deployment script:

```bash
./scripts/deploy.sh
```

This will:
- Enable required GCP APIs
- Deploy infrastructure with Terraform
- Build and deploy the application to Cloud Run
- Set up Cloud Scheduler for automated tasks

### 4. Configure Stripe Webhook

After deployment, configure your Stripe webhook:

1. Go to your Stripe Dashboard → Webhooks
2. Add endpoint: `https://your-cloud-run-url/stripe-webhook`
3. Select events: `checkout.session.completed`, `invoice.payment_failed`
4. Copy the webhook secret to your terraform.tfvars

## 📁 Project Structure

```
gcp-telegram-bot/
├── src/                          # Source code
│   ├── gcp_bot.py               # Main bot application
│   ├── firestore_service.py     # Firestore database operations
│   ├── gcp_stripe_service.py    # Stripe integration with Secret Manager
│   └── webhook_handler.py       # FastAPI webhook handler
├── config/
│   └── terraform/               # Infrastructure as Code
│       ├── main.tf              # Terraform configuration
│       ├── terraform.tfvars.example
│       └── terraform.tfvars     # Your actual config (git-ignored)
├── deployment/
│   ├── Dockerfile               # Container configuration
│   └── cloudbuild.yaml         # Cloud Build CI/CD
├── scripts/
│   └── deploy.sh               # Deployment automation script
└── requirements.txt            # Python dependencies
```

## 🔧 Configuration

### Environment Variables

The bot uses the following environment variables (managed automatically):

- `GOOGLE_CLOUD_PROJECT`: Your GCP project ID
- Secrets are stored in Secret Manager, not environment variables

### Secret Manager Secrets

All sensitive data is stored in Secret Manager:

- `telegram-bot-token`: Your Telegram bot token
- `stripe-secret-key`: Stripe secret key
- `stripe-publishable-key`: Stripe publishable key
- `stripe-webhook-secret`: Stripe webhook secret
- `stripe-price-id`: Stripe price ID for subscriptions
- `vip-chat-id`: (Optional) Telegram group chat ID

## 🤖 Bot Commands

- `/start` - Welcome message and subscription button
- `/status` - Check current subscription status
- `/help` - Show available commands
- `/test` - Create test subscription (development only)

## 🔒 Security Features

- **Secret Manager**: All API keys stored securely
- **IAM**: Least-privilege service account permissions
- **Webhook Verification**: Stripe webhook signature validation
- **Cloud Logging**: Audit trail for all operations
- **No Hardcoded Secrets**: All sensitive data managed externally

## 📊 Monitoring

### Cloud Logging

View logs in Google Cloud Console:

```bash
gcloud logging read "resource.type=cloud_run_revision" --limit 50
```

### Health Checks

The webhook handler includes health endpoints:

- `GET /health` - Application health check
- `POST /check-expired` - Manual expired subscription check

## 🔄 Deployment Options

### Full Deployment

Deploy everything (infrastructure + application):

```bash
./scripts/deploy.sh deploy
```

### Infrastructure Only

Deploy only the infrastructure:

```bash
./scripts/deploy.sh infrastructure
```

### Application Only

Deploy only the application code:

```bash
./scripts/deploy.sh app
```

## 🛠️ Development

### Local Development

For local development, you can use environment variables instead of Secret Manager:

```bash
export GOOGLE_CLOUD_PROJECT="your-project-id"
export TELEGRAM_BOT_TOKEN="your-token"
export STRIPE_SECRET_KEY="your-stripe-key"
# ... other variables

python src/gcp_bot.py
```

### Testing Webhooks Locally

Use ngrok to test webhooks locally:

```bash
ngrok http 8080
# Update Stripe webhook URL to your ngrok URL
python src/webhook_handler.py
```

## 📈 Scaling

The bot automatically scales with Cloud Run:

- **Auto-scaling**: 0 to 10 instances based on demand
- **Cold Start**: Minimal cold start time with optimized container
- **Cost Effective**: Pay only for actual usage

## 🔧 Troubleshooting

### Common Issues

1. **Permission Errors**: Ensure service account has required IAM roles
2. **Secret Access**: Check Secret Manager permissions
3. **Firestore Errors**: Verify Firestore is enabled and configured
4. **Webhook Issues**: Validate Stripe webhook signature

### Debug Commands

```bash
# Check service status
gcloud run services describe telegram-bot --region=us-central1

# View recent logs
gcloud logging read "resource.type=cloud_run_revision" --limit 10

# Test secrets access
gcloud secrets access-versions latest --secret="telegram-bot-token"
```

## 💰 Cost Estimation

Typical monthly costs for light usage:

- **Cloud Run**: $0-5 (free tier covers most small bots)
- **Firestore**: $0-1 (free tier: 50K reads, 20K writes)
- **Secret Manager**: $0.06 per secret per month
- **Cloud Scheduler**: $0.10 per job per month
- **Cloud Build**: $0.003 per build minute (120 free minutes/day)

**Total**: ~$1-10/month for small to medium usage

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 🆘 Support

For support and questions:

1. Check the troubleshooting section
2. Review Cloud Logging for errors
3. Open an issue in the repository

## 🔗 Related Documentation

- [Cloud Run Documentation](https://cloud.google.com/run/docs)
- [Firestore Documentation](https://cloud.google.com/firestore/docs)
- [Secret Manager Documentation](https://cloud.google.com/secret-manager/docs)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Stripe API Documentation](https://stripe.com/docs/api) 