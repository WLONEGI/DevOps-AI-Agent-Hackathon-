#!/usr/bin/env bash
# Deploy to Google Cloud Run with maximum request timeout (60 minutes / 3600 seconds)
set -euo pipefail

# Load environment variables if available
ENV_FILE="backend/.env"
if [ -f "$ENV_FILE" ]; then
  echo "Loading environment variables from $ENV_FILE"
  PROJECT_ID=$(grep -E "^GOOGLE_CLOUD_PROJECT=" "$ENV_FILE" | cut -d'=' -f2- | tr -d '"'\''')
  REGION=$(grep -E "^GOOGLE_CLOUD_LOCATION=" "$ENV_FILE" | cut -d'=' -f2- | tr -d '"'\''')
else
  PROJECT_ID=""
  REGION="us-central1"
fi

PROJECT_ID=${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo "")}
SERVICE_NAME="smart-glasses-gateway"
IMAGE_NAME="gcr.io/${PROJECT_ID}/${SERVICE_NAME}:latest"

if [ -z "$PROJECT_ID" ]; then
  echo "❌ Error: GOOGLE_CLOUD_PROJECT is not set in $ENV_FILE and no default project is set in gcloud."
  exit 1
fi

echo "=== Cloud Run Deployment Configuration ==="
echo "Project ID:  $PROJECT_ID"
echo "Region:      $REGION"
echo "Service:     $SERVICE_NAME"
echo "Image:       $IMAGE_NAME"
echo "Timeout:     3600s (60 minutes - Cloud Run Maximum)"
echo "=========================================="

echo "1. Building and pushing Docker image using Cloud Builds..."
gcloud builds submit --project="$PROJECT_ID" --tag="$IMAGE_NAME" .

echo "2. Deploying to Cloud Run with maximum timeout (3600s)..."
gcloud run deploy "$SERVICE_NAME" \
  --project="$PROJECT_ID" \
  --image="$IMAGE_NAME" \
  --region="$REGION" \
  --platform="managed" \
  --timeout=3600 \
  --allow-unauthenticated

echo "✅ Cloud Run deployment completed successfully with maximum timeout!"
