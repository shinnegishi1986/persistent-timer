# UI_APPEARANCE_PATCH_V1
# app.py
#
# Install:
#   pip install "streamlit>=1.37" "python-dateutil>=2.9"
#
# Run:
#   streamlit run app.py
#
# Initial administrator:
#   Username: timer-admin
#   Password: tadmin123@
#
# Change the initial administrator password after the first login.
#
# Persistence:
#   SQLite database: ./data/timer_app.sqlite3
#   Attachments are stored inside SQLite.
#   Mount ./data on a persistent volume when deploying.
#   Use a single application instance with SQLite.
#
# CSV:
#   UTF-8 with BOM for Japanese Excel compatibility.
#   Timer CSV includes hierarchy and reusable duration settings.
#   Journal CSV includes attachments encoded as JSON/base64.
#   CSV imports create new records rather than overwriting existing records.
#
# Authentication:
#   Passwords use PBKDF2-HMAC-SHA256 with random salts.
#   Authentication lasts for the current Streamlit session.
#   Serve the application over HTTPS in production.

import base64
import calendar
import csv
import hashlib
import hmac
import io
import json
import math
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
from dateutil.relativedelta import relativedelta


st.set_page_config(
    page_title="Persistent Timers & Journal",
    page_icon="⏱",
    layout="wide",
)

