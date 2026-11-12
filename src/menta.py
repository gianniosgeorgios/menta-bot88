#!/usr/bin/env python3
"""Menta 88 - "Η λέξη που σε ταξιδεύει" bot.

Commands:
  gate  -> decides (for GitHub Actions) whether this run falls inside a slot window
  run   -> records the stream around the current slot, transcribes, extracts the
           word, submits the Google Form, sends Telegram alerts
  test  -> 30s recording + transcription + form-mapping check (nothing submitted)
  window -> records 60s before through 60s after the next scheduled announcement
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Athens")
SLOT_HOURS = [8, 10, 12, 14, 16, 18, 20]
STREAM_URL = os.environ.get("STREAM_URL") or "https://netradio.live24.gr/menta88ath"
FORM_ID = "1FAIpQLScs7WM7jjaeZXNnOfrcOJhj4OfOksyB-V8DVa0M5T7xgRimJw"
FORM_VIEW = f"https://docs.google.com/forms/d/e/{FORM_ID}/viewform"
FORM_POST = f"https://docs.google.com/forms/d/e/{FORM_ID}/formResponse"

MINUTES_AFTER = int(os.environ.get("MINUTES_AFTER", "12"))   # keep listening this long after the hour
GATE_BEFORE = 20                                             # a run may start up to 20 min before the hour
CHUNK = int(os.environ.get("CHUNK_SECONDS", "20"))
CONFIRMATIONS = int(os.environ.get("CONFIRMATIONS", "2"))    # windows that must agree before submitting
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")

STATE = Path("state/submitted.json")
REC = Path("recordings")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
PROMPT = "Η λέξη που σε ταξιδεύει είναι:"


# ----------------------------------------------------------------- helpers
def now():
    return datetime.now(TZ)


def log(*a):
    print(f"[{now().strftime('%H:%M:%S')}]", *a, flush=True)


def strip_acc(s):
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return s.lower().replace("ς", "σ")


def current_slot(t=None):
    t = t or now()
    cands = []
    for d in (-1, 0, 1):
        day = (t + timedelta(days=d)).date()
        for h in SLOT_HOURS:
            cands.append(datetime(day.year, day.month, day.day, h, tzinfo=TZ))
    return min(cands, key=lambda s: abs((s - t).total_seconds()))



def next_slot_within(minutes=15):
    """Return the next scheduled Athens slot if it is within `minutes`."""
    t = now()
    candidates = []
    for d in (0, 1):
        day = (t + timedelta(days=d)).date()
        for h in SLOT_HOURS:
            slot = datetime(day.year, day.month, day.day, h, 0, 0, tzinfo=TZ)
            if slot > t:
                candidates.append(slot)
    if not candidates:
        return None
    slot = min(candidates)
    delta = (slot - t).total_seconds()
    return slot if 0 < delta <= minutes * 60 else None


def iso_ms(dt):
    return dt.isoformat(timespec="milliseconds")


def telegram_window_result(message, audio_path):
    """Send result text and the complete MP3 as a Telegram audio attachment."""
    tg(message)
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    if not Path(audio_path).exists():
        return
    try:
        import requests
        with open(audio_path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendAudio",
                data={"chat_id": chat, "title": Path(audio_path).name,
                      "caption": "Complete 2-minute recording (Athens time)."},
                files={"audio": f}, timeout=120)
        r.raise_for_status()
    except Exception as e:
        log("Telegram audio upload failed:", e)


def slot_id(s):
    return s.strftime("%Y-%m-%dT%H")


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2))


def tg(text, audio=None):
    """Telegram notification (silently skipped if not configured)."""
    log("TG:", text)
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text[:4000]}, timeout=30)
        if audio and Path(audio).exists():
            with open(audio, "rb") as f:
                requests.post(f"https://api.telegram.org/bot{token}/sendAudio",
                              data={"chat_id": chat}, files={"audio": f}, timeout=60)
    except Exception as e:
        log("Telegram failed:", e)


# --------------------------------------------------------------- recording
def start_recorder(outdir, stamp, seconds, chunk):
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-user_agent", UA,
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-i", STREAM_URL,
        "-t", str(seconds), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libmp3lame", "-b:a", "48k",
        "-f", "segment", "-segment_time", str(chunk), "-reset_timestamps", "1",
        str(outdir / f"{stamp}_chunk%03d.mp3"),
    ]
    log("ffmpeg:", " ".join(cmd))
    return subprocess.Popen(cmd)


def make_window(prev, cur, outdir):
    """prev+cur concatenated, so a phrase cut at a chunk boundary is still heard whole."""
    if prev is None:
        return cur
    wdir = outdir / "windows"
    wdir.mkdir(exist_ok=True)
    out = wdir / f"win_{cur.stem}.mp3"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", f"concat:{prev}|{cur}", "-c", "copy", str(out)])
    return out if r.returncode == 0 and out.exists() else cur


# ----------------------------------------------------------- transcription
_model = None


def get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        log("Loading Whisper model", WHISPER_MODEL)
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model


def transcribe(path):
    segments, _ = get_model().transcribe(
        str(path), language="el", beam_size=5,
        initial_prompt=PROMPT, condition_on_previous_text=False)
    return " ".join(s.text.strip() for s in segments).strip()


def extract_word(text):
    tokens = re.findall(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", text)
    norm = [strip_acc(t) for t in tokens]
    for i in range(len(tokens) - 2):
        if norm[i].startswith("ταξιδ") and norm[i + 1] in ("ειναι", "ηναι", "ειν"):
            return tokens[i + 2]
    return None


# -------------------------------------------------------------- the form
def fetch_form():
    import requests
    r = requests.get(FORM_VIEW, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    m = re.search(r"FB_PUBLIC_LOAD_DATA_ = (\[.*?\]);\s*</script>", r.text, re.S)
    if not m:
        raise RuntimeError("Could not parse form data (FB_PUBLIC_LOAD_DATA_)")
    data = json.loads(m.group(1))
    fb = re.search(r'name="fbzx" value="([^"]+)"', r.text)
    return data[1][1], (fb.group(1) if fb else "")


def build_payload(questions, word):
    env = os.environ
    gender_map = {"MALE": "ανδρας", "FEMALE": "γυναικα", "OTHER": "αλλο"}
    want_gender = gender_map.get(env.get("GENDER", "MALE").upper(), "ανδρας")
    values, report = {}, []
    for q in questions:
        if not q[4]:
            continue  # section header / description
        title_raw = q[1] or ""
        title = strip_acc(title_raw)
        qtype = q[3]
        entry = q[4][0]
        key = f"entry.{entry[0]}"
        opts = [o[0] for o in (entry[1] or [])]
        required = bool(entry[2]) if len(entry) > 2 else False
        val = None
        if "λεξη" in title:
            val = word
        elif "mail" in title:
            val = env["EMAIL"]
        elif "επωνυμο" in title:
            val = env["SURNAME"]
        elif "ονομα" in title:
            val = env["FIRST_NAME"]
        elif "φυλο" in title:
            val = next((o for o in opts if strip_acc(o) == want_gender), None)
        elif "ηλικια" in title:
            val = env["AGE"]
        elif "τηλεφωνο" in title:
            val = env["PHONE"]
        elif "περιοχη" in title:
            val = env["AREA"]
        elif qtype == 4 and ("προσωπικων" in title or "πολιτικη" in title):
            val = opts[0] if opts else None            # privacy consent: tick
        elif qtype == 4 and "ενημερωτικ" in title:
            val = None                                  # newsletter: NOT ticked
        elif required:
            raise RuntimeError(f"Unknown required question: {title_raw!r}")
        if val is not None:
            values[key] = val
            if qtype in (2, 4, 3):
                values[f"{key}_sentinel"] = ""
        report.append(f"{title_raw.strip()[:40]!r} -> {key} = {val!r}")
    values["emailAddress"] = env["EMAIL"]  # harmless if the form doesn't collect it
    return values, report


def submit_word(word, dry):
    import requests
    questions, fbzx = fetch_form()
    values, report = build_payload(questions, word)
    log("Form mapping:\n  " + "\n  ".join(report))
    if dry:
        return True, "DRY RUN - not submitted\n" + "\n".join(report)
    values.update({"fvv": "1", "pageHistory": "0", "fbzx": fbzx,
                   "partialResponse": json.dumps([None, None, fbzx])})
    r = requests.post(FORM_POST, data=values, headers={"User-Agent": UA, "Referer": FORM_VIEW}, timeout=30)
    ok = r.status_code == 200 and ("ConfirmationMessage" in r.text or "freebirdFormviewerViewResponse" in r.text)
    return ok, f"HTTP {r.status_code}"


# ------------------------------------------------------------- commands
def cmd_gate():
    t = now()
    s = current_slot(t)
    inside = s - timedelta(minutes=GATE_BEFORE) <= t <= s + timedelta(minutes=MINUTES_AFTER)
    todo = inside and slot_id(s) not in load_state()
    log(f"now={t:%H:%M} slot={slot_id(s)} inside={inside} run={todo}")
    line = f"run={'true' if todo else 'false'}\n"
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(line)


def watch(sid, seconds, confirmations=None):
    outdir = REC / sid
    stamp = now().strftime("%Y-%m-%d_%H-%M-%S")
    t0 = time.time()
    proc = start_recorder(outdir, stamp, seconds, CHUNK)
    get_model()
    processed, prev, found = 0, None, {}
    while True:
        chunks = sorted(outdir.glob(f"{stamp}_chunk*.mp3"))
        finished = len(chunks) - (1 if proc.poll() is None else 0)
        while processed < finished:
            cur = chunks[processed]
            win = make_window(prev, cur, outdir)
            text = transcribe(win)
            word = extract_word(text)
            start_iso = datetime.fromtimestamp(t0 + processed * CHUNK, TZ).isoformat(timespec="seconds")
            cur.with_suffix(".json").write_text(json.dumps(
                {"chunk": cur.name, "approx_start": start_iso, "stream": STREAM_URL,
                 "window": str(win), "transcript": text, "word": word},
                ensure_ascii=False, indent=2))
            log(f"{cur.name}: {text!r} -> {word}")
            if word:
                k = strip_acc(word)
                e = found.setdefault(k, {"word": word, "n": 0, "audio": str(win)})
                e["n"] += 1
                if e["n"] == 1:
                    tg(f"👂 Heard candidate word: {word}\n\"{text}\"", audio=str(win))
                if e["n"] >= (confirmations or CONFIRMATIONS):
                    proc.terminate()
                    return e
            prev = cur
            processed += 1
        if proc.poll() is not None and processed >= len(chunks):
            break
        time.sleep(2)
    if proc.returncode not in (0, None) and not list(outdir.glob("*.mp3")):
        raise RuntimeError("ffmpeg could not record the stream (geo-block / bad URL?)")
    return max(found.values(), key=lambda e: e["n"]) if found else None


def cmd_run():
    t = now()
    s = current_slot(t)
    sid = slot_id(s)
    seconds = int((s + timedelta(minutes=MINUTES_AFTER)).timestamp() - time.time())
    if seconds < 60:
        tg(f"⚠️ Run for slot {sid} started too late - skipped.")
        return
    tg(f"🎙 Listening for slot {sid} ({seconds // 60} min). DRY_RUN={DRY_RUN}")
    try:
        res = watch(sid, seconds)
    except Exception as e:
        tg(f"❌ Slot {sid} failed: {e}")
        sys.exit(1)
    if not res:
        tg(f"🤷 Slot {sid}: no word detected. Check the recordings artifact.")
        return
    ok, info = submit_word(res["word"], DRY_RUN)
    if ok and not DRY_RUN:
        st = load_state()
        st[sid] = {"word": res["word"], "at": now().isoformat(timespec="seconds")}
        save_state(st)
    tg(f"{'✅' if ok else '❌'} Slot {sid}: word '{res['word']}' (heard {res['n']}x) - {info}", audio=res["audio"])
    if not ok:
        sys.exit(1)


def cmd_window():
    """Listen around the nearest announcement slot (works even if the run starts late).

    Records in short chunks, transcribes while recording, and sends a Telegram alert
    (text + audio clip) the moment a candidate word is heard. Nothing is submitted.
    """
    env = os.environ
    lookahead = int(env.get("WINDOW_LOOKAHEAD_MINUTES", "15")) * 60
    before = int(env.get("WINDOW_BEFORE_SECONDS", "60"))
    after = int(env.get("WINDOW_AFTER_SECONDS", "300"))

    t = now()
    slot = current_slot(t)
    delta = (t - slot).total_seconds()          # negative = slot still ahead
    if not (-lookahead <= delta <= after - 30):
        log(f"No slot in range (nearest {slot:%H:%M}, delta {delta:.0f}s) - skipping.")
        return

    get_model()                                  # preload while we wait
    start_target = slot - timedelta(seconds=before)
    end_target = slot + timedelta(seconds=after)
    wait = (start_target - now()).total_seconds()
    if wait > 0:
        log(f"Slot {slot:%H:%M}: waiting {wait:.1f}s until recording start.")
        time.sleep(wait)

    seconds = int((end_target - now()).total_seconds())
    if seconds < 30:
        tg(f"⚠️ {slot:%H:%M} run started too late - skipped.")
        return

    sid = slot.strftime("%Y-%m-%d_%H-%M")
    outdir = REC / sid
    actual_start = now()
    start_offset = (actual_start - start_target).total_seconds()
    tg(f"🎙 Listening for the {slot:%H:%M} announcement until {end_target:%H:%M:%S} "
       f"({seconds}s, start offset {start_offset:+.0f}s)")
    try:
        res = watch(sid, seconds, confirmations=10**6)   # never stop early; alert on each new candidate
    except Exception as e:
        tg(f"❌ {slot:%H:%M} recording failed: {e}")
        sys.exit(1)
    actual_end = now()

    parts = []
    for p in sorted(outdir.glob("*_chunk*.json")):
        try:
            parts.append(json.loads(p.read_text()))
        except Exception:
            pass
    transcript = " ".join(x.get("transcript", "") for x in parts)
    word = res["word"] if res else None
    metadata = {
        "announcement_slot_athens": slot.isoformat(timespec="seconds"),
        "scheduled_start_athens": iso_ms(start_target),
        "scheduled_end_athens": iso_ms(end_target),
        "actual_start_athens": iso_ms(actual_start),
        "actual_end_athens": iso_ms(actual_end),
        "actual_start_utc": actual_start.astimezone(ZoneInfo("UTC")).isoformat(timespec="milliseconds"),
        "actual_end_utc": actual_end.astimezone(ZoneInfo("UTC")).isoformat(timespec="milliseconds"),
        "start_offset_from_schedule_seconds": round(start_offset, 3),
        "stream_url": STREAM_URL.split("?")[0],
        "chunk_seconds": CHUNK,
        "detected_word": word,
        "chunks": parts,
        "note": "Timestamps are runner capture times; stream buffering may add delay vs. real broadcast.",
    }
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"menta_{sid}.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    head = f"🎯 Best word: {word}" if word else "⚠️ No word extracted"
    tg(f"{head}\n🎙 {slot:%H:%M} slot | recorded {actual_start:%H:%M:%S}-{actual_end:%H:%M:%S} "
       f"(start offset {start_offset:+.0f}s)\n\n📝 Transcript (overlapping windows):\n{transcript or '[no speech]'}")


def cmd_test():
    out = REC / "test"
    shutil.rmtree(out, ignore_errors=True)
    proc = start_recorder(out, "test", 30, 15)
    proc.wait()
    chunks = sorted(out.glob("*.mp3"))
    if not chunks:
        tg("❌ TEST: no audio recorded - stream may be geo-blocked from this runner.")
        sys.exit(1)
    texts = []
    for c in chunks:
        txt = transcribe(c)
        texts.append(txt)
        log(c.name, "->", txt)
    summary = ["extract_word self-test: " + str(extract_word("Η λέξη που σε ταξιδεύει είναι: Ουρανός"))]
    try:
        _, report = build_payload(fetch_form()[0], "ΔΟΚΙΜΗ")
        summary.append("Form mapping OK:\n" + "\n".join(report))
    except Exception as e:
        summary.append(f"Form mapping FAILED: {e}")
    tg("✅ TEST finished\nTranscript: " + " | ".join(texts) + "\n\n" + "\n".join(summary), audio=str(chunks[0]))
    print("\n".join(summary))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    {"gate": cmd_gate, "run": cmd_run, "test": cmd_test, "window": cmd_window}[cmd]()
