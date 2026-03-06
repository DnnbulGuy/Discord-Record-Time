FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# DB 파일이 저장될 폴더 생성
RUN mkdir -p /app/data
CMD ["python", "main.py"]
