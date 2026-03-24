# 1. 使用輕量級的 Python 基礎鏡像
FROM python:3.11-slim

# 2. 設置工作目錄
WORKDIR /app

# PaddleOCR + pdf2image system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    libglib2.0-0 \
    libgl1 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 3. 複製依賴清單並安裝
# (所以請務必先執行 pip freeze > requirements.txt)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. 複製所有代碼文件到容器內
COPY . .

# 5. 設置環境變量 (讓 Python 打印日誌不緩存，方便查看)
ENV PYTHONUNBUFFERED=1

# 6. 啟動命令
CMD ["python", "main.py"]