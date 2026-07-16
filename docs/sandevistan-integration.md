# Super Scaner ← サンデヴィスタン 集成对接说明（执行器义务）

> 本文件是**只读对接说明**，不改 Super Scaner 任何代码逻辑。
> 权威契约在胶水仓库：`~/Documents/サンデヴィスタン/contracts/job-state-machine.md`（**v0.5**，2026-07-04）。
> 角色：サンデヴィスタン 是**控制面/状态机**（Firestore 单一权威状态源），把 Super Scaner 当**被动回报的执行器**（收文件→OCR+仕訳+写 Sheets→回报状态）。
> 更新日：2026-07-04（第 3 轮修订：回写已決 F53/F54/F15/F59/F20，全文对应「⚠️待拍板」注记改为「已決」修订注记，详见 サンデヴィスタン/docs/pending-decisions.md）。前轮：2026-07-03（第 2 轮修订：应用机械修正 F01/F02/F03/F31/F45/F49，插入待拍板注记 F06/F09/F15/F20/F53/F54/F56）。初稿 2026-06-30（经趙逐条拍板 + 本仓库代码勘察 + Codex 对辩定稿）。SS 侧改动一律走分支 `feature/sandevistan-headless`（`HEADLESS_MODE`）；**本轮回写的全部新增/修订义务同样仅适用该分支，现役 UI 版行为零改动**。

---

## 0. 一句话

控制面交棒一份文件给 SS 时会带一个 **base `posting_id`**（载体=Drive 公开 `properties`，(F20已決2026-07-04)）；SS 按**页级**把同页全部票聚合为一次 `append_rows` 原子写 (F53已決2026-07-04)，去重与断点续跑以 **Firestore postings 台账三步协议**为权威、彻底不查 Sheets (F54已決2026-07-04)，最后用 Firestore SDK 回报 `POSTED`/`DEAD_LETTER`。〔修订注记：原句「按票/页级做硬去重并能从断点票续跑」据 F53/F54 更新为上述口径〕

---

## 1. 今日定稿的关键结论（与本仓库现状的关系）

| 决策 | 对 SS 的含义 | 勘察依据（本仓库代码） |
|---|---|---|
| **content_hash = Drive `md5Checksum`** | 控制面复用 SS 现成同款字段当幂等键。SS 无需改，知悉即可：公司统一扫描，不存在同票两扫，字节级 MD5 足够。〔修订 (F59已決2026-07-04)：「不存在同票两扫」前提被双入口（客户手机拍照+员工到店扫描）修正——字节级 MD5 仍作幂等键，内容级（日期+金额+取引先+摘要）四元组判重**不进 SS 写账关键路径**，由控制面対账侧批量扫 Firestore 台账承担（advisory 不拦截），另加员工规程「客户夹已出现的票不再到店扫描」〕 | `main.py:259 is_duplicate_file` |
| **写账粒度 = 页级原子化** (F53已決2026-07-04) | headless 分支：**同页全部票（含识别不能页的占位行）聚合为一次 `append_rows` 原子写**（`main.py:320` 循环缓冲 + `sheets_output` 新增页级方法）→ 页内崩溃窗口消失，崩溃恢复边界在页之间。实现要点：①颜色高亮按票段偏移计算（唯一实现风险，**单测须覆盖「同页 2 票+异常高亮位置」**）；②取引No 整页重试可跳号（无业务影响）。〔修订注记：原「每票一次 `append_entries`、恢复边界在票之间」及「一页多票致 posting_id 派生公式待改」的待拍板问题据 F53 关闭——原选项 a（票序 d{doc_idx}）/ b（票内容哈希）/ c（强制一页一票）全否〕 | `main.py:320 for page in process_pipeline()`；`sheets_output.py:395 append_rows`；`ocr_engine.py:1460 _normalize_receipt_results` |
| **posting_id 页级派生** (F53已決2026-07-04，原「票级派生」修订) | base 由控制面生成；公式**保持页级 `{base}:p{page_num}`**（契约 §5.1 公式不变，id 内不需要 doc_idx），同页多票共享同一页级 id、随页级原子写一并落账，去重粒度=页级。〔修订注记：原「待拍板改为页内票序或票内容哈希」据 F53 关闭〕 | 新增义务，见 §2 |
| **停滞检测 = 页级打卡制** (F39已決2026-07-04，原 per_page×total_pages 总时长制**已废**) | SS 不直接管停滞判定，也**无需任何续租动作**——每次页级台账写入（`written_at`）本身即打卡；控制面 reconciliation 按「距最近打卡超 `stall_threshold`（初值 10 分钟）」判停滞，触发只读复核（查 **Firestore postings 台账**），**不会重复记账**（靠台账三步协议兜底）。检测延迟与件大小无关（入口不设页数/体积上限，客户一整年合集照收）。〔修订 (F54已決2026-07-04)：记账去重权威=Firestore postings 台账，**彻底不查 Sheets**——每日 22:00 GAS 备份清空输出 Sheets 不再影响去重（员工备份流程零改动）；仅 PENDING 残余窗口按「查当日 Sheets 该页行在否」补判，见 §2-1〕 | 控制面侧 |
| **回报传输：SS 直连 Firestore** | SS 用 Firestore SDK 直接调 `transition` 回报（Python SDK 成熟）。注意：（議題1-B 已決2026-07-04，契约 v0.6）分类已内化进控制面进程，GAS/inbox/HTTP 端点方案**全部作废**——管线内跨运行时回报只剩 SS 这一条 Firestore SDK 通道 | — |

