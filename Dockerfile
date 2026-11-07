FROM python:3.12-slim
ENV APP_ENV=production DATABASE_PATH=/data/company.db PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv/backend
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home demo \
    && mkdir -p /data && chown demo:demo /data && chmod 700 /data
COPY app/ ./app/
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "app.serve"]