DATA_DIR = Path(os.environ.get("TIMER_DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "timer_app.sqlite3"

UTC = timezone.utc
JST = ZoneInfo("Asia/Tokyo")
UNITS = ["hours", "days", "weeks", "months", "years", "until"]
UNIT_LABELS = {
    "hours": "Hours / 時間",
    "days": "Days / 日",
    "weeks": "Weeks / 週",
    "months": "Months / 月",
    "years": "Years / 年",
    "until": "Until date / 指定日まで",
}
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_POST_BYTES = 30 * 1024 * 1024
MAX_CSV_BYTES = 50 * 1024 * 1024
MAX_IMPORT_ROWS = 1000
PASSWORD_ROUNDS = 600_000
DUMMY_SALT = bytes.fromhex("cd" * 16)


@contextmanager
def database():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def utc_now():
    return datetime.now(UTC)


def stamp(value=None):
    return (value or utc_now()).astimezone(UTC).isoformat()


def parse_stamp(value):
    result = datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        raise ValueError("Dates must include a timezone / 日時にはタイムゾーンが必要です。")
    return result.astimezone(UTC)


def local_text(value):
    return parse_stamp(value).astimezone(JST).strftime("%Y-%m-%d %H:%M JST")


def identifier():
    return str(uuid.uuid4())


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ROUNDS
    )
    return f"{PASSWORD_ROUNDS}${salt.hex()}${digest.hex()}"


def password_matches(password, stored):
    try:
        rounds, salt_hex, expected = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(rounds),
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def validate_password(password):
    if len(password) < 10:
        raise ValueError("Password must have at least 10 characters / パスワードは10文字以上。")
    if len(password) > 1024:
        raise ValueError("Password is too long / パスワードが長すぎます。")


def validate_username(username):
    username = username.strip()
    if not 3 <= len(username) <= 80:
        raise ValueError("Username must have 3–80 characters / ユーザー名は3〜80文字。")
    if any(ord(character) < 32 for character in username):
        raise ValueError("Invalid username / 無効なユーザー名。")
    return username


def code_hash(code):
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def initialize():
    with database() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                must_change_password INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS invitations (
                id TEXT PRIMARY KEY,
                code_hash TEXT NOT NULL UNIQUE,
                max_uses INTEGER NOT NULL CHECK(max_uses > 0),
                uses INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT NOT NULL,
                created_by TEXT NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS timers (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                parent_id TEXT REFERENCES timers(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                duration_unit TEXT NOT NULL,
                duration_value INTEGER NOT NULL,
                until_date TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS timers_user_start
            ON timers(user_id, starts_at);

            CREATE TABLE IF NOT EXISTS journals (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                timer_id TEXT REFERENCES timers(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS journals_user_created
            ON journals(user_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS attachments (
                id TEXT PRIMARY KEY,
                journal_id TEXT NOT NULL REFERENCES journals(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                content BLOB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auth_failures (
                username TEXT PRIMARY KEY,
                failures INTEGER NOT NULL,
                blocked_until REAL NOT NULL DEFAULT 0
            );
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO users
            (id, username, password_hash, is_admin, must_change_password, created_at)
            VALUES (?, ?, ?, 1, 1, ?)
            """,
            (
                identifier(),
                "timer-admin",
                password_hash(
                    os.environ.get("TIMER_ADMIN_INITIAL_PASSWORD", "tadmin123@")
                ),
                stamp(),
            ),
        )


def rows(query, parameters=()):
    with database() as connection:
        return [
            dict(row)
            for row in connection.execute(query, parameters).fetchall()
        ]


def current_user():
    user_id = st.session_state.get("user_id")
    if not user_id:
        return None
    result = rows("SELECT * FROM users WHERE id = ?", (user_id,))
    return result[0] if result else None


def authenticate(username, password):
    key = username.strip().casefold()
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        failure = connection.execute(
            "SELECT * FROM auth_failures WHERE username = ?", (key,)
        ).fetchone()
        if failure and failure["blocked_until"] > time.time():
            return None, "Too many attempts. Try again later / しばらくして再試行してください。"

        user = connection.execute(
            "SELECT * FROM users WHERE username = ?", (username.strip(),)
        ).fetchone()
        if user:
            valid = password_matches(password, user["password_hash"])
        else:
            hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), DUMMY_SALT, PASSWORD_ROUNDS
            )
            valid = False

        if not valid:
            count = (failure["failures"] if failure else 0) + 1
            blocked_until = time.time() + 900 if count >= 5 else 0
            connection.execute(
                """
                INSERT INTO auth_failures(username, failures, blocked_until)
                VALUES (?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    failures = excluded.failures,
                    blocked_until = excluded.blocked_until
                """,
                (key, 0 if blocked_until else count, blocked_until),
            )
            return None, "Invalid username or password / ログイン情報が違います。"

        connection.execute("DELETE FROM auth_failures WHERE username = ?", (key,))
        return dict(user), None


def register(username, password, invitation):
    username = validate_username(username)
    validate_password(password)
    hashed_password = password_hash(password)
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        invite = connection.execute(
            "SELECT * FROM invitations WHERE code_hash = ?",
            (code_hash(invitation),),
        ).fetchone()
        if (
            not invite
            or invite["revoked"]
            or invite["uses"] >= invite["max_uses"]
            or parse_stamp(invite["expires_at"]) <= utc_now()
        ):
            raise ValueError("Invalid or expired invitation / 招待コードが無効または期限切れです。")
        connection.execute(
            """
            INSERT INTO users(id, username, password_hash, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (identifier(), username, hashed_password, stamp()),
        )
        connection.execute(
            "UPDATE invitations SET uses = uses + 1 WHERE id = ?", (invite["id"],)
        )


def auth_screen():
    st.title("Timers & Journal / タイマーと日記")
    login_tab, registration_tab = st.tabs(["Login / ログイン", "Register / 新規登録"])
    with login_tab:
        with st.form("login"):
            username = st.text_input("Username / ユーザー名")
            password = st.text_input("Password / パスワード", type="password")
            submitted = st.form_submit_button("Login / ログイン", type="primary")
        if submitted:
            user, error = authenticate(username, password)
            if error:
                st.error(error)
            else:
                persistent_start_login(user)
                st.rerun()

    with registration_tab:
        with st.form("register"):
            username = st.text_input("Username / ユーザー名", key="register_username")
            password = st.text_input(
                "Password / パスワード", type="password", key="register_password"
            )
            repeated = st.text_input("Repeat password / 確認", type="password")
            invitation = st.text_input("Invitation code / 招待コード")
            submitted = st.form_submit_button("Create account / 登録")
        if submitted:
            try:
                if password != repeated:
                    raise ValueError("Passwords do not match / パスワードが一致しません。")
                register(username, password, invitation)
                st.success("Account created. Please log in / 登録完了。ログインしてください。")
            except sqlite3.IntegrityError:
                st.error("Username already exists / ユーザー名は使用済みです。")
            except ValueError as error:
                st.error(str(error))


def account_settings(user):
    with st.form("change_password"):
        old = st.text_input("Current password / 現在のパスワード", type="password")
        new = st.text_input("New password / 新しいパスワード", type="password")
        repeated = st.text_input("Repeat new password / 確認", type="password")
        submitted = st.form_submit_button("Change password / パスワード変更")
    if submitted:
        try:
            if not password_matches(old, user["password_hash"]):
                raise ValueError("Current password is incorrect / 現在のパスワードが違います。")
            validate_password(new)
            if new != repeated:
                raise ValueError("Passwords do not match / パスワードが一致しません。")
            if password_matches(new, user["password_hash"]):
                raise ValueError("Choose a different password / 別のパスワードを設定してください。")
            with database() as connection:
                connection.execute(
                    """
                    UPDATE users SET password_hash = ?, must_change_password = 0
                    WHERE id = ?
                    """,
                    (password_hash(new), user["id"]),
                )
            st.rerun()
        except ValueError as error:
            st.error(str(error))


def user_timers(user_id):
    return rows(
        "SELECT * FROM timers WHERE user_id = ? ORDER BY starts_at DESC",
        (user_id,),
    )


def timer_status(timer):
    if timer_quit_record(timer["id"], timer["user_id"]):
        return QUIT_STATUS
    now = utc_now()
    if now < parse_stamp(timer["starts_at"]):
        return "Future / 予定"
    if now >= parse_stamp(timer["ends_at"]):
        return "Past / 終了"
    return "Current / 進行中"


def timer_label(timer):
    return (
        f"{timer['title']} · {local_text(timer['starts_at'])}"
        f" · {timer['id'][:8]}"
    )



# UNTIL_CALENDAR_TIME_PATCH_V1

def parse_until_value(value):
    """Return the selected JST date and time from legacy or new storage."""
    text = str(value or "").strip()

    if not text.isascii() or not text.isdigit():
        raise ValueError(
            "Invalid Until date/time / 終了日時が不正です。"
        )

    if len(text) == 8:
        parsed = datetime.strptime(text, "%Y%m%d")
        return parsed.date(), datetime_time(23, 59)

    if len(text) == 12:
        parsed = datetime.strptime(text, "%Y%m%d%H%M")
        return parsed.date(), parsed.time()

    raise ValueError(
        "Until must use YYYYMMDD or YYYYMMDDHHMM / "
        "終了日時の保存形式が不正です。"
    )


def calculate_end(start, unit, amount, until_text):
    if unit == "until":
        chosen_date, chosen_time = parse_until_value(until_text)

        # Legacy date-only values retain their original inclusive-day meaning.
        if len(str(until_text).strip()) == 8:
            return datetime.combine(
                chosen_date + timedelta(days=1),
                datetime_time.min,
                JST,
            ).astimezone(UTC)

        # Date-and-time values represent an exact end timestamp in JST.
        return datetime.combine(
            chosen_date,
            chosen_time,
            JST,
        ).astimezone(UTC)

    if amount < 1:
        raise ValueError(
            "Duration must be positive / 期間は1以上にしてください。"
        )

    if unit in {"hours", "days", "weeks"}:
        return start + timedelta(**{unit: amount})

    local_start = start.astimezone(JST)
    return (
        local_start + relativedelta(**{unit: amount})
    ).astimezone(UTC)


def validate_parent(connection, user_id, parent_id, timer_id=None):
    visited = set()
    cursor = parent_id
    while cursor:
        if cursor == timer_id or cursor in visited:
            raise ValueError("Parent relationship would create a cycle / 親子関係が循環します。")
        visited.add(cursor)
        parent = connection.execute(
            "SELECT parent_id FROM timers WHERE id = ? AND user_id = ?",
            (cursor, user_id),
        ).fetchone()
        if not parent:
            raise ValueError("Parent timer not found / 親タイマーが見つかりません。")
        cursor = parent["parent_id"]






def duration_text(seconds):
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"




def journal_page(user):
    timers = user_timers(user["id"])
    timer_map = {timer["id"]: timer for timer in timers}
    with st.expander("Write a journal / 日記を書く"):
        journal_form_epoch = st.session_state.get(
            "_journal_form_epoch", 0
        )
        with st.form(
            f"journal_create_{journal_form_epoch}",
            clear_on_submit=True,
        ):
            title = st.text_input("Post title / 記事タイトル")
            body = st.text_area("Article / 本文", height=220)
            timer_id = st.selectbox(
                "Linked timer / 関連タイマー",
                [None] + list(timer_map),
                format_func=lambda value: (
                    "None / なし"
                    if value is None
                    else f"{timer_status(timer_map[value])} · {timer_label(timer_map[value])}"
                ),
            )
            uploads = st.file_uploader(
                "Images or files / 画像・ファイル",
                accept_multiple_files=True,
                help="10 MB per file; 30 MB per post / 1ファイル10MB、記事合計30MB",
            )
            submitted = st.form_submit_button("Publish / 投稿", type="primary")
        if submitted:
            try:
                if not title.strip() or not body.strip():
                    raise ValueError("Title and article are required / タイトルと本文が必要です。")
                if any(upload.size > MAX_FILE_BYTES for upload in uploads):
                    raise ValueError("A file exceeds 10 MB / 10MBを超えるファイルがあります。")
                if sum(upload.size for upload in uploads) > MAX_POST_BYTES:
                    raise ValueError("Attachments exceed 30 MB / 添付合計が30MBを超えています。")
                post_id = identifier()
                with database() as connection:
                    if timer_id and not connection.execute(
                        "SELECT 1 FROM timers WHERE id=? AND user_id=?",
                        (timer_id, user["id"]),
                    ).fetchone():
                        raise ValueError("Timer not found / タイマーがありません。")
                    connection.execute(
                        """
                        INSERT INTO journals
                        (id,user_id,timer_id,title,body,created_at) VALUES (?,?,?,?,?,?)
                        """,
                        (post_id, user["id"], timer_id, title.strip(), body, stamp()),
                    )
                    for upload in uploads:
                        connection.execute(
                            "INSERT INTO attachments VALUES (?,?,?,?,?)",
                            (
                                identifier(), post_id,
                                Path(upload.name.replace("\\", "/")).name,
                                upload.type or "application/octet-stream",
                                upload.getvalue(),
                            ),
                        )
                st.session_state["_journal_form_epoch"] = (
                    st.session_state.get("_journal_form_epoch", 0) + 1
                )
                st.session_state.pop("journal_export", None)
                st.rerun()
            except ValueError as error:
                st.error(str(error))

    selected_timer = st.selectbox(
        "Filter articles by timer / タイマーで絞り込み",
        ["all", "unlinked"] + list(timer_map),
        format_func=lambda value: (
            "All articles / 全記事" if value == "all"
            else "Unlinked / 関連なし" if value == "unlinked"
            else timer_label(timer_map[value])
        ),
    )
    where = "j.user_id = ?"
    parameters = [user["id"]]
    if selected_timer == "unlinked":
        where += " AND j.timer_id IS NULL"
    elif selected_timer != "all":
        where += " AND j.timer_id = ?"
        parameters.append(selected_timer)

    total = rows(
        f"SELECT COUNT(*) AS n FROM journals j WHERE {where}", parameters
    )[0]["n"]
    pages = max(1, math.ceil(total / 20))
    page = int(st.number_input(
        "Page / ページ",
        min_value=1,
        max_value=pages,
        value=1,
        step=1,
        key=f"journal_page_{selected_timer}_{pages}",
    ))
    st.caption(f"{total} posts · {page}/{pages} · 20 posts/page / 1ページ20記事")
    articles = rows(
        f"""
        SELECT j.*, t.title AS timer_title
        FROM journals j LEFT JOIN timers t ON t.id = j.timer_id
        WHERE {where}
        ORDER BY j.created_at DESC, j.id DESC LIMIT 20 OFFSET ?
        """,
        parameters + [(page - 1) * 20],
    )
    for article in articles:
        with st.container(border=True):
            st.subheader(article["title"])
            st.caption(local_text(article["created_at"]))
            if article["timer_title"]:
                st.caption(f"Timer / タイマー: {article['timer_title']}")
            st.text(article["body"])
            if st.button(
                "Delete article / 記事を削除",
                key=f"delete_article_{article['id']}",
            ):
                confirm_record_deletion(
                    "journal", article["id"], article["title"]
                )
            attachments = rows(
                """
                SELECT id, filename, mime_type, length(content) AS bytes
                FROM attachments WHERE journal_id = ?
                """,
                (article["id"],),
            )
            if attachments:
                with st.expander(f"Attachments / 添付 ({len(attachments)})"):
                    for attachment in attachments:
                        content = rows(
                            "SELECT content FROM attachments WHERE id = ?",
                            (attachment["id"],),
                        )[0]["content"]
                        if attachment["mime_type"] in {
                            "image/png", "image/jpeg", "image/webp", "image/gif"
                        }:
                            try:
                                st.image(content, caption=attachment["filename"])
                            except Exception:
                                st.caption("Image preview unavailable / 画像を表示できません。")
                        st.download_button(
                            f"Download / 保存: {attachment['filename']}",
                            data=content,
                            file_name=attachment["filename"],
                            mime="application/octet-stream",
                            key=f"attachment_{attachment['id']}",
                        )


TIMER_FIELDS = [
    "id", "parent_id", "title", "description", "starts_at", "ends_at",
    "duration_unit", "duration_value", "until_date", "created_at",
]
JOURNAL_FIELDS = [
    "id", "timer_id", "title", "body", "created_at", "attachments_json",
]


def make_csv(records, fields):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(records)
    return buffer.getvalue().encode("utf-8-sig")


def read_csv(upload, fields):
    if not upload or upload.size > MAX_CSV_BYTES:
        raise ValueError("Select a CSV file up to 50 MB / 50MB以下のCSVを選択してください。")
    csv.field_size_limit(MAX_CSV_BYTES)
    reader = csv.DictReader(
        io.StringIO(upload.getvalue().decode("utf-8-sig"), newline="")
    )
    if not reader.fieldnames or not set(fields).issubset(reader.fieldnames):
        raise ValueError("CSV columns do not match / CSVの列が一致しません。")
    result = []
    for record in reader:
        result.append(record)
        if len(result) > MAX_IMPORT_ROWS:
            raise ValueError("Maximum 1,000 records per import / 1回の取込は1000件まで。")
    ids = [record["id"] for record in result]
    if any(not value for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("CSV IDs must be unique and nonempty / IDが空または重複しています。")
    return result


def import_timers(user_id, records):
    mapping = {record["id"]: identifier() for record in records}
    source = {record["id"]: record for record in records}

    for record in records:
        if not record["title"].strip():
            raise ValueError("Timer title is required / タイトルがありません。")
        if record["duration_unit"] not in UNITS:
            raise ValueError("Invalid duration unit / 期間の単位が不正です。")
        if int(record["duration_value"]) < 1:
            raise ValueError("Invalid duration / 期間が不正です。")
        if parse_stamp(record["ends_at"]) <= parse_stamp(record["starts_at"]):
            raise ValueError("Invalid start/end / 開始・終了が不正です。")
        parse_stamp(record["created_at"])
        if record["duration_unit"] == "until":
            parse_until_value(record["until_date"])
        seen = {record["id"]}
        parent = record["parent_id"]
        while parent:
            if parent not in source:
                raise ValueError("Missing parent in CSV / CSVに親タイマーがありません。")
            if parent in seen:
                raise ValueError("Cyclic parent hierarchy / 親子関係が循環しています。")
            seen.add(parent)
            parent = source[parent]["parent_id"]

    with database() as connection:
        for record in records:
            connection.execute(
                """
                INSERT INTO timers
                (id,user_id,parent_id,title,description,starts_at,ends_at,
                 duration_unit,duration_value,until_date,created_at)
                VALUES (?,?,NULL,?,?,?,?,?,?,?,?)
                """,
                (
                    mapping[record["id"]], user_id, record["title"],
                    record["description"], stamp(parse_stamp(record["starts_at"])),
                    stamp(parse_stamp(record["ends_at"])), record["duration_unit"],
                    int(record["duration_value"]), record["until_date"] or None,
                    stamp(parse_stamp(record["created_at"])),
                ),
            )
        for record in records:
            if record["parent_id"]:
                connection.execute(
                    "UPDATE timers SET parent_id = ? WHERE id = ?",
                    (mapping[record["parent_id"]], mapping[record["id"]]),
                )
    return mapping


def import_journals(user_id, records, mapping):
    own_ids = {timer["id"] for timer in user_timers(user_id)}
    with database() as connection:
        for record in records:
            if not record["title"].strip() or not record["body"].strip():
                raise ValueError("Journal title/body required / 記事タイトル・本文が必要です。")
            linked = record["timer_id"]
            target = mapping.get(linked, linked) if linked else None
            if target and target not in own_ids:
                raise ValueError(
                    "Linked timer not found. Import its timer CSV first in this session, "
                    "or clear timer_id / 関連タイマーCSVを同じセッションで先に取り込んでください。"
                )
            attachments = json.loads(record["attachments_json"] or "[]")
            if not isinstance(attachments, list):
                raise ValueError("Invalid attachments / 添付データが不正です。")
            decoded = []
            total_bytes = 0
            for attachment in attachments:
                content = base64.b64decode(attachment["base64"], validate=True)
                total_bytes += len(content)
                if len(content) > MAX_FILE_BYTES or total_bytes > MAX_POST_BYTES:
                    raise ValueError("Attachment limit exceeded / 添付容量を超えています。")
                decoded.append((
                    Path(str(attachment["filename"]).replace("\\", "/")).name or "file",
                    str(attachment.get("mime_type") or "application/octet-stream"),
                    content,
                ))
            post_id = identifier()
            connection.execute(
                "INSERT INTO journals VALUES (?,?,?,?,?,?)",
                (
                    post_id, user_id, target, record["title"], record["body"],
                    stamp(parse_stamp(record["created_at"])),
                ),
            )
            for filename, mime_type, content in decoded:
                connection.execute(
                    "INSERT INTO attachments VALUES (?,?,?,?,?)",
                    (identifier(), post_id, filename, mime_type, content),
                )


def data_page(user):
    st.subheader("CSV import / export · CSV入出力")
    st.caption(
        "Exports contain private text and file contents. Keep them secure. "
        "Imports always create copies. For a full restore: import timers first, "
        "then journals during the same login session. Parent IDs and journal "
        "links are remapped. Import only trusted CSVs; exported text is preserved "
        "verbatim and spreadsheet applications may interpret formulas."
        "\n\n"
        "CSVには個人情報と添付ファイルが含まれます。取込は常に新規作成です。"
        "全体復元は同じログイン中にタイマー→日記の順で実行してください。"
        "Excel等では数式として解釈される文字列に注意してください。"
    )
    left, right = st.columns(2)
    with left:
        st.markdown("### Timers / タイマー")
        st.download_button(
            "Export timer CSV / タイマーCSV出力",
            make_csv(user_timers(user["id"]), TIMER_FIELDS),
            file_name="timers.csv",
            mime="text/csv",
        )
        upload = st.file_uploader("Timer CSV / タイマーCSV", type=["csv"])
        if st.button("Import timers / タイマー取込"):
            try:
                records = read_csv(upload, TIMER_FIELDS)
                mapping = import_timers(user["id"], records)
                existing = st.session_state.setdefault("timer_import_mapping", {})
                existing.update(mapping)
                st.success(f"Imported / 取込完了: {len(records)}")
            except (ValueError, KeyError, TypeError, OverflowError, csv.Error) as error:
                st.error(f"Import failed; nothing imported / 取込失敗: {error}")

    with right:
        st.markdown("### Journals / 日記")
        if st.button("Prepare journal export / 日記CSVを準備"):
            articles = rows(
                "SELECT * FROM journals WHERE user_id=? ORDER BY created_at",
                (user["id"],),
            )
            for article in articles:
                attachments = rows(
                    "SELECT filename,mime_type,content FROM attachments WHERE journal_id=?",
                    (article["id"],),
                )
                article["attachments_json"] = json.dumps(
                    [
                        {
                            "filename": attachment["filename"],
                            "mime_type": attachment["mime_type"],
                            "base64": base64.b64encode(attachment["content"]).decode("ascii"),
                        }
                        for attachment in attachments
                    ],
                    ensure_ascii=False,
                )
            st.session_state.journal_export = make_csv(articles, JOURNAL_FIELDS)
        if "journal_export" in st.session_state:
            st.download_button(
                "Download journal CSV / 日記CSV出力",
                st.session_state.journal_export,
                file_name="journals.csv",
                mime="text/csv",
            )
            st.caption("Prepared snapshot; prepare again after new posts / 投稿後は再準備してください。")
        journal_upload = st.file_uploader(
            "Journal CSV / 日記CSV", type=["csv"], key="journal_csv"
        )
        if st.button("Import journals / 日記取込"):
            try:
                records = read_csv(journal_upload, JOURNAL_FIELDS)
                import_journals(
                    user["id"], records,
                    st.session_state.get("timer_import_mapping", {}),
                )
                st.success(f"Imported / 取込完了: {len(records)}")
            except (
                ValueError, KeyError, TypeError, OverflowError, csv.Error,
                sqlite3.IntegrityError,
            ) as error:
                st.error(f"Import failed; nothing imported / 取込失敗: {error}")


def admin_page(user):
    # Recheck authorization at the operation boundary.
    authorized = rows(
        "SELECT is_admin FROM users WHERE id=?", (user["id"],)
    )
    if not authorized or not authorized[0]["is_admin"]:
        st.error("Administrator access required / 管理者権限が必要です。")
        return

    st.subheader("Invitations / 招待コード")
    with st.form("issue_invitation"):
        max_uses = st.number_input("Maximum uses / 使用可能回数", 1, 10000, 1)
        expiry_days = st.number_input("Expires in days / 有効日数", 1, 3650, 7)
        submitted = st.form_submit_button("Issue invitation / 招待コード発行")
    if submitted:
        code = secrets.token_urlsafe(24)
        with database() as connection:
            connection.execute(
                """
                INSERT INTO invitations
                (id,code_hash,max_uses,uses,expires_at,created_by,created_at,revoked)
                VALUES (?,?,?,0,?,?,?,0)
                """,
                (
                    identifier(), code_hash(code), int(max_uses),
                    stamp(utc_now() + timedelta(days=int(expiry_days))),
                    user["id"], stamp(),
                ),
            )
        st.session_state.new_invitation_code = code
    if st.session_state.get("new_invitation_code"):
        st.info("Copy this code now; only its hash is stored / コードを控えてください。")
        st.code(st.session_state.new_invitation_code, language=None)
        if st.button("Hide code / コードを非表示"):
            del st.session_state.new_invitation_code
            st.rerun()

    invitations = rows(
        """
        SELECT id,max_uses,uses,expires_at,revoked,created_at
        FROM invitations ORDER BY created_at DESC
        """
    )
    if invitations:
        st.dataframe(invitations, hide_index=True, use_container_width=True)
        choices = [invite["id"] for invite in invitations if not invite["revoked"]]
        if choices:
            selected = st.selectbox("Invitation to revoke / 無効にする招待ID", choices)
            if st.button("Revoke selected invitation / 選択した招待を無効化"):
                with database() as connection:
                    connection.execute(
                        "UPDATE invitations SET revoked=1 WHERE id=?", (selected,)
                    )
                st.rerun()

    st.subheader("Create user / ユーザー作成")
    with st.form("admin_create_user"):
        username = st.text_input("Username / ユーザー名", key="admin_username")
        password = st.text_input("Temporary password / 仮パスワード", type="password")
        submitted = st.form_submit_button("Create user / 作成")
    if submitted:
        try:
            username = validate_username(username)
            validate_password(password)
            with database() as connection:
                connection.execute(
                    """
                    INSERT INTO users
                    (id,username,password_hash,is_admin,must_change_password,created_at)
                    VALUES (?,?,?,0,1,?)
                    """,
                    (identifier(), username, password_hash(password), stamp()),
                )
            st.success("User created; password change required at login / 作成しました。初回変更必須です。")
        except sqlite3.IntegrityError:
            st.error("Username already exists / ユーザー名は使用済みです。")
        except ValueError as error:
            st.error(str(error))

    st.subheader("Users / ユーザー一覧")
    st.dataframe(
        rows(
            """
            SELECT username,is_admin,must_change_password,created_at
            FROM users ORDER BY created_at
            """
        ),
        hide_index=True,
        use_container_width=True,
    )



# AUDIO_NOTIFICATIONS_PATCH_V1

@st.cache_data(show_spinner=False)
def completion_chime():
    """Generate a short WAV chime without external files or dependencies."""
    import io as audio_io
    import math as audio_math
    import struct
    import wave

    sample_rate = 22050
    samples = bytearray()
    notes = [
        (659.25, 0.18),
        (830.61, 0.18),
        (987.77, 0.42),
        (0.0, 0.12),
        (987.77, 0.32),
    ]

    for frequency, duration in notes:
        count = int(sample_rate * duration)
        for index in range(count):
            position = index / sample_rate
            if frequency:
                attack = min(1.0, position / 0.012)
                release = min(1.0, (duration - position) / 0.065)
                envelope = attack * release
                sample = (
                    0.30
                    * envelope
                    * audio_math.sin(2 * audio_math.pi * frequency * position)
                )
            else:
                sample = 0.0
            samples.extend(struct.pack("<h", int(32767 * sample)))

    buffer = audio_io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples)
    return buffer.getvalue()


@st.fragment(run_every="2s")
def audio_notification_controls(user_id):
    """Run independently of the selected app page and timer list filters."""
    state = st.session_state

    if state.get("_audio_owner") != user_id:
        for key in (
            "_audio_enabled",
            "_audio_armed_at",
            "_audio_seen",
            "_audio_pending",
            "_audio_player_visible",
        ):
            state.pop(key, None)
        state["_audio_owner"] = user_id

    state.setdefault("_audio_seen", set())
    state.setdefault("_audio_pending", [])
    state.setdefault("_audio_player_visible", False)

    enabled = st.checkbox(
        "Enable sound / 終了音を有効化",
        value=False,
        key="_audio_enabled",
    )

    if not enabled:
        state.pop("_audio_armed_at", None)
        state["_audio_pending"] = []
        state["_audio_player_visible"] = False
        st.caption(
            "Enable sound before a timer finishes. "
            "終了前に音を有効にしてください。"
        )
        return

    now = utc_now()

    if "_audio_armed_at" not in state:
        state["_audio_armed_at"] = stamp(now)
        state["_audio_seen"] = set()

    # Compare parsed timestamps, rather than assuming imported ISO strings
    # all have the same UTC offset or formatting.
    armed_at = parse_stamp(state["_audio_armed_at"])
    completed = []

    for timer in user_timers(user_id):
        if timer_quit_record(timer["id"], user_id):
            continue
        end = parse_stamp(timer["ends_at"])
        event_key = (timer["id"], stamp(end))
        if (
            armed_at < end <= now
            and event_key not in state["_audio_seen"]
        ):
            completed.append((timer, event_key))

    completed.sort(key=lambda item: parse_stamp(item[0]["ends_at"]))

    if completed:
        for timer, event_key in completed:
            state["_audio_seen"].add(event_key)
            state["_audio_pending"].append(
                {
                    "id": timer["id"],
                    "title": timer["title"],
                    "ends_at": timer["ends_at"],
                }
            )
        state["_audio_player_visible"] = True
        st.toast(
            f"{len(completed)} timer(s) finished / タイマー終了",
        )

    test_clicked = st.button(
        "Test sound / 音をテスト",
        key="_audio_test",
        use_container_width=True,
    )
    if test_clicked:
        state["_audio_player_visible"] = True

    pending = state["_audio_pending"]
    if pending:
        st.warning(f"Finished / 終了: {len(pending)}")
        # Display user text as plain text rather than interpolated Markdown.
        for event in pending[-10:]:
            st.text(event["title"])
            st.caption(local_text(event["ends_at"]))
        if len(pending) > 10:
            st.caption(
                f"+ {len(pending) - 10} more / その他"
            )

    # Keep the player mounted between checks. Recreating it every two seconds
    # could interrupt playback or trigger unnecessary autoplay attempts.
    if state["_audio_player_visible"]:
        if test_clicked or not pending:
            audio_content, audio_format = completion_chime(), "audio/wav"
        else:
            # One sound per check: use the latest completed timer.
            audio_content, audio_format = alarm_audio_for_timer(
                pending[-1]["id"], user_id
            )

        st.audio(
            audio_content,
            format=audio_format,
            autoplay=True,
        )
        st.caption(
            "If silent, press Play. Automatic playback may be blocked. "
            "無音の場合は再生ボタンを押してください。"
        )

    if pending or state["_audio_player_visible"]:
        if st.button(
            "Dismiss / 閉じる",
            key="_audio_dismiss",
            use_container_width=True,
        ):
            state["_audio_pending"] = []
            state["_audio_player_visible"] = False
            st.rerun(scope="fragment")

    st.caption(
        "Keep this app open and connected. Device sleep or browser restrictions "
        "can delay or prevent sound. "
        "アプリを開いたままにしてください。スリープやブラウザ制限で"
        "通知が遅れたり、鳴らない場合があります。"
    )



# CUSTOM_AUDIO_SUPPORT_PATCH_V1

CUSTOM_SOUND_LIMIT = 10 * 1024 * 1024

CUSTOM_SOUND_MIMES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}


def initialize_custom_audio():
    with database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS alarm_sounds (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                content BLOB NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS alarm_sounds_user
            ON alarm_sounds(user_id, title);

            CREATE TABLE IF NOT EXISTS timer_alarm_sounds (
                timer_id TEXT PRIMARY KEY
                    REFERENCES timers(id) ON DELETE CASCADE,
                sound_id TEXT NOT NULL
                    REFERENCES alarm_sounds(id) ON DELETE CASCADE
            );
            """
        )


def custom_sound_list(user_id):
    return rows(
        """
        SELECT id,title,description,filename,mime_type,
               length(content) AS size_bytes,created_at,updated_at
        FROM alarm_sounds
        WHERE user_id=?
        ORDER BY title COLLATE NOCASE,id
        """,
        (user_id,),
    )


def custom_sound_upload(upload):
    if upload is None:
        raise ValueError("Choose an audio file / 音声ファイルを選択してください。")

    filename = Path(upload.name.replace("\\", "/")).name
    extension = Path(filename).suffix.lower()

    if extension not in CUSTOM_SOUND_MIMES:
        raise ValueError("Unsupported audio extension / 未対応の音声形式です。")

    if upload.size <= 0 or upload.size > CUSTOM_SOUND_LIMIT:
        raise ValueError("Audio must be 1 byte–10 MiB / 音声は10MiB以下にしてください。")

    return filename, CUSTOM_SOUND_MIMES[extension], upload.getvalue()


def preview_saved_sound(user_id, sound_id):
    result = rows(
        """
        SELECT content,mime_type FROM alarm_sounds
        WHERE id=? AND user_id=?
        """,
        (sound_id, user_id),
    )
    if result:
        st.audio(
            result[0]["content"],
            format=result[0]["mime_type"],
        )


def selected_timer_sound(timer_id, user_id):
    if not timer_id:
        return None
    result = rows(
        """
        SELECT s.id
        FROM timer_alarm_sounds a
        JOIN timers t ON t.id=a.timer_id
        JOIN alarm_sounds s ON s.id=a.sound_id
        WHERE t.id=? AND t.user_id=? AND s.user_id=?
        """,
        (timer_id, user_id, user_id),
    )
    return result[0]["id"] if result else None


def save_timer_sound(timer_id, user_id, sound_id):
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if not connection.execute(
            "SELECT 1 FROM timers WHERE id=? AND user_id=?",
            (timer_id, user_id),
        ).fetchone():
            raise ValueError("Timer not found / タイマーがありません。")

        if sound_id:
            if not connection.execute(
                "SELECT 1 FROM alarm_sounds WHERE id=? AND user_id=?",
                (sound_id, user_id),
            ).fetchone():
                raise ValueError("Sound not found / 音声がありません。")
            connection.execute(
                """
                INSERT INTO timer_alarm_sounds(timer_id,sound_id)
                VALUES (?,?)
                ON CONFLICT(timer_id) DO UPDATE SET sound_id=excluded.sound_id
                """,
                (timer_id, sound_id),
            )
        else:
            connection.execute(
                "DELETE FROM timer_alarm_sounds WHERE timer_id=?",
                (timer_id,),
            )


def alarm_audio_for_timer(timer_id, user_id):
    result = rows(
        """
        SELECT s.content,s.mime_type
        FROM timer_alarm_sounds a
        JOIN timers t ON t.id=a.timer_id
        JOIN alarm_sounds s ON s.id=a.sound_id
        WHERE t.id=? AND t.user_id=? AND s.user_id=?
        """,
        (timer_id, user_id, user_id),
    )
    if result:
        return result[0]["content"], result[0]["mime_type"]
    return completion_chime(), "audio/wav"


def custom_audio_library(user):
    st.caption(
        "Private sound library / 自分専用の音声ライブラリ"
    )
    sounds = custom_sound_list(user["id"])
    sound_map = {sound["id"]: sound for sound in sounds}
    options = [None] + list(sound_map)

    if st.session_state.pop("_reset_custom_sound_picker", False):
        st.session_state["custom_sound_library_selection"] = None

    selected_id = st.selectbox(
        "Create or edit sound / 音声の新規登録・編集",
        options,
        format_func=lambda value: (
            "New sound / 新規登録"
            if value is None
            else sound_map[value]["title"]
        ),
        key="custom_sound_library_selection",
    )
    selected = sound_map.get(selected_id, {})
    widget_prefix = f"custom_sound_{selected_id or 'new'}"
    widget_prefix += (
        f"_{st.session_state.get('_custom_sound_form_epoch', 0)}"
    )

    notice = st.session_state.pop("_custom_sound_notice", None)
    if notice:
        st.success(notice)

    if selected_id:
        st.caption(
            f"{selected['filename']} · "
            f"{selected['size_bytes'] / 1024:.1f} KiB"
        )
        preview_saved_sound(user["id"], selected_id)
        if st.button(
            "Delete sound / 音声を削除",
            key=f"delete_sound_{selected_id}",
        ):
            confirm_record_deletion(
                "sound", selected_id, selected["title"]
            )

    # Outside a form so choosing a file immediately enables its preview.
    upload = st.file_uploader(
        (
            "Replace audio (optional) / 音声差替え（任意）"
            if selected_id
            else "Upload audio / 音声をアップロード"
        ),
        type=["mp3", "wav", "ogg", "m4a", "aac", "flac", "webm"],
        key=f"{widget_prefix}_upload",
    )

    if upload is not None:
        try:
            _, mime_type, content = custom_sound_upload(upload)
            st.caption("Uploaded audio preview / アップロード音声の試聴")
            st.audio(content, format=mime_type)
        except ValueError as error:
            st.error(str(error))

    with st.form(f"{widget_prefix}_form"):
        title = st.text_input(
            "Sound title / 音声タイトル",
            value=selected.get("title", ""),
            max_chars=200,
        )
        description = st.text_area(
            "Sound description / 音声の説明",
            value=selected.get("description", ""),
            max_chars=10000,
        )
        submitted = st.form_submit_button(
            "Save sound / 音声を保存",
            type="primary",
        )

    if submitted:
        try:
            if not title.strip():
                raise ValueError("Title is required / タイトルを入力してください。")

            uploaded = custom_sound_upload(upload) if upload is not None else None
            if not selected_id and uploaded is None:
                raise ValueError("Upload an audio file / 音声ファイルが必要です。")

            with database() as connection:
                if selected_id:
                    if not connection.execute(
                        "SELECT 1 FROM alarm_sounds WHERE id=? AND user_id=?",
                        (selected_id, user["id"]),
                    ).fetchone():
                        raise ValueError("Sound not found / 音声がありません。")

                    if uploaded:
                        filename, mime_type, content = uploaded
                        connection.execute(
                            """
                            UPDATE alarm_sounds
                            SET title=?,description=?,filename=?,mime_type=?,
                                content=?,updated_at=?
                            WHERE id=? AND user_id=?
                            """,
                            (
                                title.strip(), description, filename,
                                mime_type, content, stamp(),
                                selected_id, user["id"],
                            ),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE alarm_sounds
                            SET title=?,description=?,updated_at=?
                            WHERE id=? AND user_id=?
                            """,
                            (
                                title.strip(), description, stamp(),
                                selected_id, user["id"],
                            ),
                        )
                else:
                    filename, mime_type, content = uploaded
                    now = stamp()
                    connection.execute(
                        """
                        INSERT INTO alarm_sounds
                        (id,user_id,title,description,filename,mime_type,
                         content,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            identifier(), user["id"], title.strip(),
                            description, filename, mime_type, content, now, now,
                        ),
                    )

            if not selected_id:
                st.session_state["_reset_custom_sound_picker"] = True
                st.session_state["_custom_sound_form_epoch"] = (
                    st.session_state.get("_custom_sound_form_epoch", 0) + 1
                )

            st.session_state["_custom_sound_notice"] = (
                "Sound saved / 音声を保存しました。"
            )
            st.rerun()
        except ValueError as error:
            st.error(str(error))



# TIMER_QUIT_DASHBOARD_PATCH_V1

QUIT_STATUS = "Quit / 中断"


def initialize_quit_history():
    with database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS timer_quit_events (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                timer_id TEXT NOT NULL UNIQUE REFERENCES timers(id),
                title_snapshot TEXT NOT NULL,
                starts_at_snapshot TEXT NOT NULL,
                ends_at_snapshot TEXT NOT NULL,
                quit_at TEXT NOT NULL,
                elapsed_seconds REAL NOT NULL CHECK(elapsed_seconds >= 0),
                scheduled_seconds REAL NOT NULL CHECK(scheduled_seconds > 0),
                reason TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS quit_events_user_date
            ON timer_quit_events(user_id, quit_at);
            """
        )


def timer_quit_record(timer_id, user_id):
    result = rows(
        """
        SELECT * FROM timer_quit_events
        WHERE timer_id=? AND user_id=?
        """,
        (timer_id, user_id),
    )
    return result[0] if result else None


def quit_running_timer(timer_id, user_id, reason=""):
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")

        timer = connection.execute(
            "SELECT * FROM timers WHERE id=? AND user_id=?",
            (timer_id, user_id),
        ).fetchone()
        if not timer:
            raise ValueError("Timer not found / タイマーがありません。")

        previous = connection.execute(
            """
            SELECT id FROM timer_quit_events
            WHERE timer_id=? AND user_id=?
            """,
            (timer_id, user_id),
        ).fetchone()
        if previous:
            return False

        now = utc_now()
        start = parse_stamp(timer["starts_at"])
        end = parse_stamp(timer["ends_at"])

        if not start <= now < end:
            raise ValueError(
                "Only a currently running timer can be quit. "
                "進行中のタイマーのみ中断できます。"
            )

        connection.execute(
            """
            INSERT INTO timer_quit_events
            (id,user_id,timer_id,title_snapshot,starts_at_snapshot,
             ends_at_snapshot,quit_at,elapsed_seconds,scheduled_seconds,reason)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                identifier(), user_id, timer_id, timer["title"],
                timer["starts_at"], timer["ends_at"], stamp(now),
                (now - start).total_seconds(),
                (end - start).total_seconds(),
                reason.strip()[:2000],
            ),
        )
    return True


def quit_timer_controls(timer, user_id):
    record = timer_quit_record(timer["id"], user_id)
    if record:
        return record

    now = utc_now()
    if not (
        parse_stamp(timer["starts_at"])
        <= now
        < parse_stamp(timer["ends_at"])
    ):
        return None

    with st.expander("Stop / quit this timer · タイマーを中断"):
        reason = st.text_input(
            "Reason (optional) / 理由（任意）",
            max_chars=2000,
            key=f"quit_reason_{timer['id']}",
        )
        st.caption(
            "Records the elapsed time and stops completion audio for this timer. "
            "Child timers remain independent. This action cannot be undone here. "
            "経過時間を記録し、このタイマーの終了音を停止します。"
            "子タイマーは独立して継続します。この画面では取り消せません。"
        )
        if st.button(
            "Stop and record quit / 中断して記録",
            key=f"quit_action_{timer['id']}",
        ):
            try:
                quit_running_timer(timer["id"], user_id, reason)
                st.rerun(scope="fragment")
            except ValueError as error:
                st.error(str(error))
    return None


def quit_history_dashboard(user_id):
    import altair as alt
    import pandas as pd

    st.title("Quit dashboard / タイマー中断分析")
    st.caption(
        "Manual quits only. Weekdays and hours use the quit time in JST. "
        "Elapsed time is measured from the saved timer start, not from browser use. "
        "手動中断のみを集計します。曜日・時刻は中断時の日本時間です。"
        "経過時間はタイマー開始日時から計算します。"
    )

    records = rows(
        """
        SELECT * FROM timer_quit_events
        WHERE user_id=? ORDER BY quit_at DESC
        """,
        (user_id,),
    )
    if not records:
        st.info(
            "No quit records yet. Stop a running timer to create a record. "
            "中断履歴はありません。進行中のタイマーを中断すると記録されます。"
        )
        return

    df = pd.DataFrame(records)
    df["quit_local"] = pd.to_datetime(
        df["quit_at"], utc=True
    ).dt.tz_convert("Asia/Tokyo")
    df["local_date"] = df["quit_local"].dt.date

    left, right = st.columns(2)
    from_date = left.date_input(
        "From / 開始日",
        value=min(df["local_date"]),
        key="quit_dashboard_from",
    )
    to_date = right.date_input(
        "Through / 終了日",
        value=max(df["local_date"]),
        key="quit_dashboard_through",
    )
    search = st.text_input(
        "Filter timer title / タイトルで絞り込み",
        key="quit_dashboard_search",
    )

    if from_date > to_date:
        st.error("Invalid date range / 日付範囲が不正です。")
        return

    filtered = df.loc[
        (df["local_date"] >= from_date)
        & (df["local_date"] <= to_date)
    ].copy()

    if search.strip():
        filtered = filtered.loc[
            filtered["title_snapshot"].str.contains(
                search.strip(), case=False, regex=False, na=False
            )
        ].copy()

    if filtered.empty:
        st.info("No matching quit events / 該当する中断履歴はありません。")
        return

    filtered["elapsed_hours"] = filtered["elapsed_seconds"] / 3600
    filtered["weekday_number"] = filtered["quit_local"].dt.weekday
    filtered["hour"] = filtered["quit_local"].dt.hour
    filtered["elapsed_percent"] = (
        filtered["elapsed_seconds"] / filtered["scheduled_seconds"] * 100
    )

    count = len(filtered)
    sample_sd = filtered["elapsed_hours"].std(ddof=1)

    first, second, third = st.columns(3)
    first.metric("Quit events / 中断回数", count)
    second.metric(
        "Mean elapsed / 平均経過時間",
        f"{filtered['elapsed_hours'].mean():.2f} h",
    )
    third.metric(
        "Sample SD / 標本標準偏差",
        f"{sample_sd:.2f} h" if count >= 2 else "N/A (n < 2)",
    )

    weekdays = [
        "Monday / 月", "Tuesday / 火", "Wednesday / 水",
        "Thursday / 木", "Friday / 金", "Saturday / 土",
        "Sunday / 日",
    ]

    weekday_counts = (
        filtered.groupby("weekday_number")
        .size()
        .reindex(range(7), fill_value=0)
        .rename("count")
        .reset_index()
    )
    weekday_counts["weekday"] = weekdays

    hour_counts = (
        filtered.groupby("hour")
        .size()
        .reindex(range(24), fill_value=0)
        .rename("count")
        .reset_index()
    )

    st.subheader("When do you quit? / いつ中断するか")
    st.caption(
        "Counts describe recorded quits, not the probability of quitting. "
        "回数の比較であり、中断率ではありません。"
    )

    weekday_chart = (
        alt.Chart(weekday_counts)
        .mark_bar()
        .encode(
            x=alt.X("weekday:N", sort=weekdays, title="Weekday / 曜日"),
            y=alt.Y("count:Q", title="Quit events / 中断回数"),
            tooltip=[
                alt.Tooltip("weekday:N", title="Weekday"),
                alt.Tooltip("count:Q", title="Quits", format="d"),
            ],
        )
        .properties(height=280)
    )
    st.altair_chart(weekday_chart, use_container_width=True)

    hour_chart = (
        alt.Chart(hour_counts)
        .mark_bar()
        .encode(
            x=alt.X(
                "hour:O", sort=list(range(24)),
                title="Quit hour, JST / 中断時刻",
            ),
            y=alt.Y("count:Q", title="Quit events / 中断回数"),
            tooltip=[
                alt.Tooltip("hour:O", title="Hour (JST)"),
                alt.Tooltip("count:Q", title="Quits", format="d"),
            ],
        )
        .properties(height=280)
    )
    st.altair_chart(hour_chart, use_container_width=True)

    st.subheader(
        "Elapsed duration and standard deviation / 経過時間と標準偏差"
    )
    st.caption(
        "Bars show mean elapsed hours at quit. Error bars show mean ± one "
        "sample SD, not a confidence interval. No error bar is shown for n=1. "
        "Groups may mix timers with different intended durations. "
        "棒は中断までの平均経過時間、誤差線は平均±1標本標準偏差です。"
        "信頼区間ではありません。1件のみの場合は誤差線を表示しません。"
        "予定期間の異なるタイマーが混在する場合があります。"
    )

    def elapsed_chart(group_column, label_column, ordered_labels):
        summary = (
            filtered.groupby(group_column)["elapsed_hours"]
            .agg(n="count", mean_hours="mean", sd_hours="std")
            .reset_index()
        )
        summary[label_column] = summary[group_column].map(
            dict(enumerate(ordered_labels))
        )
        summary["lower"] = summary["mean_hours"] - summary["sd_hours"]
        summary["upper"] = summary["mean_hours"] + summary["sd_hours"]

        x = alt.X(
            f"{label_column}:N",
            sort=ordered_labels,
            title=(
                "Weekday / 曜日"
                if group_column == "weekday_number"
                else "Quit hour, JST / 中断時刻"
            ),
        )
        base = alt.Chart(summary).encode(x=x)
        bars = base.mark_bar(opacity=0.75).encode(
            y=alt.Y(
                "mean_hours:Q",
                title="Elapsed hours at quit / 中断までの経過時間",
            ),
            tooltip=[
                alt.Tooltip(f"{label_column}:N", title="Group"),
                alt.Tooltip("n:Q", title="Events", format="d"),
                alt.Tooltip("mean_hours:Q", title="Mean hours", format=".3f"),
                alt.Tooltip("sd_hours:Q", title="Sample SD hours", format=".3f"),
            ],
        )
        errors = (
            base.transform_filter(alt.datum.n >= 2)
            .mark_errorbar(ticks=True, color="#202020")
            .encode(
                y=alt.Y("lower:Q", title="Elapsed hours"),
                y2="upper:Q",
            )
        )
        return (bars + errors).properties(height=320), summary

    chart, weekday_summary = elapsed_chart(
        "weekday_number", "weekday", weekdays
    )
    st.altair_chart(chart, use_container_width=True)

    chart, hour_summary = elapsed_chart(
        "hour", "hour_label", [f"{hour:02d}:00" for hour in range(24)]
    )
    st.altair_chart(chart, use_container_width=True)

    st.caption(
        "A lower mean−SD endpoint can be negative mathematically; "
        "individual elapsed durations are never negative. "
        "平均−標準偏差が負になる場合がありますが、実測の経過時間は非負です。"
    )

    with st.expander("Summary tables / 集計表"):
        st.dataframe(
            weekday_summary, hide_index=True, use_container_width=True
        )
        st.dataframe(
            hour_summary, hide_index=True, use_container_width=True
        )

    st.subheader("Quit history / 中断履歴")

    export = filtered[
        [
            "id", "timer_id", "title_snapshot", "starts_at_snapshot",
            "ends_at_snapshot", "quit_at", "elapsed_seconds",
            "scheduled_seconds", "elapsed_percent", "reason",
        ]
    ].copy()
    export["quit_at_jst"] = filtered["quit_local"].dt.strftime(
        "%Y-%m-%d %H:%M:%S %z"
    )

    display = pd.DataFrame({
        "Timer / タイマー": filtered["title_snapshot"],
        "Quit at JST / 中断日時": export["quit_at_jst"],
        "Elapsed hours / 経過時間": filtered["elapsed_hours"].round(3),
        "Elapsed % / 経過率": filtered["elapsed_percent"].round(1),
        "Reason / 理由": filtered["reason"],
    })

    st.dataframe(display, hide_index=True, use_container_width=True)
    st.download_button(
        "Export filtered quit CSV / 表示中の中断履歴CSV",
        export.to_csv(index=False).encode("utf-8-sig"),
        file_name="timer_quit_history.csv",
        mime="text/csv",
    )
    st.caption(
        "CSV preserves user text. Treat spreadsheet formulas cautiously. "
        "CSVは入力文字列を保持します。表計算ソフトの数式解釈に注意してください。"
    )



# DELETE_AND_CLEAR_FEATURES_PATCH_V1

def edit_feature_table_exists(connection, name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def delete_owned_record(kind, record_id, user_id):
    """Enforce ownership inside the same transaction as deletion."""
    tables = {
        "timer": "timers",
        "sound": "alarm_sounds",
        "journal": "journals",
    }
    table = tables.get(kind)
    if table is None:
        raise ValueError("Unknown record type / 不明なレコード種別です。")

    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")

        # Table name is selected exclusively from the fixed mapping above.
        owned = connection.execute(
            f"SELECT 1 FROM {table} WHERE id=? AND user_id=?",
            (record_id, user_id),
        ).fetchone()
        if not owned:
            raise ValueError(
                "Record not found or already deleted. "
                "対象が存在しないか、すでに削除されています。"
            )

        if kind == "sound":
            if edit_feature_table_exists(connection, "timer_alarm_sounds"):
                connection.execute(
                    "DELETE FROM timer_alarm_sounds WHERE sound_id=?",
                    (record_id,),
                )
            connection.execute(
                "DELETE FROM alarm_sounds WHERE id=? AND user_id=?",
                (record_id, user_id),
            )

        elif kind == "timer":
            # Preserve children and journals instead of deleting them.
            connection.execute(
                "UPDATE timers SET parent_id=NULL WHERE parent_id=?",
                (record_id,),
            )
            connection.execute(
                "UPDATE journals SET timer_id=NULL WHERE timer_id=?",
                (record_id,),
            )
            if edit_feature_table_exists(connection, "timer_alarm_sounds"):
                connection.execute(
                    "DELETE FROM timer_alarm_sounds WHERE timer_id=?",
                    (record_id,),
                )
            if edit_feature_table_exists(connection, "timer_quit_events"):
                connection.execute(
                    "DELETE FROM timer_quit_events WHERE timer_id=?",
                    (record_id,),
                )
            connection.execute(
                "DELETE FROM timers WHERE id=? AND user_id=?",
                (record_id, user_id),
            )

        elif kind == "journal":
            # Explicit deletion also supports databases without cascade settings.
            connection.execute(
                "DELETE FROM attachments WHERE journal_id=?",
                (record_id,),
            )
            connection.execute(
                "DELETE FROM journals WHERE id=? AND user_id=?",
                (record_id, user_id),
            )


def clear_after_record_deletion(kind, record_id):
    state = st.session_state

    # Discard prepared exports that could contain deleted information.
    state.pop("journal_export", None)

    if kind == "sound":
        state["_reset_custom_sound_picker"] = True
        state["_custom_sound_form_epoch"] = (
            state.get("_custom_sound_form_epoch", 0) + 1
        )

    if kind == "timer":
        state["_audio_pending"] = [
            event for event in state.get("_audio_pending", [])
            if event.get("id") != record_id
        ]
        if not state["_audio_pending"]:
            state["_audio_player_visible"] = False

        mapping = state.get("timer_import_mapping", {})
        state["timer_import_mapping"] = {
            old_id: new_id
            for old_id, new_id in mapping.items()
            if new_id != record_id
        }

        # Avoid selecting a timer that no longer exists in edit/reuse tabs.
        for key in list(state):
            if key.startswith("timer_source_"):
                del state[key]

    state["_record_deleted_notice"] = "Deleted / 削除しました。"


@st.dialog("Confirm deletion / 削除の確認")
def confirm_record_deletion(kind, record_id, title):
    user = current_user()
    if not user:
        st.error("Please log in again / 再ログインしてください。")
        return

    st.text(title)

    descriptions = {
        "sound": (
            "Delete this sound permanently? Its audio BLOB will be removed. "
            "Timers using it will use the default chime. "
            "この音声を完全に削除しますか？音声データも削除され、"
            "使用中のタイマーは標準チャイムになります。"
        ),
        "timer": (
            "Delete this timer permanently? Its quit history will also be deleted. "
            "Child timers and journal articles will remain, without this link. "
            "このタイマーを完全に削除しますか？中断履歴も削除されます。"
            "子タイマーと日記は残り、このタイマーとの関連だけ解除されます。"
        ),
        "journal": (
            "Delete this article and all attached image/file BLOBs permanently? "
            "この記事と添付された画像・ファイルを完全に削除しますか？"
        ),
    }

    st.warning(descriptions[kind])
    confirmed = st.checkbox(
        "I understand this cannot be undone / 取り消せないことを確認しました",
        key=f"delete_confirm_{kind}_{record_id}",
    )
    left, right = st.columns(2)

    if left.button(
        "Delete permanently / 完全に削除",
        disabled=not confirmed,
        type="primary",
        key=f"delete_execute_{kind}_{record_id}",
    ):
        try:
            delete_owned_record(kind, record_id, user["id"])
            clear_after_record_deletion(kind, record_id)
            st.rerun()
        except ValueError as error:
            st.error(str(error))

    if right.button(
        "Cancel / キャンセル",
        key=f"delete_cancel_{kind}_{record_id}",
    ):
        st.rerun()



# PERSISTENT_AUTH_PATCH_V1

PERSISTENT_COOKIE_NAME = os.environ.get(
    "TIMER_COOKIE_NAME", "persistent_timer_login_v1"
)
PERSISTENT_COOKIE_COMPONENT = "_persistent_cookie_component"
PERSISTENT_COOKIE_DAYS = 400
PERSISTENT_COOKIE_SECURE = (
    os.environ.get("TIMER_COOKIE_SECURE", "1").strip().lower()
    not in {"0", "false", "no"}
)


def initialize_persistent_auth():
    with database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS persistent_login_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                password_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL,
                revoked_at TEXT
            );

            CREATE INDEX IF NOT EXISTS persistent_sessions_user
            ON persistent_login_sessions(user_id);
            """
        )


def persistent_token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def persistent_password_version(stored_password_hash):
    return hashlib.sha256(
        stored_password_hash.encode("utf-8")
    ).hexdigest()


def persistent_controller():
    controller = st.session_state.get("_persistent_controller")
    if controller is None:
        raise RuntimeError("Persistent cookie controller is not initialized.")
    return controller


def persistent_write_cookie(token):
    controller = persistent_controller()
    controller.set(
        PERSISTENT_COOKIE_NAME,
        token,
        path="/",
        expires=utc_now() + timedelta(days=PERSISTENT_COOKIE_DAYS),
        max_age=PERSISTENT_COOKIE_DAYS * 86400,
        secure=PERSISTENT_COOKIE_SECURE,
        same_site="strict",
    )
    st.session_state["_persistent_cookie_written_at"] = time.time()


def persistent_clear_cookie():
    persistent_controller().set(
        PERSISTENT_COOKIE_NAME,
        "",
        path="/",
        expires=utc_now() - timedelta(days=1),
        max_age=0,
        secure=PERSISTENT_COOKIE_SECURE,
        same_site="strict",
    )


def persistent_clear_application_state():
    # Keep the cookie component's internal state while clearing account data.
    # Removing its widget state during a running script can cause widget errors.
    for key in list(st.session_state):
        if not str(key).startswith("_persistent_"):
            del st.session_state[key]


def persistent_revoke_token(token):
    if not isinstance(token, str) or not token:
        return
    with database() as connection:
        connection.execute(
            """
            UPDATE persistent_login_sessions
            SET revoked_at=?
            WHERE token_hash=? AND revoked_at IS NULL
            """,
            (stamp(), persistent_token_hash(token)),
        )


def persistent_start_login(user):
    previous_token = (
        st.session_state.get("_persistent_token")
        or persistent_controller().get(PERSISTENT_COOKIE_NAME)
    )
    persistent_revoke_token(previous_token)

    token = secrets.token_urlsafe(48)
    now = stamp()

    with database() as connection:
        connection.execute(
            """
            INSERT INTO persistent_login_sessions
            (token_hash,user_id,password_version,created_at,last_used_at,revoked_at)
            VALUES (?,?,?,?,?,NULL)
            """,
            (
                persistent_token_hash(token),
                user["id"],
                persistent_password_version(user["password_hash"]),
                now,
                now,
            ),
        )

    persistent_clear_application_state()
    st.session_state["_persistent_token"] = token
    st.session_state["user_id"] = user["id"]
    persistent_write_cookie(token)

    # Give the browser component an opportunity to process the cookie write.
    # If the browser blocks cookies, this login still works for this session.
    time.sleep(0.5)


def persistent_logout():
    token = (
        st.session_state.get("_persistent_token")
        or persistent_controller().get(PERSISTENT_COOKIE_NAME)
    )
    persistent_revoke_token(token)
    persistent_clear_cookie()
    persistent_clear_application_state()
    st.session_state.pop("_persistent_token", None)
    st.session_state.pop("_persistent_cookie_written_at", None)
    time.sleep(0.5)


def restore_persistent_login():
    try:
        from streamlit_cookies_controller import CookieController
    except ImportError:
        st.error(
            "Missing dependency. Install: pip install streamlit-cookies-controller"
        )
        st.stop()

    # Construct on every full script run so the component can synchronize
    # with the browser. Helper functions reuse this run's controller instance.
    controller = CookieController(key=PERSISTENT_COOKIE_COMPONENT)
    st.session_state["_persistent_controller"] = controller

    # On a fresh browser connection, cookies arrive asynchronously.
    # The component's initial response triggers a rerun.
    if (
        PERSISTENT_COOKIE_COMPONENT not in st.session_state
        and not st.session_state.get("_persistent_token")
    ):
        st.info("Restoring login / ログイン状態を確認しています…")
        st.stop()

    token = (
        st.session_state.get("_persistent_token")
        or controller.get(PERSISTENT_COOKIE_NAME)
    )

    if not isinstance(token, str) or not 40 <= len(token) <= 256:
        if st.session_state.get("user_id"):
            persistent_clear_application_state()
        st.session_state.pop("_persistent_token", None)
        return

    result = rows(
        """
        SELECT s.token_hash,s.password_version,s.revoked_at,
               u.id AS user_id,u.password_hash
        FROM persistent_login_sessions s
        JOIN users u ON u.id=s.user_id
        WHERE s.token_hash=?
        """,
        (persistent_token_hash(token),),
    )

    valid = bool(
        result
        and not result[0]["revoked_at"]
        and hmac.compare_digest(
            result[0]["password_version"],
            persistent_password_version(result[0]["password_hash"]),
        )
    )

    if not valid:
        persistent_revoke_token(token)
        persistent_clear_cookie()
        persistent_clear_application_state()
        st.session_state.pop("_persistent_token", None)
        return

    record = result[0]

    # Never retain another account's cached UI state.
    previous_user = st.session_state.get("user_id")
    if previous_user and previous_user != record["user_id"]:
        persistent_clear_application_state()

    st.session_state["_persistent_token"] = token
    st.session_state["user_id"] = record["user_id"]

    # Renew the browser cookie and update activity at most once per day
    # within this Streamlit session.
    last_write = st.session_state.get("_persistent_cookie_written_at", 0)
    if time.time() - last_write >= 86400:
        with database() as connection:
            connection.execute(
                """
                UPDATE persistent_login_sessions
                SET last_used_at=?
                WHERE token_hash=? AND revoked_at IS NULL
                """,
                (stamp(), record["token_hash"]),
            )
        persistent_write_cookie(token)



# UNIQUE_TIMER_UI_PATCH_V1

def unique_timer_dashboard_records(records):
    """Preserve ordering while allowing each database timer ID only once."""
    seen_ids = set()
    result = []

    for timer in records:
        timer_id = timer.get("id")
        if not timer_id or timer_id in seen_ids:
            continue
        seen_ids.add(timer_id)
        result.append(timer)

    return result



# TIMER_ISSUE_202611161225_FIXED

def _fixed_timer_sound_rows(user_id):
    return rows(
        """
        SELECT id,title,description FROM alarm_sounds
        WHERE user_id=? ORDER BY title COLLATE NOCASE,id
        """,
        (user_id,),
    )


def _fixed_timer_notice(message):
    st.session_state["_fixed_timer_notice"] = message


def _fixed_timer_duration(seconds):
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"


def _fixed_timer_quit(timer):
    lookup = globals().get("timer_quit_record")
    return (
        lookup(timer["id"], timer["user_id"])
        if callable(lookup)
        else None
    )


def _fixed_timer_status(timer, quit_record=None):
    if quit_record:
        return "Quit / 中断"
    now = utc_now()
    if now < parse_stamp(timer["starts_at"]):
        return "Future / 予定"
    if now >= parse_stamp(timer["ends_at"]):
        return "Past / 終了"
    return "Current / 進行中"


def timer_editor(user):
    notice = st.session_state.pop("_fixed_timer_notice", None)
    if notice:
        st.success(notice)

    modes = ["New / 新規", "Reuse template / 再利用", "Edit / 編集"]
    for tab, mode in zip(st.tabs(modes), modes):
        with tab:
            _timer_editor_content(user, mode)


def _timer_editor_content(user, mode):
    timers = user_timers(user["id"])
    by_id = {timer["id"]: timer for timer in timers}
    editing = mode == "Edit / 編集"
    creating = mode == "New / 新規"
    source = {}

    if not creating:
        if not by_id:
            st.info("No saved timers / 保存済みタイマーはありません。")
            return

        selector_key = f"_fixed_timer_source_{mode}"
        if (
            selector_key in st.session_state
            and st.session_state[selector_key] not in by_id
        ):
            del st.session_state[selector_key]

        selected_id = st.selectbox(
            "Saved timer / 保存済みタイマー",
            list(by_id),
            format_func=lambda value: timer_label(by_id[value]),
            key=selector_key,
        )
        source = by_id[selected_id]

    # This immutable local value is the UPDATE target.
    # It never depends on title, sound selection, or widget state.
    edit_target_id = source["id"] if editing else None
    epoch = st.session_state.get("_fixed_new_timer_epoch", 0)
    prefix = f"fixed_timer_{mode}_{source.get('id', 'new')}"
    if creating:
        prefix += f"_{epoch}"

    default_start = (
        parse_stamp(source["starts_at"]).astimezone(JST)
        if editing
        else utc_now().astimezone(JST)
    )

    if source.get("until_date"):
        raw = source["until_date"]
        parser = globals().get("parse_until_value")
        if callable(parser):
            until_day, until_time = parser(raw)
        else:
            parsed = datetime.strptime(raw, "%Y%m%d")
            until_day, until_time = parsed.date(), datetime_time(23, 59)
    else:
        until_day = default_start.date()
        until_time = datetime_time(23, 59)

    sound_rows = _fixed_timer_sound_rows(user["id"])
    sounds = {sound["id"]: sound for sound in sound_rows}
    sound_options = [None] + list(sounds)
    assignments = rows(
        """
        SELECT a.sound_id FROM timer_alarm_sounds a
        JOIN timers t ON t.id=a.timer_id
        WHERE t.id=? AND t.user_id=?
        """,
        (source.get("id", ""), user["id"]),
    )
    default_sound = assignments[0]["sound_id"] if assignments else None
    if default_sound not in sound_options:
        default_sound = None

    sound_key = f"{prefix}_sound"
    if (
        sound_key in st.session_state
        and st.session_state[sound_key] not in sound_options
    ):
        del st.session_state[sound_key]

    # Outside the form so changing a sound immediately changes its preview.
    sound_id = st.selectbox(
        "Alarm sound / 終了音",
        sound_options,
        index=sound_options.index(default_sound),
        format_func=lambda value: (
            "Default chime / 標準チャイム"
            if value is None
            else sounds[value]["title"]
        ),
        key=sound_key,
    )
    if sound_id:
        if sounds[sound_id]["description"]:
            st.text(sounds[sound_id]["description"])
        audio = rows(
            "SELECT content,mime_type FROM alarm_sounds WHERE id=? AND user_id=?",
            (sound_id, user["id"]),
        )
        if audio:
            st.audio(audio[0]["content"], format=audio[0]["mime_type"])
    elif callable(globals().get("completion_chime")):
        st.audio(completion_chime(), format="audio/wav")

    parent_options = [None] + [
        timer_id for timer_id in by_id if timer_id != edit_target_id
    ]
    default_parent = source.get("parent_id")
    if default_parent not in parent_options:
        default_parent = None

    with st.form(f"{prefix}_form"):
        if editing:
            st.caption(f"Editing timer ID / 編集対象ID: {edit_target_id}")

        title = st.text_input("Title / タイトル", value=source.get("title", ""))
        description = st.text_area(
            "Description / 説明", value=source.get("description", "")
        )
        parent_id = st.selectbox(
            "Parent timer / 親タイマー",
            parent_options,
            index=parent_options.index(default_parent),
            format_func=lambda value: (
                "None / なし" if value is None else timer_label(by_id[value])
            ),
        )
        left, right = st.columns(2)
        start_day = left.date_input(
            "Start date (JST) / 開始日",
            value=default_start.date(),
            min_value=date(1900, 1, 1),
            max_value=date(9998, 12, 31),
        )
        start_clock = right.time_input(
            "Start time (JST) / 開始時刻",
            value=default_start.time().replace(
                tzinfo=None, second=0, microsecond=0
            ),
            step=60,
        )
        unit = st.selectbox(
            "Duration type / 期間の単位",
            UNITS,
            index=UNITS.index(source.get("duration_unit", "days")),
            format_func=lambda value: UNIT_LABELS[value],
        )
        amount = st.number_input(
            "Duration amount / 期間（指定日時では不使用）",
            min_value=1,
            max_value=1_000_000,
            value=max(1, int(source.get("duration_value", 1))),
            step=1,
        )
        date_col, hour_col, minute_col = st.columns([2, 1, 1])
        end_day = date_col.date_input(
            "Until date (JST) / 終了日",
            value=until_day,
            min_value=min(date(1900, 1, 1), until_day),
            max_value=max(date(9998, 12, 31), until_day),
            format="YYYY/MM/DD",
        )
        end_hour = hour_col.number_input(
            "Hour / 時",
            min_value=0, max_value=23,
            value=until_time.hour, step=1, format="%02d",
        )
        end_minute = minute_col.number_input(
            "Minute / 分",
            min_value=0, max_value=59,
            value=until_time.minute, step=1, format="%02d",
        )
        st.caption(
            "Until uses the exact selected JST time. Children are independent. "
            "指定日時は日本時間ちょうどに終了します。子タイマーは独立しています。"
        )
        submitted = st.form_submit_button(
            "Update timer / 更新" if editing else "Create timer / 作成",
            type="primary",
        )

    if not submitted:
        return

    try:
        if not title.strip():
            raise ValueError("Title is required / タイトルが必要です。")

        start = datetime.combine(start_day, start_clock, JST).astimezone(UTC)
        until_text = (
            end_day.strftime("%Y%m%d")
            + f"{int(end_hour):02d}{int(end_minute):02d}"
        )
        if unit == "until":
            end = datetime.combine(
                end_day,
                datetime_time(int(end_hour), int(end_minute)),
                JST,
            ).astimezone(UTC)
        else:
            end = calculate_end(start, unit, int(amount), until_text)

        if end <= start:
            raise ValueError("End must follow start / 終了は開始より後にしてください。")

        timer_id = edit_target_id if editing else identifier()

        # Timer data and sound assignment are saved in ONE transaction.
        with database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            validate_parent(connection, user["id"], parent_id, timer_id)

            if sound_id and not connection.execute(
                "SELECT 1 FROM alarm_sounds WHERE id=? AND user_id=?",
                (sound_id, user["id"]),
            ).fetchone():
                raise ValueError("Sound no longer exists / 音声が削除されています。")

            values = (
                parent_id, title.strip(), description, stamp(start), stamp(end),
                unit, int(amount), until_text if unit == "until" else None,
            )

            if editing:
                cursor = connection.execute(
                    """
                    UPDATE timers
                    SET parent_id=?,title=?,description=?,starts_at=?,ends_at=?,
                        duration_unit=?,duration_value=?,until_date=?
                    WHERE id=? AND user_id=?
                    """,
                    values + (edit_target_id, user["id"]),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "Timer was deleted or is unavailable. No copy was created. "
                        "対象が存在しません。新しいタイマーは作成していません。"
                    )
            else:
                connection.execute(
                    """
                    INSERT INTO timers
                    (parent_id,title,description,starts_at,ends_at,duration_unit,
                     duration_value,until_date,id,user_id,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    values + (timer_id, user["id"], stamp()),
                )

            connection.execute(
                "DELETE FROM timer_alarm_sounds WHERE timer_id=?", (timer_id,)
            )
            if sound_id:
                connection.execute(
                    "INSERT INTO timer_alarm_sounds(timer_id,sound_id) VALUES (?,?)",
                    (timer_id, sound_id),
                )

        if creating:
            st.session_state["_fixed_new_timer_epoch"] = epoch + 1
        _fixed_timer_notice(
            "Updated existing timer / 既存タイマーを更新しました。"
            if editing else "Timer created / タイマーを作成しました。"
        )
        st.rerun()

    except (ValueError, OverflowError, sqlite3.IntegrityError) as error:
        st.error(str(error))


@st.dialog("Delete timer / タイマーの削除")
def _fixed_delete_timer_dialog(timer_id):
    user = current_user()
    if not user:
        st.error("Please log in / ログインしてください。")
        return

    found = rows(
        "SELECT title FROM timers WHERE id=? AND user_id=?",
        (timer_id, user["id"]),
    )
    if not found:
        st.info("Already deleted / すでに削除されています。")
        return

    st.text(found[0]["title"])
    st.warning(
        "Permanent deletion also removes this timer's quit history. "
        "Children and journal articles remain but are unlinked. "
        "完全削除すると中断履歴も削除されます。子タイマーと日記は残ります。"
    )
    confirmed = st.checkbox(
        "Confirm permanent deletion / 完全削除を確認",
        key=f"_fixed_delete_confirm_{timer_id}",
    )
    if st.button(
        "Delete permanently / 完全に削除",
        type="primary",
        disabled=not confirmed,
    ):
        with database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owned = connection.execute(
                "SELECT 1 FROM timers WHERE id=? AND user_id=?",
                (timer_id, user["id"]),
            ).fetchone()
            if owned:
                connection.execute(
                    "UPDATE timers SET parent_id=NULL WHERE parent_id=?",
                    (timer_id,),
                )
                connection.execute(
                    "UPDATE journals SET timer_id=NULL WHERE timer_id=?",
                    (timer_id,),
                )
                connection.execute(
                    "DELETE FROM timer_alarm_sounds WHERE timer_id=?",
                    (timer_id,),
                )
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='timer_quit_events'"
                ).fetchone():
                    connection.execute(
                        "DELETE FROM timer_quit_events WHERE timer_id=?",
                        (timer_id,),
                    )
                connection.execute(
                    "DELETE FROM timers WHERE id=? AND user_id=?",
                    (timer_id, user["id"]),
                )

        st.session_state.pop("journal_export", None)
        pending = [
            event for event in st.session_state.get("_audio_pending", [])
            if event.get("id") != timer_id
        ]
        st.session_state["_audio_pending"] = pending
        if not pending:
            st.session_state["_audio_player_visible"] = False
        _fixed_timer_notice("Timer deleted / タイマーを削除しました。")
        st.rerun()

    if st.button("Cancel / キャンセル"):
        st.rerun()


# SMOOTH_TIMER_ANIMATION_PATCH_V1

def _fixed_timer_statistics(timer, quit_record, now):
    import json as smooth_json
    import streamlit.components.v1 as smooth_components

    start = parse_stamp(timer["starts_at"])
    end = parse_stamp(timer["ends_at"])

    if quit_record:
        total_seconds = float(quit_record["scheduled_seconds"])
        frozen_elapsed = float(quit_record["elapsed_seconds"])
    else:
        total_seconds = (end - start).total_seconds()
        frozen_elapsed = None

    if total_seconds <= 0:
        st.warning("Invalid duration / 期間が不正です。")
        return

    payload = {
        "startMs": start.timestamp() * 1000,
        "endMs": end.timestamp() * 1000,
        "serverNowMs": now.timestamp() * 1000,
        "durationMs": total_seconds * 1000,
        "frozenElapsedMs": (
            frozen_elapsed * 1000
            if frozen_elapsed is not None
            else None
        ),
    }

    document = r"""
<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
    :root {
        color-scheme: light dark;
        --text: #172033;
        --muted: #64748b;
        --blue: #2563eb;
        --track: #cbd5e1;
    }
    * { box-sizing: border-box; }
    body {
        margin: 0;
        padding: 8px 2px;
        font-family: system-ui, -apple-system, BlinkMacSystemFont,
                     "Segoe UI", sans-serif;
        color: var(--text);
        background: transparent;
    }
    .layout {
        display: grid;
        grid-template-columns: minmax(0, 3fr) minmax(155px, 2fr);
        gap: 20px;
        align-items: center;
    }
    .metrics {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 18px 14px;
    }
    .label {
        font-size: 13px;
        color: var(--muted);
        margin-bottom: 5px;
        overflow-wrap: anywhere;
    }
    .value {
        font-size: clamp(17px, 2.8vw, 26px);
        font-weight: 600;
        line-height: 1.25;
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
    }
    .progress {
        margin-top: 20px;
        height: 9px;
        border-radius: 999px;
        background: var(--track);
        overflow: hidden;
    }
    .progress-fill {
        width: 100%;
        height: 100%;
        background: var(--blue);
        transform-origin: left center;
        transform: scaleX(0);
        will-change: transform;
    }
    .note {
        min-height: 34px;
        margin-top: 10px;
        color: var(--muted);
        font-size: 12px;
    }
    .chart { text-align: center; }
    svg {
        width: 176px;
        max-width: 100%;
        height: auto;
        overflow: visible;
    }
    .ring-track, .ring-elapsed {
        fill: none;
        stroke-width: 18;
    }
    .ring-track { stroke: var(--track); }
    .ring-elapsed {
        stroke: var(--blue);
        transform: rotate(-90deg);
        transform-origin: 100px 100px;
    }
    .chart-percent {
        fill: var(--blue);
        font-size: 22px;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
    }
    .legend {
        font-size: 11px;
        color: var(--muted);
        line-height: 1.8;
    }
    .dot {
        display: inline-block;
        width: 9px;
        height: 9px;
        border-radius: 50%;
        margin-right: 4px;
    }
    @media (prefers-color-scheme: dark) {
        :root {
            --text: #f1f5f9;
            --muted: #a9b5c8;
            --blue: #60a5fa;
            --track: #475569;
        }
    }
    @media (max-width: 420px) {
        .layout {
            grid-template-columns: minmax(0, 1fr) 110px;
            gap: 10px;
        }
        .metrics { gap: 14px 8px; }
        .label { font-size: 11px; }
        .value { font-size: 15px; }
    }
</style>
</head>
<body>
<div class="layout">
    <div>
        <div class="metrics">
            <div>
                <div class="label">Elapsed / 経過率</div>
                <div class="value" id="percentage">—</div>
            </div>
            <div>
                <div class="label">Elapsed time / 経過時間</div>
                <div class="value" id="elapsed">—</div>
            </div>
            <div>
                <div class="label" id="remaining-label">
                    Remaining / 残り時間
                </div>
                <div class="value" id="remaining">—</div>
            </div>
            <div>
                <div class="label">Duration / 予定期間</div>
                <div class="value" id="duration">—</div>
            </div>
        </div>
        <div class="progress"
             id="progress"
             role="progressbar"
             aria-label="Elapsed percentage"
             aria-valuemin="0"
             aria-valuemax="100">
            <div class="progress-fill" id="progress-fill"></div>
        </div>
        <div class="note" id="note"></div>
    </div>
    <div class="chart">
        <svg viewBox="0 0 200 200" role="img"
             aria-label="Elapsed and remaining timer progress">
            <circle class="ring-track" cx="100" cy="100" r="76"/>
            <circle class="ring-elapsed" id="ring"
                    cx="100" cy="100" r="76"/>
            <text class="chart-percent" id="chart-percentage"
                  x="100" y="107" text-anchor="middle">—</text>
        </svg>
        <div class="legend">
            <span class="dot" style="background:var(--blue)"></span>
            Elapsed / 経過<br>
            <span class="dot" style="background:var(--track)"></span>
            Remaining / 残り
        </div>
    </div>
</div>
<script>
"use strict";
const config = __SMOOTH_TIMER_CONFIGURATION__;
const elements = {};
for (const id of [
    "percentage", "elapsed", "remaining", "duration", "remaining-label",
    "progress", "progress-fill", "note", "ring", "chart-percentage"
]) {
    elements[id] = document.getElementById(id);
}

const circumference = 2 * Math.PI * 76;
elements.ring.style.strokeDasharray = `${circumference} ${circumference}`;

const frozen = config.frozenElapsedMs !== null;

// Synchronize to the server timestamp when this component is rendered.
// Elapsed browser wall time lets the display catch up after tab suspension.
const browserAnchor = Date.now();
const serverAnchor = config.serverNowMs;
const cache = {};
let lastFrame = -Infinity;

function setText(id, value) {
    if (cache[id] !== value) {
        elements[id].textContent = value;
        cache[id] = value;
    }
}

function durationText(milliseconds, roundUp) {
    const value = Math.max(0, milliseconds) / 1000;
    let seconds = roundUp ? Math.ceil(value) : Math.floor(value);
    const days = Math.floor(seconds / 86400);
    seconds %= 86400;
    const hours = Math.floor(seconds / 3600);
    seconds %= 3600;
    const minutes = Math.floor(seconds / 60);
    seconds %= 60;
    const pad = number => String(number).padStart(2, "0");
    return `${days}d ${pad(hours)}:${pad(minutes)}:${pad(seconds)}`;
}

function render() {
    const now = serverAnchor + (Date.now() - browserAnchor);
    const rawElapsed = frozen
        ? config.frozenElapsedMs
        : now - config.startMs;

    const elapsed = Math.min(config.durationMs, Math.max(0, rawElapsed));
    const remaining = Math.max(0, config.durationMs - elapsed);
    const fraction = elapsed / config.durationMs;
    const percentage = remaining === 0
        ? "100.0%"
        : `${Math.min(fraction * 100, 99.9).toFixed(1)}%`;

    setText("percentage", percentage);
    setText("chart-percentage", percentage);
    setText("elapsed", durationText(elapsed, false));

    // Round live remaining time up, so 00:00:00 means actually finished.
    setText("remaining", durationText(remaining, !frozen));
    setText("duration", durationText(config.durationMs, false));

    elements["progress-fill"].style.transform = `scaleX(${fraction})`;
    elements.ring.style.strokeDashoffset = String(
        circumference * (1 - fraction)
    );

    const wholePercent = Math.floor(fraction * 100);
    if (cache.ariaPercent !== wholePercent) {
        elements.progress.setAttribute("aria-valuenow", wholePercent);
        cache.ariaPercent = wholePercent;
    }

    if (frozen) {
        setText("remaining-label", "Unused at quit / 中断時の残り");
        setText("note", "Frozen at quit / 中断時の記録で固定");
    } else if (now < config.startMs) {
        setText(
            "note",
            "Starts in / 開始まで: "
            + durationText(config.startMs - now, true)
        );
    } else if (remaining === 0) {
        setText("note", "Completed / 終了");
    } else {
        setText("note", "Running / 進行中");
    }
}

function animate(frameTime) {
    // About 20 visual updates/second without a Streamlit server round trip.
    if (frameTime - lastFrame >= 50) {
        render();
        lastFrame = frameTime;
    }
    if (!frozen) {
        requestAnimationFrame(animate);
    }
}

document.addEventListener("visibilitychange", () => {
    if (!document.hidden) render();
});

render();
if (!frozen) requestAnimationFrame(animate);
</script>
</body>
</html>
"""

    document = document.replace(
        "__SMOOTH_TIMER_CONFIGURATION__",
        smooth_json.dumps(payload, allow_nan=False),
    )
    smooth_components.html(document, height=285, scrolling=False)


@st.fragment(run_every="30s")
def timer_dashboard(user_id):
    notice = st.session_state.pop("_fixed_timer_notice", None)
    if notice:
        st.success(notice)

    # One database ID corresponds to exactly one card.
    by_id = {}
    for timer in user_timers(user_id):
        by_id.setdefault(timer["id"], timer)

    statuses = ["All / すべて", "Current / 進行中", "Future / 予定",
                "Past / 終了", "Quit / 中断"]
    selected_status = st.selectbox(
        "Filter / 表示", statuses, key="_fixed_timer_status_filter", label_visibility="collapsed")

    children = {}
    roots = []
    for timer in by_id.values():
        parent = timer.get("parent_id")
        if parent in by_id and parent != timer["id"]:
            children.setdefault(parent, []).append(timer)
        else:
            roots.append(timer)

    ordered = []
    visited = set()

    def append_tree(root):
        stack = [(root, 0)]
        while stack:
            timer, depth = stack.pop()
            if timer["id"] in visited:
                continue
            visited.add(timer["id"])
            ordered.append((timer, depth))
            stack.extend(
                (child, depth + 1)
                for child in reversed(children.get(timer["id"], []))
            )

    for root in roots:
        append_tree(root)
    # Also display records from malformed/cyclic imported hierarchies once.
    for timer in by_id.values():
        if timer["id"] not in visited:
            append_tree(timer)

    shown = 0
    for timer, depth in ordered:
        quit_record = _fixed_timer_quit(timer)
        status = _fixed_timer_status(timer, quit_record)
        if selected_status != "All / すべて" and selected_status != status:
            continue

        shown += 1
        with st.container(border=True):
            prefix = "↳ " * min(depth, 5)
            st.subheader(prefix + timer["title"])
            st.caption(f"{status} · ID: {timer['id']}")
            if timer.get("parent_id") in by_id:
                st.caption(
                    "Parent / 親: " + by_id[timer["parent_id"]]["title"]
                )
            if timer["description"]:
                st.text(timer["description"])
            st.caption(
                local_text(timer["starts_at"]) + " → "
                + local_text(timer["ends_at"])
            )

            sound = rows(
                """
                SELECT s.title FROM timer_alarm_sounds a
                JOIN alarm_sounds s ON s.id=a.sound_id
                WHERE a.timer_id=? AND s.user_id=?
                """,
                (timer["id"], user_id),
            )
            st.caption(
                "Alarm / 終了音: "
                + (sound[0]["title"] if sound else "Default / 標準")
            )

            _fixed_timer_statistics(timer, quit_record, utc_now())

            if quit_record:
                st.caption("Quit / 中断: " + local_text(quit_record["quit_at"]))
                if quit_record["reason"]:
                    st.text(quit_record["reason"])
            elif callable(globals().get("quit_timer_controls")):
                quit_timer_controls(timer, user_id)

            if st.button(
                "Delete timer / タイマーを削除",
                key=f"_fixed_delete_timer_{timer['id']}",
            ):
                _fixed_delete_timer_dialog(timer["id"])

    if shown == 0:
        st.info("No matching timers / 該当するタイマーはありません。")


def main():
    initialize()
    initialize_quit_history()
    initialize_custom_audio()
    initialize_persistent_auth()
    restore_persistent_login()
    user = current_user()
    if not user:
        auth_screen()
        return

    deleted_notice = st.session_state.pop(
        "_record_deleted_notice", None
    )
    if deleted_notice:
        st.success(deleted_notice)

    with st.sidebar:
        st.title("Timers & Journal")
        st.write(user["username"])
        st.caption("Timezone / タイムゾーン: Asia/Tokyo")
        if st.button("Logout / ログアウト"):
            persistent_logout()
            st.rerun()

    if user["must_change_password"]:
        st.title("Set a new password / パスワードを変更してください")
        st.warning("Change your temporary password before continuing / 続行前に仮パスワードを変更してください。")
        account_settings(user)
        return

    with st.sidebar:
        st.markdown("### Audio / 通知音")
        audio_notification_controls(user["id"])
        st.divider()
        with st.expander("Alarm sounds / カスタム音声"):
            custom_audio_library(user)
        st.divider()

    # Native navigation uses links and highlights the active page.
    # Callable pages preserve the authenticated Streamlit session.
    def render_timers():
        pass
        pass
        timer_dashboard(user["id"])

    def render_editor():
        st.title("Create & edit / タイマー作成・編集")
        timer_editor(user)

    def render_journal():
        st.title("Journal / 日記")
        journal_page(user)

    def render_csv():
        data_page(user)

    def render_account():
        st.title("Account / アカウント")
        account_settings(user)

    def render_admin():
        st.title("Administration / 管理")
        admin_page(user)

    pages = [
        st.Page(
            render_timers,
            title="Timers / タイマー",
            url_path="timers",
            default=True,
        ),
        st.Page(
            render_editor,
            title="Create & edit / 作成・編集",
            url_path="timer-editor",
        ),
        st.Page(
            render_journal,
            title="Journal / 日記",
            url_path="journal",
        ),
        st.Page(
            render_csv,
            title="CSV / 入出力",
            url_path="csv",
        ),
        st.Page(
            render_account,
            title="Account / アカウント",
            url_path="account",
        ),
    ]

    if user["is_admin"]:
        pages.append(
            st.Page(
                render_admin,
                title="Admin / 管理",
                url_path="admin",
            )
        )

    def render_quit_dashboard():
        quit_history_dashboard(user["id"])

    pages.append(
        st.Page(
            render_quit_dashboard,
            title="Quit dashboard / 中断分析",
            url_path="quit-dashboard",
        )
    )

    navigation = st.navigation(pages, position="hidden")

    with st.sidebar:
        st.markdown("### Navigation / メニュー")
        for page in pages:
            st.page_link(page, use_container_width=True)

    navigation.run()


if __name__ == "__main__":
    main()