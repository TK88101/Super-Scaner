// ============================================================
// Super Scaner - Daily Backup & Clear (GAS) — 井戸会計事務所（社長専用）
// Deploy: SuperScaner_Internal/井戸会計事務所/MF_Import_Data > 拡張機能 > Apps Script
// Run setupTriggers() once to register automated triggers
//
// 重要: 必ず automation@ido-office.fukuoka.jp で配置すること。
//   対象シートは Drive の「アクセス制限」フォルダ内にあり、共有ドライブの
//   管理者と直接追加されたユーザーしか開けない。時間主導型トリガーは配置者の
//   権限で走るため、閲覧権の無いアカウントで配置すると毎晩静かに失敗する。
//
// ⚠ 同期必須: 本ファイルのロジックは daily_backup.gs / daily_backup_ido.gs /
//   daily_backup_rental.gs の3本で完全に同一。差分は先頭コメントと
//   SOURCE_SS_ID / BACKUP_SS_ID の2定数のみ。ロジックを直したら必ず3本とも直すこと。
//   (GAS のコンテナバインドスクリプトはスプレッドシート単位が配置単位のため
//    コード共有には Apps Script Library が必要だが、手動配置の運用には過剰と判断)
// ============================================================

const SOURCE_SS_ID = '1B0VOt4tDnp4zYwcs_3XnY0S2sPQXQ9uVkCUPoEDjhzc';
const BACKUP_SS_ID = '14P7AW-kJUBniWDfj2iM9aeLwnM54trvAQPU07KQc5hk';
const TOTAL_COLUMNS = 28;
const RETENTION_DAYS = 30;
const TIMEZONE = 'Asia/Tokyo';
const SKIP_TABS = ['_config'];
const HEADER_ROWS = 5;


/**
 * Main entry: backup all data tabs to MF_Backup, then delete source tabs.
 * Two-phase commit: delete only after successful backup.
 */
function dailyBackupAndClear() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) {
    Logger.log('Another backup is already running. Skipping.');
    return;
  }

  try {
    var result = backupAllTabs_();
    if (result.success) {
      var deleteResult = deleteSourceTabs_(result.tabs);
      if (deleteResult.abortedAll) {
        Logger.log(
          'Backup and clear completed: ' + result.tabs.length + ' tabs backed up, deletion ' +
          'ABORTED for all of them (all-or-nothing) because ' + deleteResult.skipped.length +
          ' tab(s) changed since backup read them; all ' + result.tabs.length +
          ' tab(s) kept for tomorrow\'s backup.'
        );
      } else {
        Logger.log(
          'Backup and clear completed: ' + result.tabs.length + ' tabs backed up, ' +
          deleteResult.deletedCount + ' deleted, 0 skipped'
        );
      }
    } else {
      Logger.log('No data to backup. Nothing deleted.');
    }
  } catch (e) {
    Logger.log('BACKUP FAILED: ' + e.message);
    try {
      MailApp.sendEmail(
        Session.getActiveUser().getEmail(),
        'Super Scaner: Backup Failed',
        'Daily backup failed at ' + new Date() + '\nError: ' + e.message + '\n' + e.stack
      );
    } catch (mailErr) {
      Logger.log('Failed to send error email: ' + mailErr.message);
    }
  } finally {
    lock.releaseLock();
  }
}


/**
 * Read all data tabs from MF_Import_Data and write a consolidated backup sheet.
 * Returns {success: boolean, tabs: [{name: string, sheetId: number, lastRow: number}]}
 *
 * sheetId + lastRow are captured at the moment each tab is read here, so
 * deleteSourceTabs_() can re-measure just before deleting and skip a tab
 * whose data changed underneath us (see deleteSourceTabs_ doc comment for
 * why this only narrows, not closes, the read-then-delete race).
 */
