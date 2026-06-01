FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bus_exporter.py .
CMD ["python", "bus_exporter.py"]
