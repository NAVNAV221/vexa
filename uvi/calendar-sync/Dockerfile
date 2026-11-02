FROM python:3.12-slim
RUN pip install --no-cache-dir "google-auth==2.*" "requests==2.*"
COPY sync.py /app/sync.py
RUN useradd -r -u 10001 sync && mkdir /state && chown sync /state
USER sync
CMD ["python", "-u", "/app/sync.py"]
