// ============================================================
// Super Scaner - Daily Backup & Clear (GAS)
// Deploy: MF_Import_Data > Extensions > Apps Script
// Run setupTriggers() once to register automated triggers
// ============================================================

const SOURCE_SS_ID = '1-cIp_TEGbUE3Z-j45ApXewTPCB1X_O32yI5bekqHhn4';
const BACKUP_SS_ID = '1K8sTStUjWmrM9SQUlxcupMIswtff6pb3jyAsm0gfhIo';
const TOTAL_COLUMNS = 28;
const RETENTION_DAYS = 30;
const TIMEZONE = 'Asia/Tokyo';
const SKIP_TABS = ['_config'];
const HEADER_ROWS = 6;


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
      deleteSourceTabs_(result.tabNames);
      Logger.log('Backup and clear completed: ' + result.tabNames.length + ' tabs');
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
 * Returns {success: boolean, tabNames: string[]}
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
    return {success: false, tabNames: []};
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

  for (var t = 0; t < dataTabs.length; t++) {
    var tab = dataTabs[t];
    var lastRow = tab.sheet.getLastRow();
    var dataRowCount = lastRow - HEADER_ROWS;

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

    // Copy data rows (row 6+ from source)
    if (dataRowCount > 0) {
      var data = tab.sheet.getRange(HEADER_ROWS + 1, 1, dataRowCount, TOTAL_COLUMNS).getValues();
      backupSheet.getRange(currentRow, 1, data.length, TOTAL_COLUMNS).setValues(data);
      currentRow += data.length;
    }

    // Add spacing between sections
    currentRow++;
  }

  SpreadsheetApp.flush();

  var tabNames = dataTabs.map(function(t) { return t.name; });
  return {success: true, tabNames: tabNames};
}


/**
 * Delete the backed-up tabs from MF_Import_Data.
 * main.py _get_or_create_tab() will auto-recreate them with legend + headers.
 */
function deleteSourceTabs_(tabNames) {
  var source = SpreadsheetApp.openById(SOURCE_SS_ID);
  var allSheets = source.getSheets();

  // Ensure at least 1 sheet remains (Sheets API requirement)
  var remainCount = allSheets.length - tabNames.length;
  if (remainCount < 1) {
    // Keep _config or create a placeholder
    var configSheet = source.getSheetByName('_config');
    if (!configSheet) {
      source.insertSheet('_config');
    }
  }

  for (var i = 0; i < tabNames.length; i++) {
    var sheet = source.getSheetByName(tabNames[i]);
    if (sheet) {
      source.deleteSheet(sheet);
    }
  }

  SpreadsheetApp.flush();
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
