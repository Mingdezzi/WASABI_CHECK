# 1. 베이스 이미지 선택 (Python 3.13)
FROM python:3.13-slim

# 2. 작업 디렉토리 설정
WORKDIR /app

# 3. 시스템 패키지 설치 (Tesseract OCR 등) - 첫 번째 RUN
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-kor \
    tesseract-ocr-eng \
    libtesseract-dev \
    # 필요한 빌드 도구
    gcc \
    libpq-dev && \
    # 설치 후 캐시 정리
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 4. requirements.txt 복사 및 파이썬 라이브러리 설치 - 두 번째 RUN
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 5. 앱 코드 전체 복사
COPY . .

# 6. 환경 변수 설정 (Gunicorn 설정)
ENV GUNICORN_CMD_ARGS="--workers 1 --bind 0.0.0.0:$PORT app:app"

# 7. 앱 실행 명령어
CMD flask init-db && gunicorn $GUNICORN_CMD_ARGS