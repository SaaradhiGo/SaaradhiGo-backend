FROM python:3.12-slim
WORKDIR /App
COPY requirements.txt /App/
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . /App
EXPOSE 8000
