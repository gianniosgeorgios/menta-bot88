# Menta 88 – "Η λέξη που σε ταξιδεύει" bot

Runs free on GitHub Actions. Records the stream around 08/10/12/14/16/18/20 (Athens),
transcribes with Whisper, extracts the word, fills the Google Form, alerts you on Telegram.
Every run uploads timestamped MP3 chunks + JSON transcripts as a downloadable artifact.

## Setup
1. Create a **public** GitHub repo (public = unlimited free minutes) and push this folder.
2. Telegram: talk to @BotFather -> /newbot -> copy the token. Send any message to your bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
3. Repo -> Settings -> Secrets and variables -> Actions -> **Secrets**:
   `EMAIL, SURNAME, FIRST_NAME, GENDER (MALE/FEMALE/OTHER), AGE, PHONE, AREA, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID`
   (the form is in Greek, so e.g. SURNAME=ΓΙΑΝΝΙΟΣ, FIRST_NAME=ΓΙΩΡΓΟΣ, AREA=ΒΟΛΟΣ is a good idea).
4. Actions tab -> **Test stream (manual)** -> Run workflow. Check Telegram + the artifact:
   audio recorded? Greek transcript sensible? "Form mapping OK"?
5. The contest workflow runs on schedule in **DRY_RUN** mode (nothing submitted, you just get alerts).
6. After you've read the contest rules and are happy with accuracy:
   Settings -> Variables -> New variable `DRY_RUN` = `false`.

## Tuning (repo Variables)
- `WHISPER_MODEL`: `small` (default) or `medium` (more accurate for Greek, slower).
- Env `CONFIRMATIONS` (default 2): how many overlapping windows must hear the same word before submitting.
  Set to 1 for fastest submission, at the cost of more risk of a misheard word.
- Disable: Actions -> Menta 88 contest -> ... -> Disable workflow.

## Notes
- GitHub cron can run 5-30 min late; runs start 10 min before the hour and listen until 12 min after.
- Duplicate protection: `state/submitted.json` records submitted slots.
- Never put personal details in the code or repo files - only in Secrets.
