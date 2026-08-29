# -*- coding: utf-8 -*-
import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor
import streamlit as st

# ---------------------------------------------------------------- 설정 및 전역 변수
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE_DIR, "data.json")
BACKUP_PATH = os.path.join(BASE_DIR, "data.json.bak")
API_BASE = "https://www.googleapis.com/youtube/v3"
KINDS = ("videos", "channels")
USER_FIELDS = ("note", "tags", "rating", "category", "order", "addedAt", "history", "latest", "latestCheckedAt")

# ---------------------------------------------------------------- 데이터 관리 도우미
def empty_breakout():
    return {"items": [], "checkedAt": "", "days": 0, "channelCheckedAt": {}}

def empty_data():
    return {
        "videos": [],
        "channels": [],
        "categories": {"videos": [], "channels": []},
        "breakout": empty_breakout(),
        "meta": {"lastAutoCheck": "", "apiKey": ""},
    }

def migrate(data):
    for kind in KINDS:
        data.setdefault(kind, [])
    categories = data.setdefault("categories", {})
    for kind in KINDS:
        if not isinstance(categories.get(kind), list):
            categories[kind] = []
    breakout = data.get("breakout")
    if not isinstance(breakout, dict):
        data["breakout"] = empty_breakout()
    else:
        breakout.setdefault("items", [])
        breakout.setdefault("channelCheckedAt", {})
        breakout.setdefault("checkedAt", "")
        breakout.setdefault("days", 0)
    meta = data.setdefault("meta", {})
    meta.setdefault("lastAutoCheck", "")
    meta.setdefault("apiKey", "")

    for kind in KINDS:
        for item in data[kind]:
            item.setdefault("category", "")
            item.setdefault("note", "")
            item.setdefault("tags", [])
            item.setdefault("rating", 0)
            item.setdefault("history", [])
            if kind == "channels":
                item.setdefault("latest", [])
                item.setdefault("latestCheckedAt", "")
            else:
                item.setdefault("channelSubscriberCount", 0)
            name = item.get("category") or ""
            if name and name not in categories[kind]:
                categories[kind].append(name)
        ensure_order(data[kind])
    return data

def load_data():
    if not os.path.exists(DATA_PATH):
        return empty_data()
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        if os.path.exists(BACKUP_PATH):
            try:
                with open(BACKUP_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                return empty_data()
        else:
            return empty_data()
    return migrate(data)

def ensure_order(items):
    fresh = [item for item in items if item.get("order") is None]
    known = [item for item in items if item.get("order") is not None]
    known.sort(key=lambda item: item["order"])
    for index, item in enumerate(fresh + known):
        item["order"] = index

def save_data(data):
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, "rb") as src, open(BACKUP_PATH, "wb") as dst:
                dst.write(src.read())
        except OSError:
            pass
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_PATH)

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())

# ---------------------------------------------------------------- YouTube API & 로직
class ApiError(Exception):
    pass

def api_get(endpoint, params, key):
    key = (key or "").strip()
    if not key:
        raise ApiError("NO_KEY")
    params = dict(params)
    params["key"] = key
    url = API_BASE + "/" + endpoint + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        message = body
        try:
            message = json.loads(body)["error"]["message"]
        except Exception:
            pass
        if e.code == 403 and "quota" in message.lower():
            raise ApiError("오늘 API 할당량을 다 썼습니다.")
        if e.code in (400, 403):
            raise ApiError("BAD_KEY:" + message)
        raise ApiError(f"YouTube API 오류 {e.code}: {message}")
    except urllib.error.URLError as e:
        raise ApiError(f"인터넷 연결을 확인하세요. ({e.reason})")

