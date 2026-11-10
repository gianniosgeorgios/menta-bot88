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
4. Actions tab -> **Menta 88 - announcement recording** -> Run workflow to test. This records around the next announcement only if it is within 15 minutes. Check Telegram and download the artifact.
5. The scheduled workflow checks hourly and records a two-minute window (one minute before through one minute after) when the next 08:00/10:00/12:00/14:00/16:00/18:00/20:00 Athens announcement is near. It sends the MP3 and full Greek transcription to Telegram.
6. Add only these two repository secrets for this recording workflow: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. The older form-submission workflow may need the personal-information secrets listed below.
7. This recording workflow does not submit the Google Form.

## Recording timestamps
Each recording folder contains an MP3 and a JSON file. The JSON stores the scheduled announcement time, scheduled and actual recording start/end times in Athens, UTC start/end, monotonic measured duration, start/end offsets, transcript, and extracted candidate word. Actual timestamps refer to the GitHub runner capturing the stream; they are not proof of the presenter's exact transmission time because internet-radio buffering can introduce delay. GitHub Actions scheduled jobs can also start late, so inspect the recorded start offset in Telegram/JSON.

## Tuning (repo Variables)
- `WHISPER_MODEL`: `small` (default) or `medium` (more accurate for Greek, slower).
- Env `CONFIRMATIONS` (default 2): how many overlapping windows must hear the same word before submitting.
  Set to 1 for fastest submission, at the cost of more risk of a misheard word.
- Disable: Actions -> Menta 88 contest -> ... -> Disable workflow.

## Notes
- GitHub cron can run 5-30 min late; runs start 10 min before the hour and listen until 12 min after.
- Duplicate protection: `state/submitted.json` records submitted slots.
- Never put personal details in the code or repo files - only in Secrets.
