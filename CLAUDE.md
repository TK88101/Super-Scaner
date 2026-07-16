# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目领域

Super Scaner 是企业级会计凭证自动化机器人。流程：监听 Google Drive 上传的发票/收据 (PDF/图片) → PaddleOCR + Gemini 双引擎识别 → 结构化为日本会计仕訳 → 写入 Google Sheets (MoneyForward 导入格式) → 原票归档。产出对接 MoneyForward 记账，面向井戸会計事務所类客户。

**Chatwork 已废弃**（公司决定不使用）：`notifier.py` 与 `main.py` 的 `send_notification` 调用是死代码残留（无 token 时不发送）。任何评审/修复/新功能不得把 Chatwork 当作现行功能；是否删除残留代码由用户决定。

代码内多语言混杂：注释/日志多为日文 (面向最终用户) 与繁中。新增代码沿用同文件的语言风格。

## 环境与运行

**必须用 Python 3.11 的 `venv311` 环境** — PaddlePaddle 仅兼容 3.11。

**生产部署**：公司 Windows 迷你 PC 上 `python main.py` 常驻运行（Windows 任务计划程序：登录自启、崩溃 1 分钟后重启、02:00 JST 每日重启）。代码更新靠人工登录该 PC `git pull`（`main` 分支）后重启 —— 无自动拉取。旧的 AWS EC2 / Docker 部署已彻底废弃。

```bash
source venv311/bin/activate          # 所有 python 命令前置
# 依赖: brew install poppler (pdf2image 需要); pip install -r requirements.txt
```

```bash
python main.py                       # 生产模式: 轮询 Drive (SCAN_INTERVAL=3s)
python local_test.py                 # 本地测试 (默认 Strategy C)
python local_test.py --strategy A    # A/B/C 见下; --start-page N 断点续跑; --only-file <名>
python benchmark_ocr.py              # 三策略精度/耗时对比
python check_models.py               # 列出可用 Gemini 模型
```

测试输入图片放 `test_images/<doc_type>/` (gitignore，本地放置)。

## 测试

测试用 **unittest** 风格 (非 pytest fixture)，但两者皆可跑。涉及 `sheets_output` / `ocr_engine` 的测试必须在 venv311 跑 (依赖 gspread/paddleocr)：

```bash
venv311/bin/python -m unittest test_sheets_output -v       # 单文件
venv311/bin/python -m unittest test_sheets_output.BuildDescriptionTest.test_xxx   # 单测试
venv311/bin/python -m pytest test_tag_rules.py -v          # 也可用 pytest
venv311/bin/python -m unittest discover -p "test_*.py"     # 全部根目录测试
```

根目录 `test_*.py` 与被测模块同名配对 (如 `test_anomaly_detector.py` ↔ `anomaly_detector.py`)。

## 架构要点（需读多文件才能理解的部分）

### Generator Pipeline（内存是硬约束）
核心设计是 **逐页流式处理**，为在低内存机器 (客户 Windows 迷你 PC / 低配 PC) 上稳定运行：
`ocr_engine.process_pipeline()` 是 generator，每页 yield `{"result", "page_num", "total_pages", "page_bytes"}`；`main.process_file()` 消费一页 → 立即 `sheets_writer.append_entries()` 写入 → GC → 下一页。**不要改成先收集全部页再批量处理**，会破坏内存模型。PaddleOCR 以单例 (`_get_paddle_ocr`) 复用。

### OCR 策略 A/B/C（`_route_ocr_strategy`）
PaddleOCR 先抽文本，再按 `config.OCR_STRATEGY` (默认 "C") 路由：
- **A**: PaddleOCR 文本 → Gemini 纯文本调用
- **B**: 置信度 ≥ `OCR_CONFIDENCE_THRESHOLD` 走 A，否则回退 Gemini Vision (原图)
- **C** (推荐): PaddleOCR 文本 + 原图同时喂 Gemini 交叉验证

**OCR 主导覆盖**：日期与 T 番号 (适格请求书编号) 由 PaddleOCR 正则提取后**覆盖** Gemini 结果 (`_apply_ocr_overrides`)，因为 Gemini 对数字/日期易幻觉。`ocr_confidence` 仅在结果来自 PaddleOCR 文本路径时有意义；Vision 兜底时为 `None`，避免给兜底结果误标低置信。

### 数据流与科目映射
1. Gemini 返回原始结构 → `_apply_ocr_overrides` 用 OCR 校正日期/T番号
2. `config.ACCOUNT_MAP`: AI 通用科目名 → MoneyForward 正式名 (如 "消耗品費"→"備品・消耗品費")
3. `config.CREDIT_ONLY_ACCOUNTS`: Gemini 误把贷方科目放借方 → 兜底替换为 `UNKNOWN_ACCOUNT`
4. `doc_types.DOC_TYPE_CONFIG`: 每种文书类型的默认借贷科目 + 税区分
5. `receipt_aggregation.py`: 领収书按税率聚合多行 → 生成仕訳行 (含軽油税等特殊处理)
6. `anomaly_detector.py`: 检测日期空/取引先空/T番号空或不正/高额/要确认科目/低置信
7. `tag_rules.py`: 异常 severity → U 列标签 (赤系/橙系/黄系)

