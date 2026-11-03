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
 * ONE-TIME SETUP (~5 minutes, do this signed in as alex.arevalo@):
 *   1. Open the form in EDIT mode (you need edit access — if Shelby owns
 *      it, have her share it with you as collaborator).
 *      Copy the ID from the edit URL:
 *      https://docs.google.com/forms/d/THIS_LONG_ID/edit
 *   2. Go to https://script.google.com → New project → paste this whole
 *      file, replacing the default code. Name it "Zen Garden Exchange".
 *   3. Paste the ID into FORM_ID below.
 *   4. Project Settings (gear icon) → confirm time zone is
 *      "(GMT-05:00) Eastern Time".
 *   5. In the toolbar, select the function `reopenNow` → Run → approve
 *      the permissions prompt. This reopens the form immediately
 *      (fixes the "form isn't available" problem same-day).
 *   6. Select the function `setup` → Run. This installs the monthly
 *      triggers. Done — it runs itself from now on.
 *
 * To verify later: Triggers (clock icon) should show reopenForm (day 1)
 * and closeFormAndEmail (day 9). Emails come from whoever ran setup.
 * ---------------------------------------------------------------------
 */

// ⚙️ CONFIG ------------------------------------------------------------
var FORM_ID = 'PASTE_FORM_EDIT_ID_HERE';
var EMAIL_TO = 'hr@amigocareaba.com,alex.arevalo@amigocareaba.com';
var DEADLINE_DAY = 8;              // keep in sync with weekly_config.json
var NOTIFY_EACH_SUBMISSION = false; // true = HR also gets an email per submission
// ----------------------------------------------------------------------

/** Run once after pasting: installs the monthly triggers (idempotent). */
function setup() {
  var handlers = ['reopenForm', 'closeFormAndEmail', 'onNewSubmission'];
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
      'th, and you’ll get a follow-up email with the submission count and the responses link.\n\n' +
      'Form: ' + form.getPublishedUrl() + '\n\n' +
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

/** Link HR should check: the linked response sheet if one exists, else the form's Responses tab. */
function responsesLink(form) {
  try {
    var destId = form.getDestinationId();
    if (destId) return 'https://docs.google.com/spreadsheets/d/' + destId;
  } catch (err) {}
  return form.getEditUrl() + '#responses';
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
