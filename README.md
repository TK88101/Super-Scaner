# 🧾 Super Scaner - 自動化會計憑證處理機器人

## 📖 項目簡介
這是一個基於 Python 和 Google Gemini AI 的企業級自動化工具。它能全天候監聽 Google Drive，將員工上傳的**發票、收據 (PDF/圖片)** 自動進行 OCR 識別，提取關鍵財務數據（日期、金額、稅率拆分），並生成符合 **MoneyForward** 導入標準的 CSV 報表。

### 🌟 核心功能
* 📂 **自動監聽：** 實時監控 Google Drive 指定文件夾 (Input)。
* 🧠 **AI 視覺識別：** 使用 **Gemini 2.0 Flash** 進行高精度 OCR，無需傳統 OCR 庫。
* 🧾 **混合稅率拆分：** 自動區分 **8% (食品/輕減)** 和 **10% (標準)** 稅率。
* ☁️ **雲端同步：** 自動生成並更新雲端 **CSV_Exports** 文件夾中的報表。
* 🛡️ **格式過濾：** 自動忽略 Word/Excel 等不支持的格式，只處理圖片與 PDF。
* 🗄️ **自動歸檔：** 處理完畢後自動將原始文件移入 **Processed** 文件夾。

---

## 🛠️ 本地開發環境搭建 (Local Setup)

### 1. 準備工作
確保電腦已安裝 **Python 3.9** 或更高版本，以及 **Git**。

### 2. 建立虛擬環境
```bash
# 1. 創建虛擬環境
python3 -m venv venv

# 2. 激活環境 (Mac/Linux)
source venv/bin/activate
# Windows 用戶請執行: .\venv\Scripts\activate
```

### 3. 安裝依賴
```bash
pip install -r requirements.txt
```

---

## 🔑 配置說明 (Configuration)

本項目依賴 **Google Cloud** 和 **Google AI** 服務。請確保根目錄下有以下兩個配置文件。

### 1. `.env` 文件 (環境變量)
請在根目錄新建 `.env` 文件，填入以下內容：

```env
# --- Google Drive 文件夾 ID (從瀏覽器網址欄獲取) ---
INPUT_FOLDER_ID=你的_Input_文件夾ID
PROCESSED_FOLDER_ID=你的_Processed_文件夾ID
CSV_FOLDER_ID=你的_CSV_Exports_文件夾ID

# --- 認證文件路徑 ---
SERVICE_ACCOUNT_FILE=service_account.json

# --- AI 模型密鑰 ---
GEMINI_API_KEY=你的_AIza開頭的Key
```

### 2. `service_account.json` (機器人鑰匙)
* 從 Google Cloud Console 下載的 JSON 密鑰文件。
* **重要：** 務必在 Google Drive 將 `Input`, `Processed`, `CSV_Exports` 三個文件夾 **共用 (Share)** 給 JSON 文件中的 `client_email` 地址（必須設為 **編輯者 Editor**）。

---

## 🚀 如何運行 (Usage)

### 啟動機器人
在終端機執行：
```bash
python main.py
```

### 使用流程
1.  員工將發票/收據 (JPG, PNG, PDF) 上傳至 Google Drive 的 **Input** 文件夾。
2.  機器人自動下載並識別 (終端機顯示進度)。
3.  識別成功後，會自動更新 **CSV_Exports** 文件夾裡的 `MF_Import_Data.csv`。
4.  原始圖片會被移動到 **Processed** 文件夾歸檔。

---

## 📂 項目結構

```text
Super Scaner/
├── .env                        # [機密] 環境變量 (Git 已忽略)
├── .gitignore                  # Git 忽略清單
├── .dockerignore               # Docker 忽略清單
├── Dockerfile                  # Docker 部署配置
├── PRD.md                      # 產品需求文檔
├── requirements.txt            # Python 依賴庫
├── service_account.json        # [機密] Google Drive 權限鑰匙 (Git 已忽略)
│
├── main.py                     # [主程序] 流程控制、文件監聽、雲端同步
├── ocr_engine.py               # [AI 引擎] 調用 Gemini 進行視覺識別與稅率拆分
├── csv_writer.py               # [寫入器] 生成 MoneyForward 格式 CSV
│
├── scripts/
│   └── deploy_ec2.sh           # AWS EC2 一鍵部署腳本
│
└── monitoring/                 # 監控子系統
    ├── system_metrics.py       # 系統指標採集 (CPU/RAM/Disk)
    ├── docker_metrics.py       # Docker 容器狀態與日誌採集
    ├── log_parser.py           # 日誌解析與統計提取
    ├── sheets_writer.py        # Google Sheets 寫入器 (含行數限制管理)
    ├── metrics_pusher.py       # 指標推送主控腳本 (cron 每分鐘執行)
    ├── cleanup.py              # 每小時定期清理 Sheets 舊數據
    ├── install_cron.sh         # EC2 cron 安裝腳本
    └── tests/                  # 單元測試
```

---