---

## 2. SS 必须实现 / 改造的义务

1. **页级原子写 + 台账三步协议（核心）**〔修订注记：原「票级硬去重」义务据 F53/F54/F15/F59（均已決2026-07-04）整体更新为以下口径；原「`append` 前按 posting_id 查 Sheets、命中整票跳过」已废〕：
   - **写账粒度 (F53已決2026-07-04)**：headless 分支同页全部票（含识别不能页的占位行）聚合为**一次 `append_rows` 原子写**；posting_id 公式保持页级 `{base}:p{n}`。实现要点：①颜色高亮按票段偏移计算——**单测须覆盖「同页 2 票+异常高亮位置」**；②取引No 整页重试可跳号（无业务影响）。
   - **三步写账协议 (F54已決2026-07-04)**：查 Firestore 台账 → 记 `PENDING` → 一次 `append_rows` → 改 `CONFIRMED`。台账=子集合 `jobs/{job_key}/postings/{page_id}`，字段 `{page, ticket_count, status: PENDING|CONFIRMED, sheet_row_range, written_at, tickets:[{date,amount,vendor}]}`。**去重彻底不查 Sheets**（22:00 备份清空不再影响查重面）。
   - **重跑判定 (F54已決2026-07-04)**：无记录→写；`CONFIRMED`→跳过；`PENDING`→查**当日** Sheets 该页行在否（在→补 `CONFIRMED`，不在→重写）；`PENDING` 跨过 22:00 清空→不猜，降级 `POST_UNKNOWN` 人工核（対账报告可见）。
   - **posting_id 不进 Sheets (F15已決2026-07-04)**：MF 28 列输出契约一列不动（`MF_HEADERS` 恰 28 项、`daily_backup.gs TOTAL_COLUMNS=28` 均不动），id 只活在 Firestore 台账；人工対账「Sheets 行 ↔ 页」双向定位靠台账 `sheet_row_range`。〔修订注记：原「posting_id 在 Sheets 存哪列未定」据 F15 关闭，本条零遗留〕
   - **四元组重复检测不进写账关键路径 (F59已決2026-07-04)**：SS 现状**无生效的行级重复检测**——`sheets_output.py:582 _detect_and_highlight_duplicates` 为**未接线的死代码**（全仓库零调用点，实际配色浅紫非黄），**不复活进关键路径**、仅作「日期+金额+取引先+摘要」四元组的参考实现；内容级判重由**控制面対账侧批量扫 Firestore 台账**承担（advisory 不拦截，翌日対账报告列疑似对、人工核），台账 `tickets` 摘要字段即为此服务。〔修订注记：原 F31 注记「该死代码可作四元组软检测的实现起点」据 F59 更新为上述口径〕
   - **接口签名已定（2026-07-04 第 6 场議題1，权威=胶水仓库 basic-design/01 签名段）**：`derive_page_id(base, page_num) -> str`＋`check_page(page_id) -> WRITE|SKIP|ESCALATE`（三值：无记录→WRITE；CONFIRMED→SKIP；PENDING＋当日 Sheets 该页行在→内部补 CONFIRMED→SKIP；PENDING 跨 22:00→ESCALATE，整件回报 POST_UNKNOWN 停写。取代原 `is_posted -> bool`——「转人工」出口 bool 表达不了）＋`post_page(page_id, rows, tickets) -> None`（记 PENDING 含 tickets 摘要/ticket_count → 一次 append_rows → 改 CONFIRMED 记 sheet_row_range；占位行=rows 普通一行）。
