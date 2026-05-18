FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for GeoPandas, Rasterio, and WeasyPrint
RUN apt-get update && apt-get install -y \
    gdal-bin \
    libgdal-dev \
    libpq-dev \
    postgresql-client \
    ffmpeg \
    gcc \
    g++ \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-xlib-2.0-0 \
    libffi-dev \
    libglib2.0-0 \
    fonts-liberation \
    dos2unix \
    && rm -rf /var/lib/apt/lists/*

# Set env vars for GDAL
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Strip Windows CRLF from all shell scripts (safety net for Windows checkouts)
RUN find /app -name "*.sh" -exec dos2unix {} \;

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
