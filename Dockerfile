FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only what the server needs to run read-only out of the box.
COPY app/ ./app/
COPY data/ ./data/
COPY processed_data/ ./processed_data/

# Default to read-only demo mode — the committed bundle is served, no API key needed.
ENV DEMO_READONLY=1

EXPOSE 8000

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