### 文书类型（`doc_types.py`）
`DocType` 四类：receipt / purchase_invoice / sales_invoice / salary_slip。各类有专属 Gemini prompt (`ocr_engine.PROMPTS`)、专属 `_build_entries_from_*` 仕訳构造、专属默认科目。新增文书类型需同步：DocType 常量、DOC_TYPE_CONFIG、ENV_FOLDER_MAP、PROMPTS、`_build_entries_from_*`、**`ocr_engine.ENTRY_BUILDERS`**、DOC_TYPE_TAB_SUFFIX。

**`ENTRY_BUILDERS` 极易漏**：它是 `DocType → _build_entries_from_*` 的分发表 (`ocr_engine.py`)。写了 builder 函数却不注册进表，等于没写。历史危害：漏注册当时不会在启动时报错——`ENTRY_BUILDERS.get(doc_type)` 返回 `None` → 一行都不 yield → `main.process_file` 数到 `count==0` → 判 Failed → **保留文件不归档** → 3 秒后再次扫到 → 无限重试，每圈烧一次 Gemini 调用。现由 `ocr_engine._validate_doc_type_registries` 在 import 时对 `DocType.ALL` 校验五张注册表（PROMPTS / ENTRY_BUILDERS / DOC_TYPE_CONFIG / DOC_TYPE_TAB_SUFFIX / ENV_FOLDER_MAP），漏一处启动即 RuntimeError（配套测试 `test_doc_type_registries.py`）。

### 多文件夹监听（`config.load_folder_map`）
`.env` 按文书类型配多个文件夹 ID (`FOLDER_RECEIPT_ID` 等)，映射为 `{folder_id: doc_type}`。兼容旧的单文件夹 `INPUT_FOLDER_ID` (默认 receipt)。上传者身份从 Drive `lastModifyingUser` email → `config.EMPLOYEE_MAP` 解析。

### Sheets 输出（`sheets_output.py`）
按 `员工名_文书类型后缀` 自动分 Tab。每行 28 列 (MF 标准 27 + 原票 URL)。A1-A4 写高亮凡例。异常单元格按 severity 标色 (`_apply_anomaly_highlight`)。取引No＝进程内存 dict `_tab_next_txn` 管理，冷启动时扫该 Tab A 列 max+1 重建（`sheets_output.py:84-101`）；`flush()` 是兼容保留的 no-op，无任何回写；`_config` tab 只存在于 GAS 侧（`gas/daily_backup.gs`），Python 运行路径无此概念（2026-07-16 盘点纠正；读取点全量清单见 `feature/sandevistan-headless` 分支的 `docs/headless-sheets-read-audit.md`）。**1000 行边界**：`_ensure_row_capacity` 自动扩容，注意历史 bug 是扩容时颜色会传染到空尾行 (`_sanitize_trailing_once` 处理)。

### 多页 PDF 精密链接（`main.PageUrlResolver`）
Drive 原生预览忽略 `#page=N`，故每页拆成单页 PDF 上传到 `SPLIT_PDF_FOLDER_ID` 并链到单独文件。冪等：单页文件名嵌入源 PDF file id，重跑/崩溃恢复时先查询既存页复用，避免重复增殖。失败时安全降级回 `base_url#page=N`。

### 错误处理语义（`main.process_file`）
- 全页失败 → `Failed`，**保留文件** 供下次重试 (不写 Sheets 占位行，防重复)
- 部分页失败 → 成功页已写，失败页写占位行，文件**归档** (防重试产生重复行)
- 判定用 `count == error_pages` 而非 `total_entries==0` (封筒/パンフ页本就 entries=0)
- `_is_envelope_page`: 封筒/送付状/挨拶状等自动跳过

### Google API 共享盘适配
所有 Drive 调用带 `supportsAllDrives=True` + `includeItemsFromAllDrives=True` — **list 缺这两个会静默返回 0 件 (无报错)**。5xx 用 `_call_with_retry` 指数退避。Service Account 无法新建 Drive 文件，Spreadsheet/文件夹须手动预建并共享给 SA。

### 备份（GAS）
- `gas/daily_backup.gs`: 22:00 JST 在 Google 服务器跑，聚合全 tab → `MF_Backup`，无需 PC 开机（有自己的时间触发器，独立于 main.py，与 monitoring 删除互不影响）。`scripts/daily_backup.py` 是已被取代的 Python cron 版（保留参考，勿在生产运行）。
- 旧的 `monitoring/` 监控子系统与 `gas/dashboard.gs` 仪表板监控的是 EC2 Docker 容器 `scan-bot`，随 EC2/Docker 环境废弃而删除（现行为公司 Windows 迷你 PC 本地常驻，无容器可监控；Chatwork 通知亦已废弃，目前没有自动化运行监控手段，靠人工查看控制台/表格产出确认）。

## 约束（项目特定）

- 改动 OCR pipeline / Sheets 写入 / 科目映射前先说明，这些直接影响客户记账正确性。
- 科目名以 `標準化勘定科目 法人.xlsx` 为准，不要臆造 MoneyForward 科目名。
- 涉及金额/批处理/重试/冪等的逻辑视为明确需求，不得以"极简"省略 (见全局 §3)。
- 密钥文件 `.env` / `service_account.json` / `*.pem` 已 gitignore，绝不提交。
- `csv_writer.py` 已废止 (CSV→Sheets 迁移)，已删除，勿恢复。
