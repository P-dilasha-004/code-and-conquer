# Worker image for persist_worker.py — the distributed chat pipeline's
# persistence consumer. Separate deployable unit from the main Streamlit
# app's Dockerfile: a Pub/Sub PUSH endpoint (Flask/gunicorn), not a
# Streamlit UI. Runs on Cloud Run like the main app, scaling to zero when
# idle since it's plain HTTP request/response, not a standing pull loop.
FROM python:3.10

EXPOSE 8081

WORKDIR /app

COPY ./src/worker/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY ./src/worker/persist_worker.py ./persist_worker.py

ENTRYPOINT ["sh", "-c", "gunicorn --bind :${PORT:-8081} persist_worker:app"]