function backupAllTabs_() {
  var source = SpreadsheetApp.openById(SOURCE_SS_ID);
  var sheets = source.getSheets();

  // Collect tabs with data
  var dataTabs = [];
  for (var i = 0; i < sheets.length; i++) {
    var sheet = sheets[i];
    var name = sheet.getName();
    if (SKIP_TABS.indexOf(name) >= 0 || name.charAt(0) === '_') continue;
    if (sheet.getLastRow() <= HEADER_ROWS) continue;
    dataTabs.push({sheet: sheet, name: name});
  }

  if (dataTabs.length === 0) {
    return {success: false, tabs: []};
  }

  // Open backup spreadsheet and create today's sheet
  var backup = SpreadsheetApp.openById(BACKUP_SS_ID);
  var today = Utilities.formatDate(new Date(), TIMEZONE, 'yyyy-MM-dd');

  // Idempotent: delete existing sheet for today (safe re-run)
  var existing = backup.getSheetByName(today);
  if (existing) {
    backup.deleteSheet(existing);
  }

  var backupSheet = backup.insertSheet(today);

  // Write column headers (from first data tab's row 5)
  var headers = dataTabs[0].sheet.getRange(HEADER_ROWS, 1, 1, TOTAL_COLUMNS).getValues()[0];
  backupSheet.getRange(1, 1, 1, TOTAL_COLUMNS).setValues([headers]);
  backupSheet.getRange(1, 1, 1, TOTAL_COLUMNS).setFontWeight('bold');

  var currentRow = 2;
  var tabs = []; // snapshot of {name, sheetId, lastRow} taken at read time, for deleteSourceTabs_

  for (var t = 0; t < dataTabs.length; t++) {
    var tab = dataTabs[t];
    var lastRow = tab.sheet.getLastRow();
    var dataRowCount = lastRow - HEADER_ROWS;

    tabs.push({name: tab.name, sheetId: tab.sheet.getSheetId(), lastRow: lastRow});

    // Section header row
    var sectionRow = new Array(TOTAL_COLUMNS);
    for (var c = 0; c < TOTAL_COLUMNS; c++) sectionRow[c] = '';
    sectionRow[0] = tab.name;
    backupSheet.getRange(currentRow, 1, 1, TOTAL_COLUMNS).setValues([sectionRow]);

    // Format section header: bold, gray background, black bottom border
    var headerRange = backupSheet.getRange(currentRow, 1, 1, TOTAL_COLUMNS);
    headerRange.setFontWeight('bold');
    headerRange.setBackground('#f0f0f0');
    headerRange.setBorder(false, false, true, false, false, false, '#000000', SpreadsheetApp.BorderStyle.SOLID_MEDIUM);

    currentRow++;

    // Copy data rows (row 6+ from source), preserving anomaly highlight colors.
    // 異常標色 (赤/橙/黄系・重複・整行黄) は全て背景色。値と一緒に背景色も
    // コピーし、翌日社員が色を頼りに作業できるようにする。
    if (dataRowCount > 0) {
      var srcRange = tab.sheet.getRange(HEADER_ROWS + 1, 1, dataRowCount, TOTAL_COLUMNS);
      var data = srcRange.getValues();
      var backgrounds = srcRange.getBackgrounds();
      var destRange = backupSheet.getRange(currentRow, 1, dataRowCount, TOTAL_COLUMNS);
      destRange.setValues(data);
      destRange.setBackgrounds(backgrounds);
      currentRow += data.length;
    }

    // Add spacing between sections
    currentRow++;
  }

  SpreadsheetApp.flush();

  return {success: true, tabs: tabs};
}