2. **接收交棒契约**：SS 监听夹 = 控制面投放夹（Drive 资料夹 ID 两侧一致，P7）＝**`handoff_folder_id` 全局静态配置**——与归档目标 `route_folder_id` 分离（F11已決甲 2026-07-04，契约 v0.7/ADR-008，乙案「SS 按 job 领件」关闭，夹监听定稿）；交棒随件传入 **base posting_id**，SS 需读取并在写账时派生页级 id。
   - 交棒顺序已钉死：控制面**先 `transition`（设 lease）后 move 文件**；move 失败/中间崩溃由控制面 reconciliation 兜住，SS 无需处理 (F01)。
   - **已決 (F20已決2026-07-04)**：①载体=Drive 文件的**公开 `properties`**（**不是 appProperties**——appProperties 为 per-OAuth-client 私有，跨凭证读不到）；控制面顺序钉死「**先写属性、后 move**」，故监听夹内文件必然带 base posting_id（写属性未 move 的残留由控制面既有 reconciliation 重试 move 兜）。②**读不到属性的文件一律不处理**：挪隔离夹 + **直写 Firestore `alerts/{file_id}` 上报**（inbox 已随議題1-B 作废 2026-07-04；SS 有 SDK、文档 ID 天然幂等）——监听夹无 id 文件无任何合法场景（回流已废；人工承接走独立带 UI 版 SS，与本管线无关）。〔修订注记：原「载体未钉死 / 无 id 文件策略未定」据 F20 关闭〕
3. **断点页续跑**〔修订注记 (F53/F54已決2026-07-04)：原「断点票续跑，靠票级 posting_id 去重」更新为页级〕：崩溃重跑时按 §2-1 台账三步协议的**重跑判定**逐页续跑（配合控制面 `POST_UNKNOWN` + reconciliation）。SS 现有「全页失败保留文件 / 部分页失败写占位行归档」语义需与此对齐。
   - ⚠️[待拍板 F06→docs/pending-decisions.md] 部分失败语义未定义：契约回报终点只有 `POSTED`（全部成功）/`DEAD_LETTER`（整件 NON_RETRYABLE），「4 页已写账 + 1 页永久失败」两边都不是；SS 现状占位行（`main.py:374-392` → return True 归档）无 posting_id，崩溃重跑会重复占位行。待定：新增 `POSTED_PARTIAL` vs 任一页永久失败即整件 `DEAD_LETTER`（会计口径需问员工）。〔修订注记 (F53/F54已決2026-07-04)：其中「占位行无 id、重跑重复占位行」一段已消解——占位行随 F53 并入同页一次 `append_rows`（共享页级 `{base}:p{n}`、计入台账 `ticket_count`），重跑防重由台账三步协议承担；F06 仅剩回报态（`POSTED_PARTIAL` 与否）待拍〕