VIDEO_PATTERNS = [
    re.compile(r"(?:youtube\.com|youtube-nocookie\.com)/watch\?(?:[^\s\"']*&)?v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/shorts/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/live/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/embed/([A-Za-z0-9_-]{11})"),
]
CHANNEL_ID_PATTERN = re.compile(r"youtube\.com/channel/(UC[A-Za-z0-9_-]{20,})")
HANDLE_PATTERN = re.compile(r"youtube\.com/@([^\s/?&#\"'<>,]+)")
LEGACY_C_PATTERN = re.compile(r"youtube\.com/(?:c|user)/([^\s/?&#\"'<>,]+)")

def extract_video_ids(text):
    found = []
    seen = set()
    for pattern in VIDEO_PATTERNS:
        for match in pattern.finditer(text):
            vid = match.group(1)
            if vid not in seen:
                seen.add(vid)
                found.append(vid)
    for line in text.splitlines():
        line = line.strip()
        if len(line) == 11 and re.fullmatch(r"[A-Za-z0-9_-]{11}", line) and line not in seen:
            seen.add(line)
            found.append(line)
    return found

def extract_channel_refs(text):
    refs = []
    seen = set()
    def push(kind, value):
        key = (kind, value.lower())
        if key not in seen:
            seen.add(key)
            refs.append((kind, value))
    for match in CHANNEL_ID_PATTERN.finditer(text):
        push("id", match.group(1))
    for pattern in (HANDLE_PATTERN, LEGACY_C_PATTERN):
        for match in pattern.finditer(text):
            push("handle", match.group(1))
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("@") and len(line) > 1:
            push("handle", line[1:])
        elif re.fullmatch(r"UC[A-Za-z0-9_-]{20,}", line):
            push("id", line)
    return refs

def chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]

