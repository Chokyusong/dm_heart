FROM python:3.11-slim

# OS 패키지
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg unzip fonts-noto-cjk locales \
    && rm -rf /var/lib/apt/lists/*

# 로케일(한글 로그 깨짐 방지)
RUN sed -i 's/# ko_KR.UTF-8 UTF-8/ko_KR.UTF-8 UTF-8/' /etc/locale.gen && locale-gen
ENV LANG=ko_KR.UTF-8 LC_ALL=ko_KR.UTF-8

# Google Chrome 설치
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor > /usr/share/keyrings/google.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# 작업 디렉토리
WORKDIR /app
COPY requirements.txt /app/

# 파이썬 패키지
RUN pip install --no-cache-dir -r requirements.txt

# 앱 파일
COPY . /app

# 스트림릿 설정(헤드리스)
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
ENV STREAMLIT_SERVER_HEADLESS=true

# 컨테이너 포트
EXPOSE 8501

# 실행
CMD ["streamlit", "run", "main_app.py", "--server.port=8501", "--server.address=0.0.0.0"]
