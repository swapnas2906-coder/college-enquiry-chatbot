import csv
import io
import sqlite3
from functools import wraps
import os
import re
import threading
import time
import uuid
from datetime import datetime

from dotenv import load_dotenv
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session, url_for)
from google import genai
from google.genai import types
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))  # reads GEMINI_API_KEY from .env file

DATA_FILE = os.path.join(BASE, "college_data.txt")
LEADS_FILE = os.path.join(BASE, "leads.csv")
LOGS_FILE = os.path.join(BASE, "chat_logs.csv")
DB_FILE = os.path.join(BASE, "users.db")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")  # change this!

API_KEY = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=API_KEY) if API_KEY else None
print("API key loaded:", bool(API_KEY))  # should print True when running

MODELS = ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]

histories = {}
file_lock = threading.Lock()


# ---------- helpers ----------
def read_kb():
    with open(DATA_FILE, encoding="utf-8") as f:
        return f.read()


def build_system():
    return f"""You are a friendly assistant for Jain Institute of Technology, Davangere.

For college-specific facts (fees, dates, courses, hostel,
placements, contacts), use ONLY the info below. If the answer
is not there, say you don't have it and give the office number.
Never invent college facts.
When asked which courses are available, list ALL courses
(every B.E. programme and the other programmes).
If the student wants to apply or be contacted, tell them to use
the "Enquiry form" button in the chat.

For any other question (studies, DBMS, OS, maths, coding, exams,
career), answer fully from your own knowledge like a good tutor.
Never say "I don't have this info" for general study questions.
Only college-specific facts must come from COLLEGE INFO.

LANGUAGE: Reply in the same language the student writes in.
- Kannada script -> reply in Kannada script.
- Kannada written in English letters (e.g. "CSE fees eshtu") -> reply in
  simple Kannada written in English letters.
- English -> reply in English.
Keep numbers, course names and phone numbers exactly as in the info.
Keep answers short and friendly.

COLLEGE INFO:
{read_kb()}"""


def money(pattern, text):
    m = re.search(pattern, text)
    return int(m.group(1).replace(",", "")) if m else 0


def parse_fees():
    """Read fees from college_data.txt so the calculator never goes out of date."""
    kb = read_kb()
    courses = {}
    for line in kb.splitlines():
        m = re.match(r"\s*-\s*([A-Za-z ]+):\s*Rs\.?\s*([\d,]+)\s*$", line)
        if m and m.group(1).strip() in ("CSE", "ISE", "ECE", "Mechanical", "Civil"):
            courses[m.group(1).strip()] = int(m.group(2).replace(",", ""))
    return {
        "courses": courses,
        "boys_hostel": money(r"Boys hostel fees:\s*Rs\.?\s*([\d,]+)", kb),
        "girls_hostel": money(r"Girls hostel fees:\s*Rs\.?\s*([\d,]+)", kb),
        "bus": money(r"Bus fee\s*Rs\.?\s*([\d,]+)", kb),
        "exam": money(r"Exam fee\s*Rs\.?\s*([\d,]+)", kb),
    }


def append_csv(path, header, row):
    with file_lock:
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(header)
            w.writerow(row)


def get_sid():
    return session.setdefault("sid", uuid.uuid4().hex)


def get_history():
    return histories.setdefault(get_sid(), [])


