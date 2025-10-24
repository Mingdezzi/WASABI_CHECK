# 1. 베이스 이미지 선택 (Python 3.13)
FROM python:3.13-slim

# 2. 작업 디렉토리 설정
WORKDIR /app

# 3. 시스템 패키지 설치 (Tesseract OCR + 한국어/영어 언어팩)
#    RUN 명령어는 Docker 이미지를 빌드할 때 실행됨
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-kor \
    tesseract-ocr-eng \
    # 필요한 빌드 도구 (psycopg2 설치 시 필요할 수 있음)
    gcc \
    libpq-dev && \
    # 설치 후 캐시 정리
    rm -rf /var/lib/apt/lists/*

# 4. requirements.txt 복사 및 파이썬 라이브러리 설치
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 5. 앱 코드 전체 복사
COPY . .

# 6. 환경 변수 설정 (Gunicorn 설정)
ENV GUNICORN_CMD_ARGS="--workers 1 --bind 0.0.0.0:$PORT app:app"
#    $PORT 는 Render가 자동으로 설정해주는 포트 번호

# 7. 앱 실행 명령어 (Render가 이 명령어를 사용함)
#    먼저 DB 테이블 생성 후 Gunicorn 서버 시작
CMD flask init-db && gunicorn $GUNICORN_CMD_ARGS