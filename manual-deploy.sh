### VARIABLES TO CHANGE - START
# Detached from the team's shared project on purpose — fill in your own
# GCP project/service name here (see PERSONAL_DEPLOY.md). Do not reuse
# daniel-reyes-uprm / study-group-finder; that's the team's live app.
PROJECT_ID=TODO-your-own-gcp-project-id
SERVICE_NAME=TODO-your-own-service-name
### VARIABLES TO CHANGE - END

# ----------- Manual Deployment ------------ #
gcloud config set project ${PROJECT_ID}

if [ $? != 0 ]; then
    echo "'gcloud config set project' failed!"
    exit 1
fi

gcloud builds submit --tag gcr.io/${PROJECT_ID}/${SERVICE_NAME}

if [ $? != 0 ]; then
    echo "'gcloud builds' failed!"
    exit 1
fi

gcloud run deploy ${SERVICE_NAME} \
    --image gcr.io/${PROJECT_ID}/${SERVICE_NAME}:latest \
    --region us-central1 \
    --allow-unauthenticated

if [ $? != 0 ]; then
    echo "'gcloud run deploy' failed!"
fi