def to_contents(turns):
    turns = turns[-10:]
    while turns and turns[0][0] != "user":
        turns = turns[1:]
    return [types.Content(role=r, parts=[types.Part(text=t)]) for r, t in turns]


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- users / login ----------
def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created TEXT NOT NULL)""")


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            if request.path == "/":
                return redirect(url_for("login"))
            return jsonify({"reply": "Please log in first.", "ok": False}), 401
        return f(*args, **kwargs)
    return wrapper


init_db()


# ---------- student pages ----------
@app.route("/")
@login_required
def home():
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("home"))
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        with db() as c:
            u = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if u and check_password_hash(u["password_hash"], password):
            session.clear()
            session["user_id"] = u["id"]
            session["user_name"] = u["name"]
            return redirect(url_for("home"))
        error = "Wrong email or password."
    return render_template("login.html", mode="login", error=error)


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:80]
        email = (request.form.get("email") or "").strip().lower()[:120]
        password = request.form.get("password") or ""
        if len(name) < 2:
            error = "Please enter your name."
        elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            error = "Enter a valid email address."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        else:
            try:
                with db() as c:
                    cur = c.execute(
                        "INSERT INTO users (name, email, password_hash, created) VALUES (?, ?, ?, ?)",
                        (name, email, generate_password_hash(password), now()))
                    uid = cur.lastrowid
                session.clear()
                session["user_id"] = uid
                session["user_name"] = name
                return redirect(url_for("home"))
            except sqlite3.IntegrityError:
                error = "This email is already registered. Please login."
    return render_template("login.html", mode="register", error=error)


@app.route("/logout")
def logout():
    histories.pop(session.get("sid"), None)
    session.clear()
    return redirect(url_for("login"))


@app.route("/chat", methods=["POST"])
@login_required
def chat():
    if client is None:
        return jsonify({"reply": "Server is not set up yet. Please contact the admin."})

    data = request.get_json(silent=True) or {}
    msg = (data.get("message") or "").strip()[:500]
    if not msg:
        return jsonify({"reply": "Please type your question."})

    history = get_history()
    history.append(("user", msg))
    system = build_system()

    for model in MODELS:
        for attempt in range(3):
            try:
                r = client.models.generate_content(
                    model=model,
                    contents=to_contents(history),
                    config=types.GenerateContentConfig(system_instruction=system),
                )
                reply = (r.text or "").strip()
                if not reply:
                    print("EMPTY reply from", model)
                    break
                history.append(("model", reply))
                append_csv(LOGS_FILE, ["time", "session", "question", "reply"],
                           [now(), get_sid()[:8], msg, reply])
                print("OK with", model)
                return jsonify({"reply": reply})
            except Exception as e:
                err = str(e)
                print("ERROR", model, "attempt", attempt + 1, err)
                # wrong model name / bad request: retrying will not help
                if "404" in err or "400" in err or "NOT_FOUND" in err:
                    break
                # 429 / 503 / network: wait a bit longer each time and retry
                time.sleep(2 * (attempt + 1))

    history.pop()
    return jsonify({"reply": "Sorry, I couldn't answer right now. Please try again, or call the office."})


@app.route("/reset", methods=["POST"])
@login_required
def reset():
    histories.pop(get_sid(), None)
    return jsonify({"ok": True})


@app.route("/fees")
@login_required
def fees():
    return jsonify(parse_fees())


@app.route("/enquiry", methods=["POST"])
@login_required
def enquiry():
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()[:80]
    phone = re.sub(r"\D", "", d.get("phone") or "")[-10:]
    course = (d.get("course") or "").strip()[:60]
    if len(name) < 2:
        return jsonify({"ok": False, "error": "Please enter your name."})
    if not re.fullmatch(r"[6-9]\d{9}", phone):
        return jsonify({"ok": False, "error": "Enter a valid 10-digit mobile number."})
    append_csv(LEADS_FILE, ["time", "name", "phone", "course"],
               [now(), name, phone, course])
    return jsonify({"ok": True})


# ---------- admin ----------
@app.route("/admin", methods=["GET", "POST"])
def admin():
    error = saved = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "login":
            if request.form.get("password") == ADMIN_PASSWORD:
                session["admin"] = True
            else:
                error = "Wrong password."
        elif session.get("admin") and action == "save":
            text = request.form.get("content", "").replace("\r\n", "\n")
            if len(text.strip()) < 50:
                error = "Content looks too short. Not saved."
            else:
                with file_lock, open(DATA_FILE, "w", encoding="utf-8") as f:
                    f.write(text)
                saved = True
    if not session.get("admin"):
        return render_template("admin.html", logged_in=False, error=error)
    return render_template("admin.html", logged_in=True, content=read_kb(),
                           error=error, saved=saved,
                           leads=os.path.exists(LEADS_FILE),
                           logs=os.path.exists(LOGS_FILE))


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin"))


@app.route("/admin/download/<kind>")
def admin_download(kind):
    if not session.get("admin"):
        return redirect(url_for("admin"))
    path = {"leads": LEADS_FILE, "logs": LOGS_FILE}.get(kind)
    if not path or not os.path.exists(path):
        return "No data yet.", 404
    with file_lock, open(path, "rb") as f:
        data = f.read()
    return Response(data, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={kind}.csv"})


if __name__ == "__main__":
    app.run(debug=True)