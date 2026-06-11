/**
 * Amazon 会員登録自動化 - Google Apps Script (amazon-regist-pyautogui 用)
 *
 * スプレッドシート列（1行目はヘッダー、2行目以降がデータ）:
 *   A:email | B:password | C:name | D:phone_num | E:status
 *
 * - 国番号は日本固定（+81）のため列に持たない。
 * - 処理日時は status 文字列に内包して記入する（例: 完了 (2026/06/11 12:34)）。
 *   → 別の timestamp 列は持たない。
 * - status 列はデータ列の直後（startCol + numCols）に自動算出される。
 *   既定 startCol=A(1) / numCols=4 → status は E 列。
 *
 * デプロイ: 「デプロイ」→「新しいデプロイ」→ 種類=ウェブアプリ /
 *           実行者=自分 / アクセス権=全員 → URL を config.yaml の gas_webapp_url に設定。
 */

function doPost(e) {
  let result;
  try {
    const params = e.parameter || {};
    switch (params.action) {
      case 'ping':                  result = { ok: true, message: 'pong' }; break;
      case 'fetchAccounts':         result = fetchAccounts(params); break;
      case 'recordResult':          result = recordResult(params); break;
      case 'getIncompleteAccounts': result = getIncompleteAccounts(params); break;
      default: result = { ok: false, error: 'Unknown action: ' + params.action };
    }
  } catch (error) {
    result = { ok: false, error: error.message };
  }
  return ContentService
    .createTextOutput(JSON.stringify(result))
    .setMimeType(ContentService.MimeType.JSON);
}

function getSheet_(sheetName) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  return ss.getSheetByName(sheetName) || ss.getActiveSheet();
}

// 列文字 → 数値 (A=1, B=2, ...)
function columnLetterToNumber(letter) {
  let col = 0;
  for (let i = 0; i < letter.length; i++) {
    col = col * 26 + letter.charCodeAt(i) - 64;
  }
  return col;
}

// 日時を yyyy/MM/dd HH:mm 形式に整形
function formatTimestamp(date) {
  const tz = Session.getScriptTimeZone();
  return Utilities.formatDate(date, tz, 'yyyy/MM/dd HH:mm');
}

// アカウント取得（A〜D の 4 列をデフォルト）
function fetchAccounts(params) {
  const sheetName = params.sheetName || 'Sheet1';
  const startColLetter = params.startColLetter || 'A';
  const numCols = parseInt(params.numCols, 10) || 4;

  const sheet = getSheet_(sheetName);
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return { ok: true, accounts: [] };

  const startCol = columnLetterToNumber(startColLetter);
  const values = sheet.getRange(2, startCol, lastRow - 1, numCols).getValues();
  const accounts = values.filter(row => row[0] && String(row[0]).trim() !== '');
  return { ok: true, accounts: accounts };
}

// 結果記録: status 列 = startCol + numCols に「status (日時)」を書込み
function recordResult(params) {
  const sheetName = params.sheetName || 'Sheet1';
  const startColLetter = params.startColLetter || params.emailColLetter || 'A';
  const emailColLetter = params.emailColLetter || 'A';
  const numCols = parseInt(params.numCols, 10) || 4;
  const email = params.email;
  const status = params.status;

  if (!email) return { ok: false, error: 'Email is required' };

  const sheet = getSheet_(sheetName);
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return { ok: false, error: 'No data in sheet' };

  const emailCol = columnLetterToNumber(emailColLetter);
  const statusCol = columnLetterToNumber(startColLetter) + numCols;

  const emails = sheet.getRange(2, emailCol, lastRow - 1, 1).getValues();
  for (let i = 0; i < emails.length; i++) {
    if (String(emails[i][0]).trim().toLowerCase() === String(email).trim().toLowerCase()) {
      const statusWithTime = status + ' (' + formatTimestamp(new Date()) + ')';
      sheet.getRange(i + 2, statusCol).setValue(statusWithTime);
      return { ok: true };
    }
  }
  return { ok: false, error: 'Email not found: ' + email };
}

// 未完了アカウント取得: status（日時内包）に「完了 / complete / ✓」を含まない行
function getIncompleteAccounts(params) {
  const sheetName = params.sheetName || 'Sheet1';
  const startColLetter = params.startColLetter || 'A';
  const numCols = parseInt(params.numCols, 10) || 4;

  const sheet = getSheet_(sheetName);
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return { ok: true, accounts: [] };

  const startCol = columnLetterToNumber(startColLetter);
  // データ numCols 列 + status 1 列 をまとめて取得
  const values = sheet.getRange(2, startCol, lastRow - 1, numCols + 1).getValues();

  const accounts = values
    .filter(row => {
      const email = String(row[0]).trim();
      const status = String(row[numCols]).trim().toLowerCase();
      const done = status.indexOf('完了') >= 0
        || status.indexOf('complete') >= 0
        || status.indexOf('✓') >= 0;
      return email && !done;
    })
    .map(row => ({
      email: String(row[0]).trim(),
      password: String(row[1]).trim(),
      name: String(row[2]).trim(),
      phone_num: String(row[3]).trim(),
      status: String(row[numCols]).trim(),
    }));

  return { ok: true, accounts: accounts };
}
