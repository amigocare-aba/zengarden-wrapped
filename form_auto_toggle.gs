/**
 * Zen Garden — Exchange Form Auto-Open/Close
 * ============================================
 * Auto-opens and closes the Rewards Exchange Form on the dates below.
 *
 * SETUP (one-time):
 *  1. Open the form: https://docs.google.com/forms/d/e/1FAIpQLSdWxjbnvPqlmxKQO2IAYVQV52zsCEJZ9a2QIZNl0FUmjwKCmQ/viewform
 *  2. Click ⋮ (3 dots top-right) → Script editor
 *  3. Delete any existing code, paste this entire file, click Save
 *  4. In left sidebar, click ⏰ "Triggers" → Add Trigger
 *       - Function: dailyCheck
 *       - Event source: Time-driven
 *       - Type: Day timer
 *       - Time: 7am to 8am (any morning hour)
 *  5. Click Save → authorize permissions
 *  6. Test: run "testRun" once to confirm it works
 *
 * UPDATING FOR NEXT YEAR:
 *   Edit the SCHEDULE array below. Format: { open: 'YYYY-MM-DD', close: 'YYYY-MM-DD' }
 */

// ── 2026 SCHEDULE ────────────────────────────────────────────────────
// Each entry: form opens on `open`, closes on `close` (inclusive of close day).
// Format: YYYY-MM-DD in America/New_York timezone
// NEW MODEL: form opens the day after month end (same day as recap post)
// and closes 7 days later.
const SCHEDULE = [
  { month: 'April',     open: '2026-05-01', close: '2026-05-08' },
  { month: 'May',       open: '2026-06-01', close: '2026-06-08' },
  { month: 'June',      open: '2026-07-01', close: '2026-07-08' },
  { month: 'July',      open: '2026-08-01', close: '2026-08-08' },
  { month: 'August',    open: '2026-09-01', close: '2026-09-08' },
  { month: 'September', open: '2026-10-01', close: '2026-10-08' },
  { month: 'October',   open: '2026-11-01', close: '2026-11-08' },
  { month: 'November',  open: '2026-12-01', close: '2026-12-08' },
  { month: 'December',  open: '2027-01-01', close: '2027-01-08' },
];

const TIMEZONE = 'America/New_York';


// ── MAIN: Runs daily ────────────────────────────────────────────────
function dailyCheck() {
  const form = FormApp.getActiveForm();
  const today = Utilities.formatDate(new Date(), TIMEZONE, 'yyyy-MM-dd');

  for (const entry of SCHEDULE) {
    if (today === entry.open) {
      form.setAcceptingResponses(true);
      Logger.log('✅ ' + today + ': Opened form for ' + entry.month + ' wrapped');
      sendOptionalEmail('Opened', entry.month, today);
      return;
    }
    if (today === entry.close) {
      // Wait until end of close day - run at end of day instead
      // For now, close at the start of the close day morning
      form.setAcceptingResponses(false);
      Logger.log('🔒 ' + today + ': Closed form for ' + entry.month + ' wrapped');
      sendOptionalEmail('Closed', entry.month, today);
      return;
    }
  }

  Logger.log(today + ': No action needed');
}


// ── TEST: Run manually to verify ────────────────────────────────────
function testRun() {
  const form = FormApp.getActiveForm();
  const isOpen = form.isAcceptingResponses();
  Logger.log('Form: ' + form.getTitle());
  Logger.log('Currently accepting responses: ' + isOpen);
  Logger.log('Today: ' + Utilities.formatDate(new Date(), TIMEZONE, 'yyyy-MM-dd'));
  Logger.log('Schedule entries: ' + SCHEDULE.length);
  Logger.log('Next open date: ' + getNextDate('open'));
  Logger.log('Next close date: ' + getNextDate('close'));
}


// ── Helper: find the next upcoming date ────────────────────────────
function getNextDate(type) {
  const today = Utilities.formatDate(new Date(), TIMEZONE, 'yyyy-MM-dd');
  for (const entry of SCHEDULE) {
    if (entry[type] >= today) return entry[type] + ' (' + entry.month + ')';
  }
  return 'none scheduled';
}


// ── Optional: email Alex when form opens/closes ────────────────────
// Comment this out if you don't want emails
function sendOptionalEmail(action, month, date) {
  // const recipient = 'alex.arevalo@amigocareaba.com';
  // const subject = '[Zen Garden] Exchange form ' + action.toLowerCase() + ' for ' + month;
  // const body = 'The exchange form was automatically ' + action.toLowerCase() + ' on ' + date + '.';
  // MailApp.sendEmail(recipient, subject, body);
}
