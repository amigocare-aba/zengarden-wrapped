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
 * ---------------------------------------------------------------------
 * ONE-TIME SETUP (~5 minutes):
 *   1. Open the form in EDIT mode → ⋮ (top right) → Script editor.
 *      (This REPLACES the old "Auto-Open/Close" schedule script that
 *      lives there — select all of its code and delete it.)
 *   2. Paste this whole file. Save (Cmd+S).
 *      Bound this way, the script finds the form by itself — you can
 *      leave FORM_ID untouched.
 *   3. Project Settings (gear icon) → confirm time zone is
 *      "(GMT-05:00) Eastern Time".
 *   4. In the toolbar, select the function `reopenNow` → Run → approve
 *      the permissions prompt. This reopens the form immediately
 *      (fixes the "form isn't available" problem same-day).
 *   5. Select the function `setup` → Run. This installs the monthly
 *      triggers AND removes the old script's trigger if one exists.
 *
 * (Alternative: paste into a standalone project at script.google.com —
 *  then you must also set FORM_ID to the long id from the form's edit
 *  URL, docs.google.com/forms/d/THIS_LONG_ID/edit.)
 *
 * To verify later: Triggers (clock icon) should show reopenForm (day 1)
 * and closeFormAndEmail (day 9) — and NOT dailyCheck. Emails come from
 * whoever ran setup.
 * ---------------------------------------------------------------------
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
  var m = wrappedMonthName();
  MailApp.sendEmail({
    to: EMAIL_TO,
    subject: '🌱 Zen Garden exchange form is OPEN — ' + m + ' redemptions',
    body: 'The point exchange form just reopened for ' + m + ' redemptions.\n\n' +
      'It closes automatically at the end of the day on the ' + DEADLINE_DAY +
      'th, and you’ll get a follow-up email with the submission count.\n\n' +
      'Form (what the team fills out): ' + form.getPublishedUrl() + '\n' +
      'Responses sheet (for HR, fills up live): ' + responsesLink(form) + '\n\n' +
      '(automated — Zen Garden Exchange script)'
  });
  Logger.log('✓ Form reopened for ' + m);
}

/** Day 9, 12–1am: close the form and tell HR what came in. */
function closeFormAndEmail() {
  var form = getForm();
  form.setCustomClosedMessage(
    'The exchange window for this month has closed 🌱 ' +
    'It reopens on the 1st with the next Wrapped. Questions? Message Shelby or HR.'
  );
  form.setAcceptingResponses(false);

  var now = new Date();
  var windowStart = new Date(now.getFullYear(), now.getMonth(), 1); // the 1st, 00:00
  var count = form.getResponses().filter(function (r) {
    return r.getTimestamp() >= windowStart;
  }).length;

  var m = wrappedMonthName();
  MailApp.sendEmail({
    to: EMAIL_TO,
    subject: '✅ Zen Garden exchange form CLOSED — ' + count + ' ' + m + ' submission' +
      (count === 1 ? '' : 's') + ' ready for payroll',
    body: 'The ' + m + ' point exchange window (1st–' + DEADLINE_DAY + 'th) just closed.\n\n' +
      'Submissions this window: ' + count + '\n' +
      'Review them here: ' + responsesLink(form) + '\n\n' +
      'Bonuses go on the following paycheck.\n\n' +
      '(automated — Zen Garden Exchange script)'
  });
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
/** Reopen the form right now (e.g. it was left closed mid-window). */
function reopenNow() {
  getForm().setAcceptingResponses(true);
  Logger.log('✓ Form is now OPEN: ' + getForm().getPublishedUrl());
}
/** Close the form right now and send HR the summary email. */
function closeNow() {
  closeFormAndEmail();
}
