# Complete GCP Setup Guide 🚀

## Prerequisites Checklist ✅

Before we start, make sure you have:
- [ ] Gmail account (for GCP)
- [ ] Credit card (for GCP billing - but we'll stay in free tier)
- [ ] Telegram bot token (from @BotFather)
- [ ] Stripe account with products set up

## Step 1: Create GCP Account & Project

### 1.1 Sign up for Google Cloud
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Sign in with your Gmail account
3. Accept the terms and complete setup
4. **Important**: Google gives you $300 free credits for 90 days!

### 1.2 Create a New Project
1. Click the project dropdown (top left, next to "Google Cloud")
2. Click "New Project"
3. Enter project name: `telegram-bot-project` (or whatever you prefer)
4. Note the **Project ID** (usually `telegram-bot-project-123456`)
5. Click "Create"

### 1.3 Enable Billing
1. Go to "Billing" in the left menu
2. Link a billing account (required even for free tier)
3. Don't worry - we'll set up alerts to avoid charges!

## Step 2: Install Required Tools

### 2.1 Install Google Cloud CLI

**macOS:**
```bash
# Install via Homebrew
brew install --cask google-cloud-sdk

# Or download from: https://cloud.google.com/sdk/docs/install
```

**Windows:**
```bash
# Download installer from: https://cloud.google.com/sdk/docs/install
# Run GoogleCloudSDKInstaller.exe
```

**Linux:**
```bash
# Download and install
curl https://sdk.cloud.google.com | bash
exec -l $SHELL
```

### 2.2 Install Terraform

**macOS:**
```bash
brew install terraform
```

**Windows:**
```bash
# Download from: https://www.terraform.io/downloads
# Add to PATH
```

**Linux:**
```bash
wget https://releases.hashicorp.com/terraform/1.6.0/terraform_1.6.0_linux_amd64.zip
unzip terraform_1.6.0_linux_amd64.zip
sudo mv terraform /usr/local/bin/
```

### 2.3 Verify Installation
```bash
gcloud --version
terraform --version
```

## Step 3: Authenticate with GCP

### 3.1 Login to gcloud
```bash
gcloud auth login
```
This opens your browser - sign in with the same Gmail account.

### 3.2 Set Your Project
```bash
# Replace YOUR-PROJECT-ID with your actual project ID
gcloud config set project YOUR-PROJECT-ID

# Verify it's set correctly
gcloud config get-value project
```

### 3.3 Enable Application Default Credentials
```bash
gcloud auth application-default login
```

## Step 4: Gather Your Credentials

### 4.1 Telegram Bot Token
1. Open Telegram and search for `@BotFather`
2. Type `/newbot`
3. Follow instructions to create your bot
4. Save the token (looks like: `123456789:ABCdefGHIjklMNOpqrSTUvwxyz`)

### 4.2 Stripe Credentials
1. Go to [Stripe Dashboard](https://dashboard.stripe.com/)
2. Go to "Developers" → "API Keys"
3. Copy:
   - **Publishable key** (starts with `pk_`)
   - **Secret key** (starts with `sk_`)
4. Create a product/price and note the **Price ID** (starts with `price_`)

### 4.3 Telegram Group Chat ID (Optional)
If you want VIP group management:
1. Add your bot to a Telegram group
2. Send a message in the group
3. Visit: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
4. Look for the chat ID (usually negative number like `-1001234567890`)

## Step 5: Configure the Project

### 5.1 Clone the Bot Code
```bash
cd ~/Documents  # or wherever you want the project
git clone <your-repo-url>
cd gcp-telegram-bot
```

### 5.2 Set Up Configuration
```bash
# Copy the example configuration
cp config/terraform/terraform.tfvars.example config/terraform/terraform.tfvars

# Edit with your values
nano config/terraform/terraform.tfvars  # or use any text editor
```

**Fill in your terraform.tfvars:**
```hcl
# Your GCP project ID (from Step 1.2)
project_id = "telegram-bot-project-123456"
region     = "us-central1"

# Your bot token (from Step 4.1)
telegram_bot_token = "123456789:ABCdefGHIjklMNOpqrSTUvwxyz"

# Your Stripe credentials (from Step 4.2)
stripe_secret_key      = "sk_test_51..."
stripe_publishable_key = "pk_test_51..."
stripe_webhook_secret  = "whsec_..."  # We'll get this later
stripe_price_id        = "price_..."

# Optional: Your group chat ID (from Step 4.3)
vip_chat_id = "-1001234567890"
```

## Step 6: Deploy! 🚀

### 6.1 Make Script Executable
```bash
chmod +x scripts/deploy.sh
```

### 6.2 Run Deployment
```bash
./scripts/deploy.sh
```

**What happens:**
- ✅ Checks dependencies
- ✅ Enables GCP APIs
- ✅ Creates Firestore database
- ✅ Stores secrets in Secret Manager
- ✅ Creates service accounts with permissions
- ✅ Builds and deploys to Cloud Run
- ✅ Sets up automated jobs

**Expected output:**
```
[INFO] Checking dependencies...
[INFO] All dependencies are installed.
[INFO] Setting up GCP project...
[INFO] Enabling required APIs...
[INFO] Deploying infrastructure with Terraform...
[INFO] Building and deploying application...
[INFO] Deployment complete!
[INFO] Your Telegram bot is now running on Google Cloud Platform.
```

### 6.3 Get Your Cloud Run URL
```bash
# Get your service URL
gcloud run services describe telegram-bot --region=us-central1 --format="value(status.url)"
```

## Step 7: Configure Stripe Webhook

### 7.1 Set Up Webhook in Stripe
1. Go to [Stripe Dashboard](https://dashboard.stripe.com/) → "Developers" → "Webhooks"
2. Click "Add endpoint"
3. Enter URL: `https://your-cloud-run-url/stripe-webhook`
4. Select events: `checkout.session.completed` and `invoice.payment_failed`
5. Click "Add endpoint"

### 7.2 Update Webhook Secret
1. Copy the webhook signing secret from Stripe
2. Update your terraform.tfvars:
   ```hcl
   stripe_webhook_secret = "whsec_your_new_webhook_secret"
   ```
3. Re-run deployment:
   ```bash
   ./scripts/deploy.sh
   ```

## Step 8: Test Your Bot! 🎉

### 8.1 Find Your Bot
1. Open Telegram
2. Search for your bot's username
3. Start a conversation

### 8.2 Test Commands
```
/start    - Should show welcome message with Subscribe button
/status   - Should show subscription status
/help     - Should show help message
/test     - Should create test subscription
```

### 8.3 Test Payment Flow
1. Click "Subscribe" button
2. Click "Pay Now" 
3. Complete payment in Stripe
4. Check `/status` - should show active subscription

## Step 9: Monitor & Maintain

### 9.1 View Logs
```bash
# View recent logs
gcloud logging read "resource.type=cloud_run_revision" --limit=20

# Or use the web console:
# https://console.cloud.google.com/logs
```

### 9.2 Check Costs
```bash
# View current billing
gcloud billing budgets list
```

**Or visit:** [GCP Billing](https://console.cloud.google.com/billing)

### 9.3 Update the Bot
```bash
# Make code changes, then redeploy
./scripts/deploy.sh app
```

## Troubleshooting 🔧

### Common Issues:

**"Permission denied" errors:**
```bash
# Re-authenticate
gcloud auth login
gcloud auth application-default login
```

**"Project not found" errors:**
```bash
# Verify project ID
gcloud projects list
gcloud config set project YOUR-CORRECT-PROJECT-ID
```

**"APIs not enabled" errors:**
```bash
# The deploy script should handle this, but if needed:
gcloud services enable firestore.googleapis.com
gcloud services enable run.googleapis.com
# ... etc
```

**Terraform errors:**
```bash
cd config/terraform
terraform init
terraform plan  # Check for issues
```

## Cost Management 💰

### Set Up Billing Alerts
1. Go to [GCP Billing](https://console.cloud.google.com/billing)
2. Click "Budgets & alerts"
3. Create budget for $10/month with alerts at 50%, 90%, 100%

### Expected Costs (Monthly)
- **Free tier usage**: $0-1
- **Light usage** (< 1000 users): $1-5
- **Medium usage** (< 10,000 users): $5-15

## Security Best Practices 🔒

1. **Never commit secrets** - they're in terraform.tfvars (git-ignored)
2. **Rotate secrets regularly** - update in Secret Manager
3. **Monitor access logs** - check Cloud Logging
4. **Use least privilege** - our Terraform does this automatically
5. **Enable 2FA** - on your Google account

## Next Steps 🎯

1. **Customize the bot** - edit `src/gcp_bot.py`
2. **Add features** - modify commands, add new functionality  
3. **Scale up** - increase Cloud Run instances if needed
4. **Monitor usage** - set up Cloud Monitoring alerts
5. **Backup data** - Firestore has automatic backups

## Getting Help 🆘

- **GCP Issues**: [GCP Support](https://cloud.google.com/support)
- **Telegram Bot API**: [Bot API Docs](https://core.telegram.org/bots/api)
- **Stripe Integration**: [Stripe Docs](https://stripe.com/docs)
- **This Project**: Check the main README.md or open an issue

---

**You're all set! 🎉 Your Telegram bot is now running on enterprise-grade Google Cloud infrastructure!** 