/**
 * Delete the backed-up tabs from MF_Import_Data.
 * main.py _get_or_create_tab() will auto-recreate them with legend + headers.
 *
 * `tabs` is the [{name, sheetId, lastRow}] snapshot backupAllTabs_() took at
 * read time.
 *
 * Two-phase, all-or-nothing:
 *   Phase 1 (verify): re-fetch every tab by name and re-measure its
 *     sheetId + lastRow. Nothing is deleted here. Every mismatch is
 *     recorded in `skipped[]`, classified by cause (not found / sheetId
 *     changed / lastRow changed).
 *   Phase 2 (commit): if `skipped` is non-empty, delete NOTHING and return
 *     {deletedCount: 0, skipped, abortedAll: true}. Only when every single
 *     tab still matches its snapshot do we delete all of them.
 *
 * Why sheetId, not just name: if the source tab was deleted and a same-named
 * tab was recreated between backup and delete, getSheetByName() returns the
 * new tab. Comparing lastRow alone could false-match (same row count by
 * coincidence) and delete data that was never backed up. sheetId is
 * immutable per physical sheet, so comparing it rules that out.
 *
 * Why lastRow: catches the ordinary case — Python appended a row to the
 * *same* tab after backupAllTabs_() read it. That row exists in the live
 * sheet but not in today's backup snapshot; deleting the tab now would lose
 * it silently (no error, no trace).
 *
 * Why all-or-nothing instead of "just delete the tabs that are still clean"
 * (the previous, more "efficient"-looking behavior — DO NOT reintroduce it,
 * this is the exact bug this rewrite fixes): partial deletion creates a
 * dangerous straddle state across a same-day rerun of dailyBackupAndClear()
 * (manual retry, trigger retry, anything that fires it twice in one day):
 *   1. Run #1 backs up tabs A/B/C into today's backup sheet.
 *   2. Old partial-delete logic: A and B are unchanged so they get deleted;
 *      C changed underneath us so it is skipped.
 *   3. Run #2 fires the SAME day. backupAllTabs_() now only sees C in the
 *      source spreadsheet (A/B are already gone).
 *   4. backupAllTabs_()'s idempotent-rerun step ("delete existing sheet for
 *      today") DELETES today's backup sheet — the one that still held A's
 *      and B's backed-up rows — and rebuilds it containing ONLY C.
 *   5. A and B's backed-up rows are gone (overwritten in step 4) AND their
 *      source tabs are gone (deleted in step 2). Unrecoverable, and nothing
 *      about it looks like an error — deleteSourceTabs_() "succeeded" both
 *      times.
 * All-or-nothing removes the straddle: either every tab backed up this run
 * is still exactly what got backed up (safe to delete all of them), or at
 * least one changed and we delete NOTHING, so every source tab (and its
 * live data) survives intact for tomorrow's backup to pick up cleanly. The
 * cost is one extra day of latency on clearing ALL of today's tabs on the
 * rare day a mid-backup write lands in the window — far cheaper than
 * silently losing already-backed-up rows.
 *
 * KNOWN RESIDUAL RACE (still not eliminated by this function — see Plan
 * §3.5): Phase 1 (check) and Phase 2 (delete) are two separate passes over
 * `tabs`, so the gap between "we confirmed tab X is unchanged" and "we
 * actually call deleteSheet() on tab X" is no longer just a few
 * milliseconds for a single tab — it now spans however long it takes to
 * check (and, for earlier tabs, also delete) every OTHER tab in this run:
 *   1. Phase 1 re-measures tab A -> matches snapshot, no skip recorded
 *   2. Phase 1 re-measures tabs B, C, ... (elapsed time passes)
 *   3. Phase 2 begins deleting; before it reaches A, Python appends a row
 *      to A                                                    <- lost here
 *   4. Phase 2 calls deleteSheet() on A without re-checking it
 * That row is backed up nowhere and disappears with the tab, silently.
 * This is a DELIBERATE trade-off, not an oversight: re-checking each tab
 * immediately before its own deleteSheet() call (like the old single-pass
 * version did) would re-introduce the exact bug this rewrite exists to
 * close — a late change on one tab would skip only that tab while its
 * siblings still get deleted, recreating the partial-delete +
 * same-day-rerun data-loss path above. Phase 2 therefore deletes
 * unconditionally once Phase 1 has committed to "zero skips", trading a
 * wider (but still much narrower than backupAllTabs_()'s own read window)
 * race for a guarantee that the outcome is always all-or-nothing. A real
 * fix needs mutual exclusion between the Python writer and this backup
 * run, or a different deletion model (P2, out of scope for this change per
 * Plan §3.5/§7-3).
 *
 * Returns {deletedCount: number, skipped: [{name, reason}], abortedAll: boolean}
 */
