### VARIABLES TO CHANGE - START
# Detached from the team's shared project/repo on purpose. Fill in your own
# GCP project + your own fork's owner/name (see PERSONAL_DEPLOY.md) if you
# want GitHub Actions auto-deploy on your fork. Do not reuse these — they
# belong to the team's live app and its WIF trust is scoped to the
# CodePath-Tech-Exchange-CTEx org repo specifically (a fork's OIDC token
# has a different repository_owner claim, so it couldn't satisfy that trust
# policy even by accident — but there's no reason to point at it anyway).
PROJECT_ID=TODO-your-own-gcp-project-id
PROJECT_NUMBER=TODO-your-own-gcp-project-number
SERVICE_NAME=TODO-your-own-service-name
GITHUB_ORG=TODO-your-own-github-username-or-org
GITHUB_REPO=code-and-conquer
### VARIABLES TO CHANGE - END

# ----------- Set Up GCP Project ----------- #
gcloud config set project ${PROJECT_ID}

if [ $? != 0 ]; then
    echo "setting GCP project failed! did you set the PROJECT_ID bash variable?"
    exit 1
fi

gcloud services enable run.googleapis.com \
    cloudbuild.googleapis.com

if [ $? != 0 ]; then
    echo "enabling cloud build & cloud run failed!"
    exit 1
fi

gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member=serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com \
    --role=roles/run.builder

if [ $? != 0 ]; then
    echo "adding an iam policy binding failed!"
    exit 1
fi

gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member=serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com \
    --role=roles/cloudbuild.builds.builder

if [ $? != 0 ]; then
    echo "adding an iam policy binding failed!"
    exit 1
fi

# ---- Configure GCP for GitHub Actions ---- #
OIDC_NAME=github-project-repo
WORKLOAD_IDENTITY_POOL_NAME=github-provider
WORKLOAD_IDENTITY_POOL_ID="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WORKLOAD_IDENTITY_POOL_NAME}"
WORKLOAD_IDENTITY_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WORKLOAD_IDENTITY_POOL_NAME}/providers/${OIDC_NAME}"

gcloud iam workload-identity-pools create "${WORKLOAD_IDENTITY_POOL_NAME}" \
    --project="${PROJECT_ID}" \
    --location="global" \
    --display-name="GitHub Actions Pool"

if [ $? != 0 ]; then
    echo "creating a workload identity pool failed!"
    exit 1
fi

gcloud iam workload-identity-pools providers create-oidc "${OIDC_NAME}" \
    --project="${PROJECT_ID}" \
    --location="global" \
    --workload-identity-pool="${WORKLOAD_IDENTITY_POOL_NAME}" \
    --display-name="My GitHub repo Provider" \
    --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
    --attribute-condition="assertion.repository_owner == '${GITHUB_ORG}'" \
    --issuer-uri="https://token.actions.githubusercontent.com"

if [ $? != 0 ]; then
    echo "creating an oidc failed!"
    exit 1
fi

gcloud iam service-accounts add-iam-policy-binding "${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --project="${PROJECT_ID}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/${WORKLOAD_IDENTITY_POOL_ID}/attribute.repository/${GITHUB_ORG}/${GITHUB_REPO}"

if [ $? != 0 ]; then
    echo "adding an iam policy binding failed!"
    exit 1
fi

# ------- Output info for cloud.yml -------- #
echo "
GitHub Actions setup complete! Please put the following into cloud-run.yml:

"
echo "  PROJECT_ID: '${PROJECT_ID}'"
echo "  SERVICE_NAME: '${SERVICE_NAME}'"
echo "  SERVICE_REGION: 'us-central1'"
echo "  SERVICE_ACCOUNT: '${PROJECT_NUMBER}-compute@developer.gserviceaccount.com'"
echo "  WORKLOAD_IDENTITY_PROVIDER: '${WORKLOAD_IDENTITY_PROVIDER}'"