def parse_duration(iso):
    if not iso:
        return 0
    match = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    if not match:
        return 0
    days, hours, minutes, seconds = (int(x) if x else 0 for x in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds

def thumbnail_url(thumbs):
    for name in ("maxres", "standard", "high", "medium", "default"):
        if name in thumbs:
            return thumbs[name]["url"]
    return ""

def fetch_videos(video_ids, key):
    result = []
    for batch in chunked(video_ids, 50):
        payload = api_get("videos", {
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(batch),
            "maxResults": 50,
        }, key)
        for item in payload.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            details = item.get("contentDetails", {})
            result.append({
                "id": item["id"],
                "url": "https://www.youtube.com/watch?v=" + item["id"],
                "title": snippet.get("title", ""),
                "channelTitle": snippet.get("channelTitle", ""),
                "channelId": snippet.get("channelId", ""),
                "publishedAt": snippet.get("publishedAt", ""),
                "thumbnail": thumbnail_url(snippet.get("thumbnails", {})),
                "viewCount": int(stats.get("viewCount", 0) or 0),
                "likeCount": int(stats.get("likeCount", 0) or 0),
                "commentCount": int(stats.get("commentCount", 0) or 0),
                "duration": parse_duration(details.get("duration", "")),
            })
    return result

def fetch_channels(channel_ids, key):
    result = []
    for batch in chunked(channel_ids, 50):
        payload = api_get("channels", {
            "part": "snippet,statistics",
            "id": ",".join(batch),
            "maxResults": 50,
        }, key)
        for item in payload.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            handle = (snippet.get("customUrl") or "").lstrip("@")
            result.append({
                "id": item["id"],
                "url": "https://www.youtube.com/channel/" + item["id"],
                "title": snippet.get("title", ""),
                "handle": handle,
                "description": (snippet.get("description") or "")[:300],
                "publishedAt": snippet.get("publishedAt", ""),
                "thumbnail": thumbnail_url(snippet.get("thumbnails", {})),
                "subscriberCount": int(stats.get("subscriberCount", 0) or 0),
                "videoCount": int(stats.get("videoCount", 0) or 0),
                "viewCount": int(stats.get("viewCount", 0) or 0),
                "hiddenSubscriberCount": bool(stats.get("hiddenSubscriberCount", False)),
            })
    return result

def merge_items(existing, fetched, stat_key):
    by_id = {item["id"]: item for item in existing}
    added, updated = 0, 0
    for fresh in fetched:
        old = by_id.get(fresh["id"])
        if old is None:
            new_item = dict(fresh)
            new_item.update({"note": "", "tags": [], "rating": 0, "category": "", "addedAt": now_iso(), "history": []})
            new_item["history"] = [{"at": now_iso(), "value": fresh.get(stat_key, 0)}]
            existing.append(new_item)
            by_id[fresh["id"]] = new_item
            added += 1
        else:
            preserved = {f: old.get(f) for f in USER_FIELDS if f in old}
            old.clear()
            old.update(fresh)
            old.update(preserved)
            history = old.setdefault("history", [])
            value = fresh.get(stat_key, 0)
            if not history or history[-1].get("value") != value:
                history.append({"at": now_iso(), "value": value})
                del history[:-60]
            updated += 1
    return existing, added, updated

def add_from_text(data, text, key, category="", category_kind=""):
    video_ids = extract_video_ids(text)
    channel_refs = extract_channel_refs(text)
    summary = {"videosAdded": 0, "videosUpdated": 0, "channelsAdded": 0, "channelsUpdated": 0, "warnings": []}

    if video_ids:
        fetched = fetch_videos(video_ids, key)
        _, added, updated = merge_items(data["videos"], fetched, "viewCount")
        summary["videosAdded"] = added
        summary["videosUpdated"] = updated

    if channel_refs:
        direct_ids = [value for kind, value in channel_refs if kind == "id"]
        if direct_ids:
            fetched = fetch_channels(direct_ids, key)
            _, added, updated = merge_items(data["channels"], fetched, "subscriberCount")
            summary["channelsAdded"] = added
            summary["channelsUpdated"] = updated

    if not video_ids and not channel_refs:
        summary["warnings"].append("유튜브 링크를 찾지 못했습니다.")

    for kind in KINDS:
        ensure_order(data[kind])
    return summary

# ---------------------------------------------------------------- Streamlit UI 구성
st.set_page_config(page_title="촘촘의 레퍼런스 수집기", layout="wide")

# 데이터 및 세션 상태 초기화
if "data" not in st.session_state:
    st.session_state.data = load_data()

data = st.session_state.data

# API Key 기본값 탐색 우선순위:
# 1. Streamlit Secrets (st.secrets["YOUTUBE_API_KEY"])
# 2. 저장된 data.json 내 meta.apiKey
default_api_key = ""
if "YOUTUBE_API_KEY" in st.secrets:
    default_api_key = st.secrets["YOUTUBE_API_KEY"]
elif data.get("meta", {}).get("apiKey"):
    default_api_key = data["meta"]["apiKey"]

st.title("📌 촘촘의 레퍼런스 수집기 v1.0.0")

# 사이드바: API Key 및 수집 폼
with st.sidebar:
    st.header("⚙️ 설정 및 수집")
    
    api_key = st.text_input(
        "YouTube Data API v3 Key", 
        value=default_api_key, 
        type="password", 
        help="한 번 입력해 저장해두면 다음부터 자동으로 불러옵니다."
    )
    
    # API 키 저장 버튼
    if st.button("API Key 저장"):
        data["meta"]["apiKey"] = api_key.strip()
        save_data(data)
        st.toast("API Key가 기본값으로 저장되었습니다.")

    st.divider()
    st.subheader("➕ URL / ID 추가")
    input_text = st.text_area("유튜브 영상 또는 채널 주소를 입력하세요.", height=120)
    
    tab_kind = st.radio("추가 대상 탭", ["videos", "channels"], format_func=lambda x: "영상" if x == "videos" else "채널")
    
    categories = data["categories"].get(tab_kind, [])
    selected_category = st.selectbox("카테고리 선택", ["(선택 없음)"] + categories)
    new_cat_input = st.text_input("새 카테고리 직접 입력")

    if st.button("수집 시작", use_container_width=True):
        if not api_key:
            st.error("API 키를 입력해주세요.")
        elif not input_text.strip():
            st.warning("입력값이 비어 있습니다.")
        else:
            cat_to_apply = new_cat_input.strip() if new_cat_input.strip() else (selected_category if selected_category != "(선택 없음)" else "")
            try:
                # 입력된 API 키 자동 저장
                if api_key.strip() != data.get("meta", {}).get("apiKey"):
                    data["meta"]["apiKey"] = api_key.strip()

                summary = add_from_text(data, input_text, api_key, category=cat_to_apply, category_kind=tab_kind)
                save_data(data)
                st.session_state.data = data
                st.success(f"완료! 영상 추가: {summary['videosAdded']}개 / 채널 추가: {summary['channelsAdded']}개")
                st.rerun()
            except Exception as e:
                st.error(f"오류 발생: {e}")

# 메인 화면 탭 구성
tab1, tab2 = st.tabs(["🎥 수집된 영상 목록", "📺 수집된 채널 목록"])

with tab1:
    st.subheader(f"영상 목록 ({len(data['videos'])}개)")
    
    # 카테고리 필터
    video_cats = ["전체"] + data["categories"].get("videos", [])
    selected_v_cat = st.selectbox("영상 카테고리 필터", video_cats, key="v_cat_filter")
    
    filtered_videos = data["videos"]
    if selected_v_cat != "전체":
        filtered_videos = [v for v in filtered_videos if v.get("category") == selected_v_cat]

    for item in filtered_videos:
        with st.expander(f"🎬 {item.get('title', '제목 없음')} | 조회수: {item.get('viewCount', 0):,}회"):
            col1, col2 = st.columns([1, 2])
            with col1:
                if item.get("thumbnail"):
                    st.image(item["thumbnail"], use_column_width=True)
                st.markdown(f"[YouTube에서 보기]({item.get('url')})")
            with col2:
                st.write(f"**채널명:** {item.get('channelTitle')}")
                st.write(f"**좋아요:** {item.get('likeCount', 0):,} | **댓글:** {item.get('commentCount', 0):,}")
                
                # 메모 및 별점 관리
                note = st.text_area("메모", value=item.get("note", ""), key=f"note_{item['id']}")
                rating = st.slider("별점", 0, 5, value=item.get("rating", 0), key=f"rate_{item['id']}")
                
                col_btn1, col_btn2 = st.columns(2)
                if col_btn1.button("저장", key=f"save_{item['id']}"):
                    item["note"] = note
                    item["rating"] = rating
                    save_data(data)
                    st.toast("저장되었습니다!")
                
                if col_btn2.button("삭제", key=f"del_{item['id']}"):
                    data["videos"] = [v for v in data["videos"] if v["id"] != item["id"]]
                    save_data(data)
                    st.rerun()

with tab2:
    st.subheader(f"채널 목록 ({len(data['channels'])}개)")
    
    channel_cats = ["전체"] + data["categories"].get("channels", [])
    selected_c_cat = st.selectbox("채널 카테고리 필터", channel_cats, key="c_cat_filter")
    
    filtered_channels = data["channels"]
    if selected_c_cat != "전체":
        filtered_channels = [c for c in filtered_channels if c.get("category") == selected_c_cat]

    for item in filtered_channels:
        with st.expander(f"📢 {item.get('title', '채널명 없음')} | 구독자: {item.get('subscriberCount', 0):,}명"):
            col1, col2 = st.columns([1, 2])
            with col1:
                if item.get("thumbnail"):
                    st.image(item["thumbnail"], use_column_width=True)
                st.markdown(f"[채널 방문하기]({item.get('url')})")
            with col2:
                st.write(f"**설명:** {item.get('description', '')[:100]}...")
                st.write(f"**총 영상 수:** {item.get('videoCount', 0):,}개")
                
                if st.button("채널 삭제", key=f"del_c_{item['id']}"):
                    data["channels"] = [c for c in data["channels"] if c["id"] != item["id"]]
                    save_data(data)
                    st.rerun()