function deleteSourceTabs_(tabs) {
  var source = SpreadsheetApp.openById(SOURCE_SS_ID);

  // Phase 1: verify only. Re-measure every tab; delete nothing yet.
  var skipped = [];

  for (var i = 0; i < tabs.length; i++) {
    var snapshot = tabs[i];
    var sheet = source.getSheetByName(snapshot.name);

    if (!sheet) {
      skipped.push({name: snapshot.name, reason: 'sheet not found (renamed or already deleted)'});
      continue;
    }

    var currentSheetId = sheet.getSheetId();
    var currentLastRow = sheet.getLastRow();

    if (currentSheetId !== snapshot.sheetId) {
      skipped.push({
        name: snapshot.name,
        reason: 'sheetId changed: backup=' + snapshot.sheetId + ' now=' + currentSheetId +
          ' (tab was recreated after backup read it)'
      });
      continue;
    }

    if (currentLastRow !== snapshot.lastRow) {
      skipped.push({
        name: snapshot.name,
        reason: 'lastRow changed: backup=' + snapshot.lastRow + ' now=' + currentLastRow +
          ' (data was written after backup read it)'
      });
      continue;
    }
  }

  // Phase 2: commit. All-or-nothing — see doc comment above for why a
  // "delete the clean ones, skip the rest" middle ground is NOT safe here.
  if (skipped.length > 0) {
    var lines = skipped.map(function(s) { return '  - ' + s.name + ': ' + s.reason; });
    Logger.log(
      'deleteSourceTabs_: ' + skipped.length + ' of ' + tabs.length + ' tab(s) changed since ' +
      'backup read them. Aborting deletion for ALL ' + tabs.length + ' tab(s) this run ' +
      '(all-or-nothing):\n' + lines.join('\n')
    );
    try {
      MailApp.sendEmail(
        Session.getActiveUser().getEmail(),
        'Super Scaner: Backup skipped deleting ALL tabs this run',
        'The following ' + skipped.length + ' tab(s) changed between backup read and delete:\n\n' +
          lines.join('\n') +
          '\n\nBecause of this, NONE of today\'s ' + tabs.length + ' backed-up tab(s) were deleted ' +
          '(all-or-nothing policy - all tabs are kept for tomorrow\'s backup, not just the changed ' +
          'ones).\n\nWhy not just delete the tabs that were still clean? Deleting some but not all ' +
          'creates a dangerous straddle if this backup is re-run later the same day: the re-run ' +
          'would only see the tabs still remaining in the source sheet, and its idempotent ' +
          '"delete existing sheet for today" step would overwrite today\'s backup sheet with only ' +
          'that partial data - silently losing the backup of whatever was already deleted. This is ' +
          'not itself data loss on its own - every source tab and its rows are still intact. See ' +
          'daily_backup.gs deleteSourceTabs_() comment for full details and the residual TOCTOU ' +
          'race this still does not cover.'
      );
    } catch (mailErr) {
      Logger.log('Failed to send skip-notification email: ' + mailErr.message);
    }
    return {deletedCount: 0, skipped: skipped, abortedAll: true};
  }

  // Every tab still matches its snapshot -> safe to delete all of them.

  // Ensure at least 1 sheet remains (Sheets API requirement)
  var allSheets = source.getSheets();
  var remainCount = allSheets.length - tabs.length;
  if (remainCount < 1) {
    // Keep _config or create a placeholder
    var configSheet = source.getSheetByName('_config');
    if (!configSheet) {
      source.insertSheet('_config');
    }
  }

  var deletedCount = 0;
  for (var j = 0; j < tabs.length; j++) {
    var sheetToDelete = source.getSheetByName(tabs[j].name);
    source.deleteSheet(sheetToDelete);
    deletedCount++;
  }

  SpreadsheetApp.flush();

  return {deletedCount: deletedCount, skipped: [], abortedAll: false};
}


/**
 * Delete backup sheets older than RETENTION_DAYS from MF_Backup.
 */
function monthlyCleanup() {
  var backup = SpreadsheetApp.openById(BACKUP_SS_ID);
  var sheets = backup.getSheets();
  var now = new Date();
  var deleted = 0;

  for (var i = sheets.length - 1; i >= 0; i--) {
    var name = sheets[i].getName();
    var date = parseDate_(name);
    if (!date) continue;

    var ageDays = Math.floor((now - date) / 86400000);
    if (ageDays > RETENTION_DAYS) {
      // Keep at least 1 sheet
      if (sheets.length - deleted <= 1) break;
      backup.deleteSheet(sheets[i]);
      deleted++;
      Logger.log('Deleted old backup: ' + name + ' (' + ageDays + ' days old)');
    }
  }

  if (deleted > 0) {
    Logger.log('Monthly cleanup: deleted ' + deleted + ' old backup sheets');
  }
}


/**
 * Parse yyyy-MM-dd string to Date. Returns null if invalid.
 */
function parseDate_(str) {
  var match = str.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) return null;
  var d = new Date(parseInt(match[1]), parseInt(match[2]) - 1, parseInt(match[3]));
  if (isNaN(d.getTime())) return null;
  return d;
}


/**
 * One-time setup: register daily and monthly triggers.
 * Run this manually from the Apps Script editor.
 */
function setupTriggers() {
  // Remove existing triggers to avoid duplicates
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    ScriptApp.deleteTrigger(triggers[i]);
  }

  // Daily backup at 22:00 JST
  ScriptApp.newTrigger('dailyBackupAndClear')
    .timeBased()
    .everyDays(1)
    .atHour(22)
    .inTimezone(TIMEZONE)
    .create();

  // Monthly cleanup on the 1st at 23:00 JST
  ScriptApp.newTrigger('monthlyCleanup')
    .timeBased()
    .onMonthDay(1)
    .atHour(23)
    .inTimezone(TIMEZONE)
    .create();

  Logger.log('Triggers registered: dailyBackupAndClear (22:00 JST daily), monthlyCleanup (1st of month 23:00 JST)');
}
