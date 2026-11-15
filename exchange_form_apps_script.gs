/**
 * Zen Garden — Exchange Form automation (Google Apps Script)
 * ===========================================================
 * Owns the part of the monthly cycle that GitHub Actions can't touch:
 * opening/closing the Google Form and emailing HR.
 *
 * Monthly cycle (deadline day = 8, must match "exchange_deadline_day"
 * in weekly_config.json in the zengarden-wrapped repo):
 *   Day 1,  6–7am ET  → form REOPENS  (Wrapped post at noon links to it)
 *   Day 8,  noon ET   → Gardy posts last-call reminder in #zengarden
 *                       (that part lives in zen_garden_weekly_auto.py)
 *   Day 9,  12–1am ET → form CLOSES + email to HR & Alex with the
 *                       submission count and the responses link
 *
 * Emails are branded HTML cards (plain-text fallback included).
 * Run sendTestEmail to preview both designs — it emails only YOU.
 */

// ⚙️ CONFIG ------------------------------------------------------------
var FORM_ID = 'PASTE_FORM_EDIT_ID_HERE';
var EMAIL_TO = 'hr@amigocareaba.com,alex.arevalo@amigocareaba.com';
var DEADLINE_DAY = 8;              // keep in sync with weekly_config.json
var NOTIFY_EACH_SUBMISSION = false; // true = HR also gets an email per submission
// Exact responses tab HR reviews (overrides auto-detection, which can't pick the right tab):
var RESPONSES_URL = 'https://docs.google.com/spreadsheets/d/1xdi8dhIRpUIsKFcPb27pi2WVO5eQt3-fmYoWl9g5hNs/edit?resourcekey=&gid=7713254#gid=7713254';
// ----------------------------------------------------------------------

/** Run once after pasting: installs the monthly triggers (idempotent). */
function setup() {
  // 'dailyCheck' = the legacy 2026 schedule script this replaces; purge its trigger too.
  var handlers = ['reopenForm', 'closeFormAndEmail', 'onNewSubmission', 'dailyCheck'];
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (handlers.indexOf(t.getHandlerFunction()) !== -1) ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('reopenForm').timeBased().onMonthDay(1).atHour(6).create();
  ScriptApp.newTrigger('closeFormAndEmail').timeBased().onMonthDay(DEADLINE_DAY + 1).atHour(0).create();
  if (NOTIFY_EACH_SUBMISSION) {
    ScriptApp.newTrigger('onNewSubmission').forForm(getForm()).onFormSubmit().create();
  }
  Logger.log('✓ Triggers installed: reopen day 1 (6am), close day ' + (DEADLINE_DAY + 1) +
    ' (12am)' + (NOTIFY_EACH_SUBMISSION ? ', plus per-submission emails' : ''));
}

function getForm() {
  try {
    var bound = FormApp.getActiveForm();
    if (bound) return bound;
  } catch (err) {}
  return FormApp.openById(FORM_ID);
}

/** Day 1: reopen the form for the month that just wrapped. */
function reopenForm() {
  var form = getForm();
  form.setAcceptingResponses(true);
  var mail = buildOpenEmail_(form);
  MailApp.sendEmail({ to: EMAIL_TO, subject: mail.subject, body: mail.body, htmlBody: mail.html });
  Logger.log('✓ Form reopened for ' + wrappedMonthName());
}

/** Day 9, 12–1am: close the form and tell HR what came in. */
function closeFormAndEmail() {
  var form = getForm();
  form.setAcceptingResponses(false);
  // Cosmetic only — newer Forms don't support it, and it must never block the close+email.
  try {
    form.setCustomClosedMessage(
      'The exchange window for this month has closed 🌱 ' +
      'It reopens on the 1st with the next Wrapped. Questions? Message Shelby or HR.'
    );
  } catch (err) {
    Logger.log('(custom closed message not supported on this form — skipped)');
  }

  var now = new Date();
  var windowStart = new Date(now.getFullYear(), now.getMonth(), 1); // the 1st, 00:00
  var count = form.getResponses().filter(function (r) {
    return r.getTimestamp() >= windowStart;
  }).length;

  var mail = buildCloseEmail_(form, count);
  MailApp.sendEmail({ to: EMAIL_TO, subject: mail.subject, body: mail.body, htmlBody: mail.html });
  Logger.log('✓ Form closed, ' + count + ' submissions, email sent to ' + EMAIL_TO);
}

