FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY rackmap ./rackmap
ENV RACKMAP_DATA=/data
VOLUME /data
EXPOSE 8080
USER 1000
CMD ["uvicorn", "rackmap.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]
