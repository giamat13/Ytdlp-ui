#!/usr/bin/env python3
"""
yt-dlp Web UI — ממשק דפדפן מקומי ל-yt-dlp (main.py + index.html).

התקנה:
    pip install -r requirements.txt
    + ffmpeg בנתיב המערכת (למיזוג וידאו+שמע, להמרת אודיו, להטמעה ולחיתוך קטעים)

הרצה:
    python main.py                       -> http://127.0.0.1:8765
    python main.py --out D:/Videos --open
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    import yt_dlp
    from yt_dlp.utils import download_range_func, match_filter_func, parse_bytes
except ImportError:
    yt_dlp = None  # נבדק ב-main, בלי לזרוק חריגה (שהדיבאגר של F5 היה עוצר עליה)

OUT_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "yt-dlp")
TPL_SINGLE = "%(title)s [%(id)s].%(ext)s"
TPL_PLAYLIST = "%(playlist_title|playlist)s/%(playlist_index|0)s - %(title)s [%(id)s].%(ext)s"
AUDIO_FORMATS = {"mp3", "m4a", "opus", "flac", "wav", "best"}
SUB_FORMATS = {"srt", "vtt", "ass", "best"}
BROWSERS = ["chrome", "edge", "firefox", "brave", "opera", "vivaldi", "chromium", "whale", "safari"]
SB_CATEGORIES = {"sponsor", "selfpromo", "interaction", "intro", "outro", "preview", "music_offtopic", "filler"}
SB_DEFAULT = ["sponsor", "selfpromo", "interaction", "intro", "outro"]
SEARCH_MAX = 50
ENTRIES_MAX = 1000
FINISHED = ("done", "error", "cancelled")

JOBS = {}                                  # id -> מצב הורדה
JOBS_LOCK = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=2)   # מספר הורדות במקביל
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")


class Cancelled(Exception):
    """נזרקת מתוך progress hook כשמשתמש ביטל הורדה פעילה."""


def clean_err(e):
    return ANSI.sub("", str(e)).strip()[:600] or "שגיאה לא ידועה"


def valid_url(body):
    url = str(body.get("url") or "").strip()
    if not re.match(r"https?://", url, re.I):
        raise ValueError("הכנס קישור שמתחיל ב-http:// או https://")
    return url


# ---------- עזרי קלט ----------

def pick(body, key, allowed, default):
    v = str(body.get(key) or default)
    if v not in allowed:
        raise ValueError(f"ערך לא נתמך: {v}")
    return v


def as_int(body, key, default, lo, hi):
    v = body.get(key)
    if v is None or v == "":
        v = default
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ValueError("מספר לא תקין")
    return min(max(v, lo), hi)


def as_list(body, key):
    v = body.get(key)
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


def as_str(body, key):
    return str(body.get(key) or "").strip()


def parse_minutes(v):
    """דקות מהממשק -> שניות. ריק = בלי הגבלה (0)."""
    v = str(v or "").strip().replace(",", ".")
    if not v:
        return 0.0
    try:
        f = float(v)
    except ValueError:
        raise ValueError("ערך משך לא תקין")
    if f < 0:
        raise ValueError("ערך משך לא תקין")
    return f * 60


def parse_time(s):
    """'1:02:03' / '2:30' / '90' -> שניות. 'inf' = עד הסוף."""
    s = s.strip().lower()
    if s == "inf":
        return float("inf")
    try:
        total = 0.0
        for part in s.split(":"):
            total = total * 60 + float(part)
    except ValueError:
        raise ValueError(f"זמן לא תקין: {s}")
    return total


def parse_sections(s):
    """'1:00-2:30, 5:00-' -> [(60, 150), (300, inf)]."""
    ranges = []
    for part in re.split(r"[,;]", s or ""):
        part = part.strip()
        if not part:
            continue
        if "-" not in part:
            raise ValueError("קטע זמן צריך להיות בצורה התחלה-סוף, למשל 1:00-2:30")
        a, b = part.split("-", 1)
        start = parse_time(a) if a.strip() else 0.0
        end = parse_time(b) if b.strip() else float("inf")
        if end <= start:
            raise ValueError(f"קטע זמן לא תקין: {part}")
        ranges.append((start, end))
    return ranges


def normalize(body):
    """בודק ומנרמל את אפשרויות ההורדה. שגיאה = ValueError עם הודעה בעברית."""
    o = {
        "mode": pick(body, "mode", ("video", "audio", "thumb", "subs"), "video"),
        "quality": as_str(body, "quality") or "best",
        "format_id": as_str(body, "format_id"),
        "format_kind": pick(body, "format_kind", ("video", "audio", "both", ""), ""),
        "container": pick(body, "container", ("mp4", "mkv", "webm"), "mp4"),
        "audio_format": pick(body, "audio_format", AUDIO_FORMATS, "mp3"),
        "audio_quality": pick(body, "audio_quality", ("0", "128", "192", "256", "320"), "192"),
        "subs": bool(body.get("subs")),
        "auto_subs": bool(body.get("auto_subs")),
        "embed_subs": bool(body.get("embed_subs")),
        "sub_langs": as_list(body, "sub_langs") or ["he", "en"],
        "sub_format": pick(body, "sub_format", SUB_FORMATS, "srt"),
        "embed_metadata": bool(body.get("embed_metadata")),
        "embed_thumbnail": bool(body.get("embed_thumbnail")),
        "embed_chapters": bool(body.get("embed_chapters")),
        "write_thumbnail": bool(body.get("write_thumbnail")),
        "write_info_json": bool(body.get("write_info_json")),
        "write_description": bool(body.get("write_description")),
        "sponsorblock": pick(body, "sponsorblock", ("off", "mark", "remove"), "off"),
        "sb_categories": [c for c in as_list(body, "sb_categories") if c in SB_CATEGORIES] or SB_DEFAULT,
        "sections": parse_sections(as_str(body, "sections")),
        "limit_rate": as_str(body, "limit_rate"),
        "retries": as_int(body, "retries", 10, 0, 50),
        "proxy": as_str(body, "proxy"),
        "cookies_browser": pick(body, "cookies_browser", [""] + BROWSERS, ""),
        "cookie_file": as_str(body, "cookie_file"),
        "outtmpl": as_str(body, "outtmpl"),
        "playlist": bool(body.get("playlist")),
        "playlist_items": as_str(body, "playlist_items"),
        "match_filter": as_str(body, "match_filter"),
    }
    check_expr(o["match_filter"])
    if o["format_id"] and not re.match(r"^[\w.\-]+$", o["format_id"]):
        raise ValueError("פורמט לא תקין")
    if o["limit_rate"] and not parse_bytes(o["limit_rate"]):
        raise ValueError("מגבלת מהירות לא תקינה, למשל 500K או 2M")
    if o["playlist_items"] and not re.match(r"^[\d\s,\-]+$", o["playlist_items"]):
        raise ValueError("בחירת פריטים לא תקינה, למשל 1-3,5")
    if o["proxy"] and not re.match(r"^(https?|socks[45]h?)://", o["proxy"], re.I):
        raise ValueError("כתובת פרוקסי לא תקינה")
    if o["cookie_file"] and not os.path.isfile(o["cookie_file"]):
        raise ValueError("קובץ העוגיות לא נמצא")
    if o["outtmpl"] and (os.path.isabs(o["outtmpl"]) or any(p == ".." for p in re.split(r"[\\/]", o["outtmpl"]))):
        raise ValueError("תבנית שם לא יכולה להיות נתיב מוחלט או לכלול ..")
    return o


SAMPLE_INFO = {"title": "x", "uploader": "x", "duration": 1, "view_count": 1, "like_count": 1,
               "is_live": False, "was_live": False, "upload_date": "20000101", "height": 1,
               "subtitles": {}, "automatic_captions": {}, "license": "", "categories": [], "formats": []}


def check_expr(expr):
    """בודק תחביר של מסנן yt-dlp על מידע לדוגמה, כדי לתפוס שגיאה לפני ההרצה."""
    if not expr:
        return
    try:
        match_filter_func(expr)(dict(SAMPLE_INFO), incomplete=True)
    except Exception:
        raise ValueError(f"מסנן yt-dlp לא תקין: {expr}")


def parse_date(v):
    """YYYY-MM-DD מהממשק -> YYYYMMDD (הפורמט של yt-dlp). ריק = בלי גבול."""
    v = str(v or "").strip()
    if not v:
        return ""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
        raise ValueError("תאריך לא תקין")
    return v.replace("-", "")


def normalize_search(body):
    q = as_str(body, "q")
    if not q:
        raise ValueError("כתוב מה לחפש")
    if len(q) > 200:
        raise ValueError("החיפוש ארוך מדי")
    o = {
        "q": q,
        "n": as_int(body, "n", 10, 1, SEARCH_MAX),
        "sort": pick(body, "sort", ("relevance", "date", "views"), "relevance"),
        "min_dur": parse_minutes(body.get("min_dur")),
        "max_dur": parse_minutes(body.get("max_dur")),
        "uploader": as_str(body, "uploader"),
        "exclude": [w.lower() for w in as_list(body, "exclude")],
        "live": pick(body, "live", ("any", "live", "normal", "was_live"), "any"),
        "date_from": parse_date(body.get("date_from")),
        "date_to": parse_date(body.get("date_to")),
        "min_views": as_int(body, "min_views", 0, 0, 10 ** 12),
        "min_likes": as_int(body, "min_likes", 0, 0, 10 ** 12),
        "min_height": as_int(body, "min_height", 0, 0, 4320),
        "need_subs": bool(body.get("need_subs")),
        "need_cc": bool(body.get("need_cc")),
        "category": as_str(body, "category").lower(),
        "expr": as_str(body, "expr"),
    }
    if o["max_dur"] and o["min_dur"] > o["max_dur"]:
        raise ValueError("משך מינימלי גדול ממקסימלי")
    if o["date_from"] and o["date_to"] and o["date_from"] > o["date_to"]:
        raise ValueError("תאריך התחלה מאוחר מתאריך הסיום")
    check_expr(o["expr"])
    return o


# ---------- yt-dlp: מידע, פורמטים, פלייליסט, חיפוש ----------

def get_info(url):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 20,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}

    if info.get("_type") == "playlist" or "entries" in info:
        entries = list(info.get("entries") or [])
        return {
            "is_playlist": True,
            "title": info.get("title") or "פלייליסט",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "count": info.get("playlist_count") or len(entries),
            "thumbnail": info.get("thumbnail") or "",
        }

    heights = sorted(
        {int(f["height"]) for f in info.get("formats") or []
         if f.get("height") and f.get("vcodec") != "none"},
        reverse=True,
    )
    return {
        "is_playlist": False,
        "title": info.get("title") or url,
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail") or "",
        "heights": heights,
        "sub_langs": sorted((info.get("subtitles") or {}).keys()),
        "auto_langs": sorted((info.get("automatic_captions") or {}).keys())[:200],
    }


def list_formats(url):
    """כל הפורמטים של סרטון, בסדר יורד לפי רזולוציה."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "socket_timeout": 20}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    out = []
    raw = sorted(info.get("formats") or [],
                 key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    for f in raw:
        if not f.get("format_id"):
            continue
        vc, ac = f.get("vcodec"), f.get("acodec")
        has_v = vc not in (None, "none")
        has_a = ac not in (None, "none")
        kind = "both" if has_v and has_a else "video" if has_v else "audio" if has_a else ""
        if not kind:
            continue  # storyboards וכדומה
        out.append({
            "id": f["format_id"],
            "ext": f.get("ext") or "",
            "res": f"{f['height']}p" if f.get("height") else (f.get("resolution") or ""),
            "fps": f.get("fps"),
            "vcodec": vc if has_v else "",
            "acodec": ac if has_a else "",
            "abr": f.get("abr"),
            "size": f.get("filesize") or f.get("filesize_approx"),
            "note": f.get("format_note") or "",
            "kind": kind,
        })
    return out


def list_entries(url):
    """פריטי פלייליסט עם מספר סידורי (זה המספר שמשמש ב-playlist_items)."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": "in_playlist", "socket_timeout": 20,
            "noplaylist": False, "playlistend": ENTRIES_MAX}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    entries = [e for e in (info.get("entries") or []) if e]
    return {
        "title": info.get("title") or "",
        "items": [{"index": i + 1, "id": e.get("id") or "",
                   "title": e.get("title") or e.get("id") or "",
                   "duration": e.get("duration")}
                  for i, e in enumerate(entries)],
    }


def is_deep(o):
    """האם צריך מידע מלא על כל תוצאה (ולא רק את התוצאה המהירה של החיפוש)."""
    return bool(o["date_from"] or o["date_to"] or o["min_views"] or o["min_likes"]
                or o["live"] != "any" or o["min_height"] or o["need_subs"]
                or o["need_cc"] or o["category"] or o["expr"] or o["sort"] != "relevance")


def full_info(url):
    """מידע מלא על סרטון (פורמטים, תאריך, לייקים...). None אם נכשל."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "socket_timeout": 20}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:
        return None


def passes(info, o):
    """סינונים קלים, שאפשר לבדוק גם על תוצאה מהירה."""
    dur = info.get("duration")
    if o["min_dur"] and (dur is None or dur < o["min_dur"]):
        return False
    if o["max_dur"] and (dur is None or dur > o["max_dur"]):
        return False
    uploader = info.get("uploader") or info.get("channel") or ""
    if o["uploader"] and o["uploader"].lower() not in uploader.lower():
        return False
    title = (info.get("title") or "").lower()
    if any(w in title for w in o["exclude"]):
        return False
    return True


def passes_deep(info, o):
    """סינונים שדורשים מידע מלא על הסרטון."""
    d = info.get("upload_date") or ""
    if o["date_from"] and (not d or d < o["date_from"]):
        return False
    if o["date_to"] and (not d or d > o["date_to"]):
        return False
    if o["min_views"] and (info.get("view_count") or 0) < o["min_views"]:
        return False
    if o["min_likes"] and (info.get("like_count") or 0) < o["min_likes"]:
        return False
    live, was = bool(info.get("is_live")), bool(info.get("was_live"))
    if o["live"] == "live" and not live:
        return False
    if o["live"] == "normal" and (live or was):
        return False
    if o["live"] == "was_live" and not was:
        return False
    if o["min_height"]:
        best = max([f.get("height") or 0 for f in info.get("formats") or []] + [info.get("height") or 0])
        if best < o["min_height"]:
            return False
    if o["need_subs"] and not (info.get("subtitles") or info.get("automatic_captions")):
        return False
    if o["need_cc"] and "creative commons" not in str(info.get("license") or "").lower():
        return False
    if o["category"] and o["category"] not in " ".join(info.get("categories") or []).lower():
        return False
    if o["expr"]:
        try:
            if match_filter_func(o["expr"])(info, incomplete=False) is not None:
                return False
        except Exception:
            return False
    return True


def to_result(info):
    vid = info.get("id") or ""
    up = info.get("upload_date") or ""
    thumb = info.get("thumbnail") or next(
        (t.get("url") for t in reversed(info.get("thumbnails") or []) if t.get("url")), "")
    return {
        "id": vid,
        "title": info.get("title") or vid,
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "url": f"https://www.youtube.com/watch?v={vid}",
        "thumbnail": thumb,
        "views": info.get("view_count"),
        "likes": info.get("like_count"),
        "upload_date": f"{up[:4]}-{up[4:6]}-{up[6:8]}" if len(up) == 8 else "",
        "live": "live" if info.get("is_live") else ("was_live" if info.get("was_live") else ""),
    }


def search(o):
    """חיפוש ביוטיוב. סינונים קלים רצים על התוצאות המהירות; סינונים מתקדמים בודקים כל תוצאה לעומק."""
    deep = is_deep(o)
    if deep:
        fetch = min(o["n"] * 3, 60)  # בדיקה מלאה איטית, לכן מגבילים את מספר המועמדים
    elif o["min_dur"] or o["max_dur"] or o["uploader"] or o["exclude"]:
        fetch = min(o["n"] * 3, 150)
    else:
        fetch = o["n"]
    flat_opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                 "extract_flat": True, "socket_timeout": 20}
    with yt_dlp.YoutubeDL(flat_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{fetch}:{o['q']}", download=False) or {}
    flat = [e for e in info.get("entries") or [] if e and e.get("id")]

    if deep:
        urls = [f"https://www.youtube.com/watch?v={e['id']}" for e in flat]
        with ThreadPoolExecutor(max_workers=8) as pool:
            candidates = [c for c in pool.map(full_info, urls) if c]
    else:
        candidates = flat

    out = [to_result(c) for c in candidates
           if passes(c, o) and (not deep or passes_deep(c, o))]
    if o["sort"] == "date":
        out.sort(key=lambda r: r["upload_date"], reverse=True)
    elif o["sort"] == "views":
        out.sort(key=lambda r: r["views"] or 0, reverse=True)
    return out[: o["n"]]


# ---------- ffmpeg ----------

def find_ffmpeg():
    """נתיב לתיקייה עם ffmpeg: ב-PATH, או בהתקנת winget שעוד לא נכנסה ל-PATH של התהליך."""
    exe = shutil.which("ffmpeg")
    if exe:
        return os.path.dirname(exe)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        pats = [os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
                os.path.join(local, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "**", "bin", "ffmpeg.exe")]
        for p in pats:
            hits = glob.glob(p, recursive=True)
            if hits:
                return os.path.dirname(hits[0])
    return None


FFMPEG_DIR = find_ffmpeg()


# ---------- הורדה ----------

def video_format(o, ff):
    fid = o["format_id"]
    if fid:
        return f"{fid}+ba/{fid}" if ff and o["format_kind"] == "video" else fid
    q = o["quality"]
    if q in ("", "best"):
        return "bv*+ba/b" if ff else "b"
    try:
        h = int(q)
    except ValueError:
        raise ValueError("איכות לא תקינה")
    return f"bv*[height<={h}]+ba/b[height<={h}]" if ff else f"b[height<={h}]/b"


def build_opts(o):
    ff = FFMPEG_DIR is not None
    mode = o["mode"]

    def need_ff(flag, what):
        if flag and not ff:
            raise ValueError(f"{what} דורש ffmpeg, והוא לא מותקן (winget install Gyan.FFmpeg)")

    need_ff(mode == "audio", "המרת אודיו")
    need_ff(o["embed_subs"], "הטמעת כתוביות")
    need_ff(o["embed_metadata"] or o["embed_chapters"], "הטמעת מטא-דאטה ופרקים")
    need_ff(o["embed_thumbnail"], "הטמעת תמונה")
    need_ff(o["sponsorblock"] != "off", "SponsorBlock")
    need_ff(bool(o["sections"]), "חיתוך קטעי זמן")
    need_ff(mode == "video" and bool(o["format_id"]) and o["format_kind"] == "video", "מיזוג וידאו ושמע")

    subs_on = o["subs"] or mode == "subs"
    opts = {
        "outtmpl": os.path.join(OUT_DIR, o["outtmpl"] or (TPL_PLAYLIST if o["playlist"] else TPL_SINGLE)),
        "noplaylist": not o["playlist"],
        "trim_file_name": 180,
        "retries": o["retries"],
        "fragment_retries": o["retries"],
        "writesubtitles": subs_on,
        "writeautomaticsub": subs_on and o["auto_subs"],
        "subtitleslangs": o["sub_langs"],
        "subtitlesformat": f"{o['sub_format']}/best",
        "writethumbnail": mode == "thumb" or o["write_thumbnail"] or o["embed_thumbnail"],
        "writeinfojson": o["write_info_json"],
        "writedescription": o["write_description"],
        "postprocessors": [],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }

    if mode in ("thumb", "subs"):
        opts["skip_download"] = True
        opts["format"] = "best/bv*+ba"
    elif mode == "audio":
        opts["format"] = o["format_id"] or "ba/b"
    else:
        opts["format"] = video_format(o, ff)
        if ff:
            opts["merge_output_format"] = o["container"]

    if ff:
        opts["ffmpeg_location"] = FFMPEG_DIR
    if o["sections"]:
        opts["download_ranges"] = download_range_func(None, o["sections"])
        opts["force_keyframes_at_cuts"] = True
    if o["limit_rate"]:
        opts["ratelimit"] = parse_bytes(o["limit_rate"])
    if o["proxy"]:
        opts["proxy"] = o["proxy"]
    if o["cookies_browser"]:
        opts["cookiesfrombrowser"] = (o["cookies_browser"],)
    if o["cookie_file"]:
        opts["cookiefile"] = o["cookie_file"]
    if o["playlist"] and o["playlist_items"]:
        opts["playlist_items"] = o["playlist_items"]
    if o["match_filter"]:
        opts["match_filter"] = match_filter_func(o["match_filter"])

    pps = opts["postprocessors"]
    if o["sponsorblock"] != "off":
        pps.append({"key": "SponsorBlock", "categories": o["sb_categories"]})
        if o["sponsorblock"] == "remove":
            pps.append({"key": "ModifyChapters", "remove_sponsor_segments": o["sb_categories"]})
    if mode == "audio":
        pps.append({"key": "FFmpegExtractAudio", "preferredcodec": o["audio_format"],
                    "preferredquality": o["audio_quality"]})
    if o["embed_subs"] and subs_on:
        pps.append({"key": "FFmpegEmbedSubtitle"})
    if o["embed_metadata"] or o["embed_chapters"]:
        pps.append({"key": "FFmpegMetadata", "add_metadata": o["embed_metadata"],
                    "add_chapters": o["embed_chapters"]})
    if o["embed_thumbnail"]:
        pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": True})
    return opts


def _collect_paths(info, acc):
    if not info:
        return acc
    for e in info.get("entries") or []:
        _collect_paths(e, acc)
    for rd in info.get("requested_downloads") or []:
        if rd.get("filepath"):
            acc.add(rd["filepath"])
    if not info.get("entries") and not info.get("requested_downloads") and info.get("filepath"):
        acc.add(info["filepath"])
    return acc


def run_job(jid, url, o):
    job = JOBS[jid]
    if job["cancel"]:
        job["status"] = "cancelled"
        return
    job["status"] = "running"
    job["stage"] = "downloading"
    paths = set()

    def on_progress(d):
        if job["cancel"]:
            raise Cancelled()
        st = d.get("status")
        if st == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            if total:
                job["progress"] = round(min(done * 100 / total, 100), 1)
            job["speed"] = d.get("speed")
            job["eta"] = d.get("eta")
            job["stage"] = "downloading"
        elif st == "finished":
            job["speed"] = None
            job["eta"] = None

    def on_pp(d):
        st = d.get("status")
        if st == "started":
            job["stage"] = "converting"
        elif st == "finished":
            p = (d.get("info_dict") or {}).get("filepath")
            if p:
                paths.add(p)

    try:
        opts = build_opts(o)
        opts["progress_hooks"] = [on_progress]
        opts["postprocessor_hooks"] = [on_pp]
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        _collect_paths(info, paths)
        job["files"] = sorted({os.path.basename(p) for p in paths if os.path.exists(p)})
        job["progress"] = 100
        job["stage"] = "finished"
        job["status"] = "done"
    except Exception as e:
        if job["cancel"]:
            job["status"] = "cancelled"
        else:
            job["error"] = clean_err(e)
            job["status"] = "error"


def create_job(url, title, o):
    jid = uuid.uuid4().hex[:8]
    job = {
        "id": jid, "url": url, "title": title or url, "mode": o["mode"],
        "playlist": o["playlist"], "cancel": False,
        "status": "queued", "stage": "", "progress": 0, "speed": None,
        "eta": None, "error": None, "files": [],
    }
    with JOBS_LOCK:
        JOBS[jid] = job
    POOL.submit(run_job, jid, url, o)
    return job


def open_folder(path):
    if os.name == "nt":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class Handler(BaseHTTPRequestHandler):
    server_version = "ytdlp-ui"

    def log_message(self, format, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(PAGE, "rb") as f:
                    page = f.read()
            except OSError:
                return self._send(500, "חסר index.html ליד הסקריפט", "text/plain; charset=utf-8")
            return self._send(200, page, "text/html; charset=utf-8")
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if path == "/api/config":
            return self._json(200, {
                "out_dir": OUT_DIR,
                "version": yt_dlp.version.__version__,
                "ffmpeg": FFMPEG_DIR is not None,
                "browsers": BROWSERS,
                "sb_categories": sorted(SB_CATEGORIES),
                "sb_default": SB_DEFAULT,
            })
        if path == "/api/jobs":
            with JOBS_LOCK:
                jobs = [dict(j) for j in JOBS.values()][::-1]
            return self._json(200, jobs)
        self._json(404, {"error": "לא נמצא"})

    def do_POST(self):
        path = urlparse(self.path).path
        # בקשות JSON מאתר זר דורשות preflight שאנחנו לא מאשרים, כך שזה מונע הפעלה מדפים אחרים
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json(415, {"error": "נדרש Content-Type: application/json"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "JSON לא תקין"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "בקשה לא תקינה"})

        try:
            if path == "/api/info":
                return self._json(200, get_info(valid_url(body)))

            if path == "/api/formats":
                return self._json(200, list_formats(valid_url(body)))

            if path == "/api/entries":
                return self._json(200, list_entries(valid_url(body)))

            if path == "/api/search":
                return self._json(200, search(normalize_search(body)))

            if path == "/api/download":
                o = normalize(body)
                build_opts(o)  # בדיקת תקינות לפני שמוסיפים לתור
                job = create_job(valid_url(body), str(body.get("title") or ""), o)
                return self._json(200, job)

            if path == "/api/jobs/clear":
                with JOBS_LOCK:
                    for k in [k for k, j in JOBS.items() if j["status"] in FINISHED]:
                        del JOBS[k]
                return self._json(200, {"ok": True})

            if path in ("/api/jobs/cancel", "/api/jobs/remove"):
                jid = str(body.get("id") or "")
                with JOBS_LOCK:
                    job = JOBS.get(jid)
                    if not job:
                        return self._json(404, {"error": "ההורדה לא נמצאה"})
                    if path == "/api/jobs/cancel":
                        job["cancel"] = True
                        if job["status"] == "queued":
                            job["status"] = "cancelled"
                    elif job["status"] in FINISHED:
                        del JOBS[jid]
                return self._json(200, {"ok": True})

            if path == "/api/open":
                open_folder(OUT_DIR)
                return self._json(200, {"ok": True})

            return self._json(404, {"error": "לא נמצא"})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:
            return self._json(500, {"error": clean_err(e)})


def main():
    global OUT_DIR
    # הקונסולה של Windows לא תמיד UTF-8 (cp1252), והדפסה בעברית הייתה קורסת את השרת
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if yt_dlp is None:
        print("חסרה ספרייה yt-dlp בעורך Python שרץ כרגע:")
        print(f"  {sys.executable}")
        print("התקן אותה ל-Python הזה עם:")
        print(f'  & "{sys.executable}" -m pip install -U yt-dlp')
        return
    ap = argparse.ArgumentParser(description="yt-dlp Web UI")
    ap.add_argument("--host", default="127.0.0.1", help="ברירת מחדל: מקומי בלבד")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--out", default=OUT_DIR, help="תיקיית הורדה")
    ap.add_argument("--open", action="store_true", help="פתח דפדפן אוטומטית")
    a = ap.parse_args()

    OUT_DIR = os.path.abspath(a.out)
    os.makedirs(OUT_DIR, exist_ok=True)

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = f"http://{a.host}:{a.port}/"
    print(f"yt-dlp UI  ->  {url}")
    print(f"תיקיית הורדה: {OUT_DIR}")
    if FFMPEG_DIR is None:
        print("אזהרה: ffmpeg לא נמצא. מיזוג, המרה, הטמעה וחיתוך קטעים לא יעבדו.")
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nסוגר…")
        POOL.shutdown(wait=False, cancel_futures=True)
        srv.server_close()


if __name__ == "__main__":
    main()
