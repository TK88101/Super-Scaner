🧾 Super Scaner - 自動化會計憑證處理機器人
項目簡介
這是一個基於 Python 和 Google Gemini AI 的自動化工具。它能自動監聽 Google Drive 文件夾，將員工上傳的發票、收據 (PDF/圖片) 自動進行 OCR 識別，提取關鍵財務數據（日期、金額、稅率拆分），並生成符合 MoneyForward 導入標準的 CSV 報表。

核心功能：

📂 自動監聽： 監控 Google Drive 指定文件夾。

🧠 AI 識別： 使用 Gemini 2.0 Flash 進行高精度 OCR。

🧾 混合稅率拆分： 自動區分 8% (食品) 和 10% (標準) 稅率。

☁️ 雲端同步： 自動生成/更新雲端 CSV 報表。

🗄️ 自動歸檔： 處理完畢後自動歸檔原始文件。

🛠️ 第一步：本地環境搭建 (Installation)
1. 準備工作

確保你的電腦（Mac/Windows）已經安裝了 Python 3.9 或更高版本。

2. 建立虛擬環境 (Virtual Environment)

為了不汙染電腦環境，我們需要建立一個獨立的「虛擬房間」。

打開 VS Code 終端機 (Terminal)，執行：

Bash
# 1. 創建虛擬環境 (venv 文件夾)
python3 -m venv venv

# 2. 激活虛擬環境
# Mac / Linux:
source venv/bin/activate
# Windows:
# .\venv\Scripts\activate
成功標誌：終端機最前面出現 (venv) 字樣。

3. 安裝依賴庫

項目所需的所有工具都記錄在 requirements.txt 中。

Bash
pip install -r requirements.txt
🔑 第二步：獲取 Google 密鑰 (Configuration)
這是最關鍵的一步，你需要拿到 2 個鑰匙 和 3 個文件夾 ID。

1. 獲取機器人鑰匙 (service_account.json)

這把鑰匙讓代碼能讀寫 Google Drive。

登錄 Google Cloud Console。

創建一個新項目 (Project)，命名為 Super Scaner。

啟用 API：

菜單 -> APIs & Services -> Library。

搜尋 Google Drive API -> 點擊 Enable。

創建服務帳號 (Service Account)：

菜單 -> APIs & Services -> Credentials。

點擊 + CREATE CREDENTIALS -> Service Account。

名字填 scan-bot -> Create。

Role (角色) 必選： Basic -> Editor (編輯者)。

下載 JSON：

在列表點擊剛創建的帳號 -> KEYS 標籤頁。

Add Key -> Create new key -> JSON。

下載文件，重命名為 service_account.json，放入項目根目錄。

2. 獲取 AI 大腦鑰匙 (GEMINI_API_KEY)

這把鑰匙用來調用 Gemini 進行識別。

登錄 Google AI Studio。

點擊 Create API key。

複製生成的 AIza 開頭的字符串。

3. 準備 Google Drive 文件夾

在你的 Google Drive 新建一個總文件夾 SuperScaner_Root。

在裡面新建三個子文件夾：

Input (放圖片)

Processed (存檔用)

CSV_Exports (放報表)

獲取 ID： 雙擊進入每個文件夾，複製瀏覽器網址列 folders/ 後面的亂碼。

授權機器人 (重要！)：

打開 service_account.json，複製 client_email (如 scan-bot@...)。

在 Google Drive 右鍵點擊 SuperScaner_Root -> 共用 (Share)。

貼上機器人郵箱 -> 權限設為 編輯者 (Editor) -> 發送。

⚙️ 第三步：配置文件設置 (.env)
在項目根目錄新建一個名為 .env 的文件（注意前面有點），填入以下內容：

程式碼片段
# Google Drive 文件夾 ID (從瀏覽器網址複製)
INPUT_FOLDER_ID=你的Input文件夾ID
PROCESSED_FOLDER_ID=你的Processed文件夾ID
CSV_FOLDER_ID=你的CSV_Exports文件夾ID

# 機器人鑰匙文件名
SERVICE_ACCOUNT_FILE=service_account.json

# AI 鑰匙 (AIza開頭的那串)
GEMINI_API_KEY=你的Gemini_API_Key
🚀 第四步：如何運行 (Usage)
1. 啟動機器人

在 VS Code 終端機執行：

Bash
python main.py
當看到 🚀 Super Scaner 全自動版啟動！ 時，代表系統已就緒。

2. 測試流程

用手機或電腦，往 Google Drive 的 Input 文件夾上傳一張發票 (JPG/PDF)。

觀察終端機，你會看到：

⬇️ 正在下載...

🧠 Gemini 正在閱讀...

✅ 已更新雲端現有的 CSV 報表

去 CSV_Exports 文件夾查看生成的表格。

3. 停止運行

在終端機按 Ctrl + C。

📂 項目文件結構說明
為了防止你忘了每個文件是幹嘛的：

Plaintext
Super Scaner/
├── .env                   # [機密] 存放密碼和ID，絕對不能給別人
├── .gitignore             # 告訴 Git 哪些文件不要上傳
├── Dockerfile             # 用於部署到服務器的配置
├── requirements.txt       # 記錄項目安裝了哪些庫
├── service_account.json   # [機密] Google Drive 機器人鑰匙
│
├── main.py                # [主程序] 負責搬運文件、上傳下載
├── ocr_engine.py          # [大腦] 負責調用 Gemini 識別圖片、拆分稅率
└── csv_writer.py          # [會計] 負責將數據寫入 CSV 格式
🚢 第五步：部署到 GCP (進階)
如果你想讓它在雲端 24 小時運行：

準備服務器： 在 GCP Compute Engine 開一台 e2-micro (Ubuntu)。

上傳代碼： 把整個文件夾傳上去。

安裝 Docker： sudo apt install docker.io

構建鏡像：

Bash
sudo docker build -t super-scaner .
後台運行 (自動重啟)：

Bash
sudo docker run -d --restart always --name my-bot super-scaner
查看日誌：

Bash
sudo docker logs -f my-bot
⚠️ 常見問題 (FAQ)
Q1: 報錯 "Service Accounts do not have storage quota"？

原因： 機器人嘗試在你的個人雲盤創建新文件。

解決： 手動在 CSV_Exports 文件夾裡上傳一個空的 MF_Import_Data.csv，機器人就會變成「更新模式」，不會報錯。

Q2: AI 識別金額不對？

解決： 檢查 ocr_engine.py 裡的 Prompt。目前的版本已經強制 AI 「只讀取底部匯總欄，不准自己計算」，通常非常準確。

Q3: 換了電腦怎麼辦？

下載代碼 -> 放入 .env 和 json 鑰匙 -> pip install -r requirements.txt -> python main.py。一鍵復活！