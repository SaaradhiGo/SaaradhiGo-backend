FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_REQUIRE_HASHES=0 \
    PIP_INDEX_URL=https://pypi.org/simple

WORKDIR /App
COPY requirements.txt /App/

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY . /App
EXPOSE 8000