/** Optional per-submission ping (enable via NOTIFY_EACH_SUBMISSION + rerun setup). */
function onNewSubmission(e) {
  var who = '';
  try { who = e.response.getRespondentEmail() || ''; } catch (err) {}
  MailApp.sendEmail({
    to: EMAIL_TO,
    subject: '🌱 New Zen Garden exchange submission' + (who ? ' — ' + who : ''),
    body: 'A new exchange form submission just came in' + (who ? ' from ' + who : '') + '.\n\n' +
      'All responses: ' + responsesLink(getForm()) + '\n\n' +
      '(automated — Zen Garden Exchange script)'
  });
}

// ── Email builders ────────────────────────────────────────────────────

/** The "form is OPEN" email (sent the 1st). */
function buildOpenEmail_(form) {
  var m = wrappedMonthName();
  var win = windowLabel_();
  var formUrl = form.getPublishedUrl();
  var sheetUrl = responsesLink(form);
  return {
    subject: '🌱 Zen Garden exchange form is OPEN — ' + m + ' redemptions',
    body: 'The point exchange form just reopened for ' + m + ' redemptions.\n\n' +
      'It closes automatically at the end of the day on the ' + DEADLINE_DAY + 'th.\n\n' +
      'Form (what the team fills out): ' + formUrl + '\n' +
      'Responses sheet (for HR, fills up live): ' + sheetUrl + '\n\n' +
      '(automated — Zen Garden Exchange script)',
    html: emailCard_({
      chip: 'FORM OPEN', chipBg: '#9FE1CB', chipColor: '#04342C',
      headline: m + ' redemptions are open',
      headlineSize: '24px',
      subline: 'Cash in your Zen Garden points — closes end of day on the ' + DEADLINE_DAY + 'th.',
      rows: [['Month redeemed', m], ['Window', win], ['Closes', 'Midnight on the ' + DEADLINE_DAY + 'th']],
      buttonText: 'Open the exchange form', buttonUrl: formUrl,
      secondaryText: 'Watch submissions arrive (responses sheet) →', secondaryUrl: sheetUrl,
      note: 'You’ll get a follow-up email with the final submission count when the window closes.',
      footer: 'closes automatically at midnight on the ' + DEADLINE_DAY + 'th'
    })
  };
}

/** The "form is CLOSED — payroll" email (sent the 9th). */
function buildCloseEmail_(form, count) {
  var m = wrappedMonthName();
  var win = windowLabel_();
  var sheetUrl = responsesLink(form);
  var plural = count === 1 ? '' : 's';
  return {
    subject: '✅ Zen Garden exchange form CLOSED — ' + count + ' ' + m + ' submission' + plural + ' ready for payroll',
    body: 'The ' + m + ' point exchange window (' + win + ') just closed.\n\n' +
      'Submissions this window: ' + count + '\n' +
      'Review them here: ' + sheetUrl + '\n\n' +
      'Bonuses go on the following paycheck.\n\n' +
      '(automated — Zen Garden Exchange script)',
    html: emailCard_({
      chip: 'FORM CLOSED', chipBg: '#ffffff', chipColor: '#0F6E56',
      headline: count + ' submission' + plural,
      headlineSize: '30px',
      subline: 'The ' + m + ' exchange window has closed — ready for payroll.',
      rows: [['Month redeemed', m], ['Window', win], ['Bonuses land', 'Next paycheck']],
      buttonText: 'Review the ' + count + ' submission' + plural, buttonUrl: sheetUrl,
      note: 'Bonuses go on the following paycheck. 💸',
      footer: 'the form reopens on the 1st'
    })
  };
}

