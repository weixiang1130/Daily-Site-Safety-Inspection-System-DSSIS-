# Python 後端容器。可部署到任何支援容器的平台
# （Render / Railway / Fly.io / Azure App Service / 公司內網 Docker）。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Taipei

WORKDIR /app

# ODBC 驅動：連 SQL Server 時需要。若後端資料庫用 PostgreSQL，
# 這一段可以拿掉以縮小映像檔。
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/microsoft.gpg] \
        https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
    && apt-get purge -y gnupg \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements-mssql.txt

COPY . .

# 上傳的照片、簽名圖與產出的 PDF 都寫在這裡，
# 部署時務必掛載持久化磁碟，否則每次重新部署資料會消失。
VOLUME ["/app/uploads"]

EXPOSE 8000

# PDF 使用 reportlab 內建的中文 CID 字型，容器內不需安裝任何字型檔。
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
