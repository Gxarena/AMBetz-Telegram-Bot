# Manual Setup vs Automated Setup Comparison

## Without Automation (Manual Setup) - 🤯
You'd need to run these commands manually:

```bash
# 1. Enable APIs (7 commands)
gcloud services enable firestore.googleapis.com
gcloud services enable secretmanager.googleapis.com
gcloud services enable logging.googleapis.com
gcloud services enable cloudbuild.googleapis.com
gcloud services enable run.googleapis.com
gcloud services enable cloudscheduler.googleapis.com
gcloud services enable containerregistry.googleapis.com

# 2. Create Firestore database
gcloud firestore databases create --region=us-central1

# 3. Create secrets (6 commands)
echo "your-bot-token" | gcloud secrets create telegram-bot-token --data-file=-
echo "your-stripe-key" | gcloud secrets create stripe-secret-key --data-file=-
echo "your-stripe-pub-key" | gcloud secrets create stripe-publishable-key --data-file=-
echo "your-webhook-secret" | gcloud secrets create stripe-webhook-secret --data-file=-
echo "your-price-id" | gcloud secrets create stripe-price-id --data-file=-
echo "your-chat-id" | gcloud secrets create vip-chat-id --data-file=-

# 4. Create service account
gcloud iam service-accounts create telegram-bot-runner

# 5. Grant permissions (3 commands)
gcloud projects add-iam-policy-binding YOUR-PROJECT \
    --member="serviceAccount:telegram-bot-runner@YOUR-PROJECT.iam.gserviceaccount.com" \
    --role="roles/datastore.user"
    
gcloud projects add-iam-policy-binding YOUR-PROJECT \
    --member="serviceAccount:telegram-bot-runner@YOUR-PROJECT.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
    
gcloud projects add-iam-policy-binding YOUR-PROJECT \
    --member="serviceAccount:telegram-bot-runner@YOUR-PROJECT.iam.gserviceaccount.com" \
    --role="roles/logging.logWriter"

# 6. Build and deploy
gcloud builds submit --tag gcr.io/YOUR-PROJECT/telegram-bot .
gcloud run deploy telegram-bot \
    --image gcr.io/YOUR-PROJECT/telegram-bot \
    --region us-central1 \
    --service-account telegram-bot-runner@YOUR-PROJECT.iam.gserviceaccount.com \
    --set-env-vars GOOGLE_CLOUD_PROJECT=YOUR-PROJECT

# 7. Create scheduler job
gcloud scheduler jobs create http check-expired \
    --schedule="0 */6 * * *" \
    --uri="https://your-service-url/check-expired" \
    --http-method=POST \
    --oidc-service-account-email=telegram-bot-runner@YOUR-PROJECT.iam.gserviceaccount.com

# That's 20+ commands and you need to remember the exact order!
```

## With Automation - 😎
```bash
# 1. Configure your secrets in terraform.tfvars
# 2. Run one command
./scripts/deploy.sh

# Done! ✨
```

## Benefits of Automation

| Manual Setup | Automated Setup |
|-------------|-----------------|
| 20+ commands | 1 command |
| Error-prone | Tested & reliable |
| Hard to repeat | Perfectly reproducible |
| No documentation | Self-documenting code |
| Manual cleanup | `terraform destroy` |
| Forget dependencies | All dependencies handled | 