4. **状态回报**：用 Firestore SDK 调 `transition`：全部票处理完 → `POSTED`；`NON_RETRYABLE`（损坏/加密/空）→ `DEAD_LETTER`。
   - **transition 被拒语义 (F01)**：被拒时若目标态已达成 → 视为**幂等成功、忽略**；其他被拒情况写 `alerts/` 交控制面裁决（inbox 已废，議題1-B 2026-07-04），SS **禁止自行重试写账**。
   - **时间戳口径 (F49)**：所有 timestamp 一律 **UTC 存储**（Firestore Timestamp 原生 UTC），展示层转 JST；不得混用 GAS manifest 的 America/New_York 口径。
5. ~~**回流入口（D20）**：新增「接收人工分解结果、注入正在运行流程」入口（契约 §7 / plan §5.2）。回流复用原 base posting_id；**例外**：若人工分类改了 `doc_type_code` 导致记账内容变，控制面会作废原 posting_id 重生新的，SS 按收到的 base 走即可。~~〔作废注记 (F20已決2026-07-04，承 2026-07-03 回流机制整体删除)：**回流已废，本义务不再成立**——管线内 SS 不再有任何「接收人工分解结果注入管线」入口；人工承接一律走独立带 UI 版 SS（本管线之外）。原文保留备考〕
6. **U7 benchmark 配合**：需 SS 跑真实票据，量出**单页端到端（OCR→写 Sheet）P99 耗时**，供控制面把 `stall_threshold` 从 10 分钟逐步缩减校准（F39 打卡制；原 per_page 参数已废）。**样本须含超大合集件**（数百页，验证内存与逐页处理——入口不设上限的实测替代）。
7. **停用 Chatwork 通知（D16, F45）**：headless 分支部署环境**不配置 `CHATWORK_API_TOKEN`**（`notifier.py` 未配 token 即静默跳过，零代码改动；`main.py:362/410/419` 成功/失败路径的 `send_notification` 调用无需删）。不得因此吞掉失败信号——失败上报一律以 Firestore `transition` 为准。

---

## 3. 不属于 SS 的部分（避免误改）

- 状态机/transition/乐观锁/重试调度/reconciliation/Kintone 路由/posting_id 生成 → **全在控制面**，SS 不实现。
- 分类段超时租约（`classify_timeout`，F02）、路由查表 / 交棒移动 / 归档收尾各阶段的 `RETRYABLE` 重试自环与 `FAILED_FINAL` 出口（F03）→ 同样**全在控制面**，SS 不感知这些阶段的重试。
- `CONFIG_MISSING`（Kintone 无路由映射）、`UNPROCESSED`（双 AI 分歧）等分流状态 → 控制面/Xenomorph 侧，SS 不感知。
- SS 只负责：收文件 → 逐页 OCR+仕訳 → 页级原子写 Sheets（台账三步协议）→ 回报。〔修订注记 (F53/F54已決2026-07-04)：原「票级去重写 Sheets」更新〕
- **F09 已決（2026-07-04 第 6 场＝a，契约 v0.7/ADR-008）**：headless 分支以 `HEADLESS_MODE=1` **禁用成功路径 `move_file`**（`main.py:501-502`）——SS 只写账＋回报 POSTED，文件留交棒夹、归档搬运全归控制面（契约 §3.2 `POSTED→DONE`）。连带停用 `is_duplicate_file`（`main.py:259`，只查旧 Processed 夹＝新架构下僵尸检查；防重单点＝控制面 `create_if_absent` md5）。**不涉单页 PDF 上传路径**：`PageUrlResolver`（`main.py:311`）拆页上传 `SPLIT_PDF_FOLDER_ID`（`config.py:10`）属写账路径产物、Sheets `source_url` 永续链接目标——headless **照常运行**、部署 `.env` 必配该 key；控制面归档/清理不得触碰该夹。UI 版（单独版）行为零改动——F09/F11 均为接入管线才产生的矛盾，单独版无此问题。

---

## 4. 对接前先读

权威契约（字段名/状态/流转以它为准）：`~/Documents/サンデヴィスタン/contracts/job-state-machine.md`
要件定義書全量：`~/.claude/plans/https-notebooklm-google-com-notebook-e35-piped-rossum.md`