## ☁️ AWS EC2 一鍵部署 (Ubuntu + Docker)

本專案新增了可直接執行的部署腳本：
`scripts/deploy_ec2.sh`

### 快速部署
在本機終端執行：
```bash
cd "/Users/ibridgezhao/Documents/Super Scaner"
chmod +x scripts/deploy_ec2.sh

# 建議明確指定目標主機
EC2_HOST=13.112.35.6 EC2_USER=ubuntu \
SSH_KEY="/Users/ibridgezhao/Documents/Super Scaner/SuperScaner.pem" \
bash scripts/deploy_ec2.sh
```

### 安全說明
* `SuperScaner.pem` 僅用於本機 SSH 連線，不會被上傳到 VPS。
* 腳本只會上傳兩個機密文件：
  * `.env`
  * `service_account.json`
* 容器啟動時使用：
  * `--env-file /home/ubuntu/super-scaner-secrets/.env`
  * `-v /home/ubuntu/super-scaner-secrets/service_account.json:/app/service_account.json:ro`

---

## 📊 監控系統 (Monitoring Dashboard)

本項目內置了 **AWS EC2 即時監控系統**，每分鐘自動將伺服器健康數據推送到 Google Sheets 儀表板。

### 監控指標
* 🖥️ **系統指標：** CPU 使用率、RAM 使用率、磁盤使用率
* 🐳 **Docker 狀態：** 容器運行狀態、啟動時間
* 📋 **應用日誌：** OCR 處理成功/失敗統計
* 🗃️ **數據保留：** 心跳數據保留 24 小時（1440 筆），日誌保留 500 筆

### 所需環境變量
```env
MONITOR_SPREADSHEET_ID=你的_Google_Sheets_ID
```

### 部署監控
```bash
# 安裝 cron 任務 (每分鐘執行一次)
bash monitoring/install_cron.sh

# 手動測試推送
python monitoring/metrics_pusher.py

# 手動觸發清理
python monitoring/cleanup.py
```

### Google Sheets 儀表板結構
| 分頁 | 內容 |
|------|------|
| Heartbeat | 每分鐘系統指標 |
| Logs | 應用錯誤與事件日誌 |
| Stats | 每日處理統計 |
| Summary | 系統概覽 |

---

## 🚢 GCP 服務器部署指南 (Server Deployment)

本項目支持 Docker 容器化部署，推薦使用 **Google Cloud Platform (GCP)** 的 **Compute Engine**。

**推薦配置：**
* **OS:** Ubuntu 22.04 LTS
* **Machine:** e2-micro (或 e2-small)
* **Disk:** 20GB Standard Persistent Disk

### 第一步：環境準備
SSH 登錄服務器後，安裝 Docker 和 Git：
```bash
sudo apt-get update
sudo apt-get install -y docker.io git
```

### 第二步：下載代碼
由於是私有倉庫，需要配置 Deploy Key：
```bash
# 1. 生成密鑰 (一路回車)
ssh-keygen -t ed25519 -C "gcp_server"

# 2. 查看公鑰 (複製內容添加到 GitHub -> Repo Settings -> Deploy keys)
cat ~/.ssh/id_ed25519.pub

# 3. 下載代碼
git clone git@github.com:TK88101/Super-Scaner.git
cd Super-Scaner
```

### 第三步：上傳機密文件
使用 SSH 工具 (如 VS Code Remote 或 GCP 控制台右上角的 "Upload files") 將以下兩個本地文件上傳到服務器項目目錄中：
1.  `.env`
2.  `service_account.json`

### 第四步：Docker 啟動 (後台運行)
```bash
# 1. 構建鏡像
sudo docker build -t super-scaner .

# 2. 啟動容器 (設置為自動重啟，保證 24/7 在線)
sudo docker run -d --restart always --name scan-bot super-scaner
```

### 常用運維指令
* **查看日誌：** `sudo docker logs -f scan-bot`
* **停止服務：** `sudo docker stop scan-bot`
* **重啟服務：** `sudo docker restart scan-bot`

---

## ⚠️ 常見問題 (FAQ)

**Q1: 報錯 "Service Accounts do not have storage quota"？**
* **原因：** 機器人嘗試在你的個人免費版 Google Drive 創建新文件。
* **解決：** 請手動在 `CSV_Exports` 文件夾裡上傳一個空的 `MF_Import_Data.csv`，機器人就會自動切換為「更新模式」，不會報錯。企業版 Workspace 帳號無此限制。

**Q2: AI 識別金額不準確？**
* **解決：** 本項目已在 Prompt 中加入了「審計級」指令，強制 AI 讀取收據底部的「稅率匯總欄」，並禁止 AI 自行計算加總，從而保證金額 100% 準確。

**Q3: 換了電腦怎麼辦？**
* 下載代碼 -> 放入 `.env` 和 `json` 鑰匙 -> `pip install -r requirements.txt` -> `python main.py`。一鍵復活！

---
*Generated for Project Super Scaner*