/** Shared branded HTML card. Inline styles only (email clients ignore everything else). */
function emailCard_(o) {
  var rows = (o.rows || []).map(function (r, i) {
    var line = i === 0 ? '' : 'border-top:1px solid #f3f4f6;';
    return '<tr>' +
      '<td style="padding:8px 0;color:#6b7280;font-size:13px;' + line + '">' + r[0] + '</td>' +
      '<td style="padding:8px 0;color:#111827;font-size:13px;font-weight:700;text-align:right;' + line + '">' + r[1] + '</td>' +
      '</tr>';
  }).join('');
  var secondary = o.secondaryText
    ? '<p style="margin:14px 0 0;font-size:13px;"><a href="' + o.secondaryUrl + '" style="color:#0F6E56;font-weight:600;text-decoration:none;">' + o.secondaryText + '</a></p>'
    : '';
  return '<div style="background:#f3f4f1;padding:28px 12px;font-family:Arial,Helvetica,sans-serif;">' +
    '<div style="max-width:520px;margin:0 auto;background:#ffffff;border-radius:14px;border:1px solid #e5e7eb;overflow:hidden;">' +
      '<table style="width:100%;border-collapse:collapse;background:#0F6E56;"><tr>' +
        '<td style="padding:16px 26px;color:#ffffff;font-size:14px;font-weight:700;letter-spacing:1.5px;">🌱 ZEN GARDEN</td>' +
        '<td style="padding:16px 26px;text-align:right;"><span style="background:' + o.chipBg + ';color:' + o.chipColor + ';font-size:10.5px;font-weight:700;letter-spacing:1px;padding:4px 12px;border-radius:999px;">' + o.chip + '</span></td>' +
      '</tr></table>' +
      '<div style="padding:26px;">' +
        '<p style="margin:0;font-size:' + (o.headlineSize || '26px') + ';font-weight:700;color:#111827;">' + o.headline + '</p>' +
        '<p style="margin:6px 0 18px;font-size:14px;color:#6b7280;">' + o.subline + '</p>' +
        '<table style="width:100%;border-collapse:collapse;border-top:1px solid #e5e7eb;">' + rows + '</table>' +
        '<div style="margin:22px 0 6px;"><a href="' + o.buttonUrl + '" style="display:inline-block;background:#1D9E75;color:#ffffff;font-size:14px;font-weight:700;text-decoration:none;padding:12px 26px;border-radius:999px;">' + o.buttonText + '</a></div>' +
      secondary +
      '<p style="margin:16px 0 0;font-size:13px;color:#374151;">' + o.note + '</p>' +
      '</div>' +
      '<div style="padding:12px 26px;background:#fafaf8;border-top:1px solid #eef0ec;font-size:11px;color:#9ca3af;">Automated · Zen Garden Exchange script · ' + o.footer + '</div>' +
    '</div>' +
  '</div>';
}

/** "Nov 1 – Nov 8" for the current month's window. */
function windowLabel_() {
  var mon = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'MMM');
  return mon + ' 1 – ' + mon + ' ' + DEADLINE_DAY;
}

// ── Helpers ───────────────────────────────────────────────────────────

/** Link HR should check: the exact configured tab, else the linked sheet, else the form's Responses tab. */
function responsesLink(form) {
  if (RESPONSES_URL) return RESPONSES_URL;
  try {
    var destId = form.getDestinationId();
    if (destId) return 'https://docs.google.com/spreadsheets/d/' + destId;
  } catch (err) {}
  return form.getEditUrl() + '#responses';
}

/** Harmless check: logs the links the emails will use (does NOT open/close anything). */
function previewLinks() {
  var form = getForm();
  Logger.log('Form (team fills out): ' + form.getPublishedUrl());
  Logger.log('Responses (HR reviews): ' + responsesLink(form));
}

/** Name of the month being redeemed (the one that just ended). */
function wrappedMonthName() {
  var now = new Date();
  var prev = new Date(now.getFullYear(), now.getMonth() - 1, 1);
  return Utilities.formatDate(prev, Session.getScriptTimeZone(), 'MMMM');
}

// 🔧 Manual helpers — run from the toolbar any time:

/** Preview BOTH email designs — sends them to YOU ONLY (not HR), subjects prefixed [TEST]. */
function sendTestEmail() {
  var me = Session.getActiveUser().getEmail() || 'alex.arevalo@amigocareaba.com';
  var form = getForm();
  var open = buildOpenEmail_(form);
  var close = buildCloseEmail_(form, 9);
  MailApp.sendEmail({ to: me, subject: '[TEST] ' + open.subject, body: open.body, htmlBody: open.html });
  MailApp.sendEmail({ to: me, subject: '[TEST] ' + close.subject, body: close.body, htmlBody: close.html });
  Logger.log('✓ Two [TEST] emails sent to ' + me + ' only');
}

/** Reopen the form right now (e.g. it was left closed mid-window). */
function reopenNow() {
  getForm().setAcceptingResponses(true);
  Logger.log('✓ Form is now OPEN: ' + getForm().getPublishedUrl());
}

/** Close the form right now and send HR the summary email. */
function closeNow() {
  closeFormAndEmail();
}
