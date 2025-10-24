# 1. 베이스 이미지 선택 (Python 3.13)
FROM python:3.13-slim

# 2. 작업 디렉토리 설정
WORKDIR /app

# 3. (*** 수정: Tesseract 설치 부분 삭제 ***)
#    시스템 패키지는 psycopg2 빌드용만 남김
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 4. requirements.txt 복사 및 파이썬 라이브러리 설치
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 5. 앱 코드 전체 복사
COPY . .

# 6. 환경 변수 설정 (Gunicorn 설정) - 삭제됨

# 7. 앱 실행 명령어
CMD flask init-db && gunicorn --workers 1 --bind 0.0.0.0:$PORT app:app