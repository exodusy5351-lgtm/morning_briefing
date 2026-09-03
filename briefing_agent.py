import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import ssl
import os
import sys
import re
import json
import difflib
import hashlib
from html.parser import HTMLParser
import time
import base64
import concurrent.futures
import threading

# Windows 콘솔(cp949) 환경에서 제목/본문에 포함된 특수 유니코드 문자(예: ⋯, —)를
# print()로 출력하다 UnicodeEncodeError가 발생해 해당 테마 수집 전체가 통째로 유실되는 것을 방지
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_LLM_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4)
_QUOTA_LOCK = threading.Lock()
# =========================================================================
# Gmail API 연동 함수 (지점장 아침열기 메일 연동 모듈)
# =========================================================================
def fetch_latest_morning_email():
    """
    Gmail API를 이용해 가장 최근 '아침열기' 또는 '영업방향' 메일을 읽어와 본문을 반환합니다.
    최초 1회 실행 시 웹 브라우저가 열리며 구글 계정 인증(token.json 생성)이 진행됩니다.
    """
    if CI_MODE:
        print("[CI] Gmail 연동 생략")
        return None
    creds = None
    token_path = 'token_gmail.json'
    credentials_path = 'credentials.json'

    if not os.path.exists(credentials_path):
        return None

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
            
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                print("\n[Gmail 인증] 최초 1회 브라우저 구글 인증을 진행합니다...")
                flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(token_path, 'w') as token:
                token.write(creds.to_json())

        service = build('gmail', 'v1', credentials=creds)

        # '아침열기' 또는 '영업' 키워드가 들어간 최신 메일 1건 검색
        query = 'subject:(아침열기 OR 영업방향 OR 절판 OR 6월 OR 7월)'
        results = service.users().messages().list(userId='me', q=query, maxResults=3).execute()
        messages = results.get('messages', [])

        if not messages:
            # 검색어가 마땅치 않은 경우 최근 수신/발신 메일 조회
            results = service.users().messages().list(userId='me', maxResults=5).execute()
            messages = results.get('messages', [])

        if not messages:
            return None

        # 가장 최신 메일 본문 추출
        msg_id = messages[0]['id']
        message = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
        
        snippet = message.get('snippet', '')
        payload = message.get('payload', {})
        body_text = ""

        def extract_text(part):
            if part.get('mimeType') == 'text/plain':
                data = part.get('body', {}).get('data', '')
                if data:
                    return base64.urlsafe_b64decode(data.encode('ASCII')).decode('utf-8', errors='ignore')
            elif 'parts' in part:
                for subpart in part['parts']:
                    res = extract_text(subpart)
                    if res:
                        return res
            return ""

        body_text = extract_text(payload)
        if not body_text:
            body_text = snippet

        print(f"      [Gmail 연동 성공] 아침열기 메일 본문 수집 완료 ({len(body_text)}자)")
        return body_text

    except Exception as e:
        print(f"      [Gmail 연동 참고] 메일 수집 중 건너뜀: {e}")
        return None

# =========================================================================
# .env 파일 즉시 로딩 (모든 환경변수 설정보다 가장 먼저 실행)
# =========================================================================
def _load_dotenv_early():
    """모듈 임포트 직후 .env 전체를 os.environ에 로딩 (로컬 실행 대응)"""
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip('"').strip("'")
                        if k and v and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass
_load_dotenv_early()  # ← 모듈 로드 즉시 실행

# =========================================================================
# 브리핑 기본 타이틀 정의
# =========================================================================
BRIEFING_TITLE = "북부영업단 아침열기 News"
# Notion 토큰/DB ID: 함수 내부에서 동적으로 읽음 (publish_to_notion_db 참고)
# → 모듈 초기화 시점이 아니라 실제 호출 직전에 os.environ 참조
NOTION_TOKEN = ""        # 하위 호환용 더미 — 실제값은 publish_to_notion_db() 내부에서 로딩
NOTION_DATABASE_ID = "" # 하위 호환용 더미 — 실제값은 publish_to_notion_db() 내부에서 로딩


# =========================================================================
# 구글 스프레드시트 & 글라이드 연동 설정
# =========================================================================
# 구글 스프레드시트 ID 입력 (URL의 d/ 와 /edit 사이의 32자리 이상 키값)
GOOGLE_SPREADSHEET_ID = ""


# =========================================================================
# 뉴스 카테고리 구성 (8대 실전 테마 + 2대 신규 테마 = 총 10대 실전 테마)
# =========================================================================
RSS_BASE_URL = "https://news.google.com/rss/search"

CATEGORIES = {
    "silson": {
        "label": "제도·정책 이슈",
        "query": '("실손" OR "실손보험" OR "건강보험" OR "금감원" OR "도수치료") ("개정" OR "변경" OR "사각지대" OR "급여화" OR "자기부담") -MOU -협약 -인사 -동정 -주가 -코스피 -실적 -개원 -봉사 -지사 -지부 -지역본부 -출장소 -캠페인',
        "badge_color": "#3B82F6",  # Blue
        "badge_bg": "rgba(59, 130, 246, 0.1)"
    },
    "fss_reform": {
        "label": "제도·정책 이슈",
        "query": '("금융감독원" OR "금감원" OR "금융위원회") ("가이드라인" OR "개정" OR "간소화" OR "경고" OR "기준") -MOU -협약 -주가 -인사 -동정 -실적 -개원',
        "badge_color": "#3B82F6",  # Blue
        "badge_bg": "rgba(59, 130, 246, 0.1)"
    },
    "hospital_cost": {
        "label": "질병·치료비 리얼리티",
        "query": '("본인부담" OR "비급여" OR "병원비" OR "약제비" OR "수술비" OR "입원 난민" OR "치료비 폭탄") ("부담" OR "사각지대" OR "실태" OR "급증" OR "쓸 약" OR "치료 옵션") -MOU -협약 -개원 -주가 -실적 -봉사 -지사 -지부 -지역본부 -출장소 -캠페인',
        "badge_color": "#10B981",  # Emerald/Green
        "badge_bg": "rgba(16, 185, 129, 0.1)"
    },
    "caregiving": {
        "label": "간병·돌봄 대란",
        "query": '("간병비" OR "간병인" OR "간병파산" OR "간병지옥" OR "요양병원 간병") ("월 300" OR "월 400" OR "급증" OR "부담" OR "일당" OR "사각지대" OR "파산") -MOU -협약 -지자체 -봉사 -지사 -지부',
        "badge_color": "#F59E0B",  # Amber
        "badge_bg": "rgba(245, 158, 11, 0.1)"
    },
    "medtech": {
        "label": "질병·치료비 리얼리티",
        "query": '("암" OR "대장암" OR "신약" OR "전이" OR "폐암" OR "유방암") (급여 OR 비급여 OR 치료비 OR 유병자 OR 재발 OR "쓸 약" OR "치료 옵션") -MOU -주가 -개원 -프로필 -실적',
        "badge_color": "#10B981",  # Emerald/Green
        "badge_bg": "rgba(16, 185, 129, 0.1)"
    },
    "reform_insurance": {
        "label": "제도·정책 이슈",
        "query": '("건강보험" OR "건보 재정" OR "필수의료" OR "의료개혁" OR "건보료") ("개정" OR "급여" OR "사각지대" OR "부담") -지자체 -MOU -동정 -인사 -주가 -실적 -지사 -지부 -지역본부 -출장소 -캠페인',
        "badge_color": "#3B82F6",  # Blue
        "badge_bg": "rgba(59, 130, 246, 0.1)"
    },
    "product_trend": {
        "label": "상품·시장 동향",
        "query": '("보험" OR "특약" OR "담보" OR "수술비") ("신상품" OR "절판" OR "한도축소" OR "보장강화" OR "인상") -대출 -주담대 -금리 -이자 -저당 -실적발표 -분기 -주식 -목표주가 -영업이익 -주가 -MOU',
        "badge_color": "#06B6D4",  # Cyan
        "badge_bg": "rgba(6, 182, 212, 0.1)"
    },
    "motivation": {
        "label": "성공·동기부여",
        "query": '("보험왕" OR "MDRT" OR "삼성화재 RC" OR "보장분석" OR "설계사") ("성공" OR "노하우" OR "미담" OR "사례") -주가 -주식 -실적 -분기',
        "badge_color": "#C026D3",  # Fuchsia
        "badge_bg": "rgba(192, 38, 211, 0.1)"
    },
    "assembly_petition": {
        "label": "국회청원 (비급여/급여화)",
        "query": '"국민동의청원" OR "국회 청원" ("급여화" OR "비급여" OR "치료비" OR "신약")',
        "badge_color": "#EC4899",  # Pink
        "badge_bg": "rgba(236, 72, 153, 0.1)"
    },
    "youtube": {
        "label": "유튜브 핫이슈",
        "query": "", # 유튜브 검색 스크래퍼로 별도 진행
        "badge_color": "#EF4444",  # Red
        "badge_bg": "rgba(239, 68, 68, 0.1)"
    }
}

# 브라우저 헤더 설정
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7'
}

def are_titles_similar(title1, title2, threshold=0.35):
    """두 기사 제목의 유사도를 계산하여 중복 여부 판정 (동일 기관/병원명 중복 원천 차단)"""
    t1 = re.sub(r'[^a-zA-Z0-9가-힣]', '', title1).lower()
    t2 = re.sub(r'[^a-zA-Z0-9가-힣]', '', title2).lower()
    
    # 1. 동일 병원/기관명 중복 검사 (예: 은평성모병원, 백병원 등 동일 기관 관련 기사는 1건만 유지)
    hospitals = re.findall(r'[가-힣]+병원|[가-힣]+의료원|[가-힣]+센터', title1)
    for h in hospitals:
        if len(h) >= 3 and h in title2:
            return True

    # 2. SequenceMatcher 기반 검사
    ratio = difflib.SequenceMatcher(None, t1, t2).ratio()
    if ratio >= threshold:
        return True
        
    # 3. 핵심 단어 교집합(Overlap) 검사
    words1 = set(w for w in re.findall(r'[가-힣a-zA-Z0-9]{2,}', title1.lower()) if len(w) >= 2)
    words2 = set(w for w in re.findall(r'[가-힣a-zA-Z0-9]{2,}', title2.lower()) if len(w) >= 2)
    
    stop_words = {"기자", "뉴스", "보도", "오늘", "내일", "관련", "위한", "대한", "통해", "경우", "최대", "국내", "최초"}
    words1 = words1 - stop_words
    words2 = words2 - stop_words

    if words1 and words2:
        intersection = words1.intersection(words2)
        if len(intersection) >= 2:
            return True
            
        union = words1.union(words2)
        if len(union) > 0 and (len(intersection) / len(union)) >= 0.22:
            return True
            
    return False

def generate_daily_insight(data):
    """
    오늘 수집된 10대 실전 테마의 최신 기사 제목과 팩트를 종합하여
    Gemini 3.5 Flash가 매일 아침 가장 임팩트 있는 시장 이슈를 반영한 100% 동적 Market Insight 한마디를 생성
    """
    # 1순위: Gmail 아침열기 메일 연동 검사
    email_body = fetch_latest_morning_email()
    if email_body:
        eb_lower = email_body.lower()
        phrases = []
        if "절판" in eb_lower:
            phrases.append("'절판'과 '조기 진도관리'")
        if "인상" in eb_lower or "보험료" in eb_lower:
            phrases.append("7월 예정된 보험료 인상 이슈")
        if "수술비" in eb_lower or "종수술비" in eb_lower:
            phrases.append("회당지급 종수술비 및 5대수술비 절판")
        if "순통치" in eb_lower or "비통치" in eb_lower:
            phrases.append("업계 최고 순통치(MRI/CT/재활) 플랜")

        if phrases:
            focus_str = ", ".join(phrases)
            return f"★ [오늘의 한마디 (북부영업단 핵심 영업방향)] {focus_str}를 명확한 소구점으로 삼아, 고객 의사결정을 선제적으로 앞당기는 타이밍 세일즈를 전개하십시오!"

    # 2순위: 오늘 수집된 실제 팩트 기사들을 모아서 Gemini 3.5 Flash 기반 동적 Market Insight 생성
    top_facts = []
    for cat_id, items in data.items():
        if cat_id in ["youtube"]:
            continue
        for item in items[:2]:
            t = item.get("title", "")
            ins = item.get("insight", "")
            if t:
                top_facts.append(f"- [{item.get('category_label', '뉴스')}] {t} (영업포인트: {ins[:60]})")

    if top_facts:
        # --no-gemini 플래그 시 Gemini 호출 완전 생략 → 폴백으로 즉시 이동
        if DISABLE_GEMINI_FLAG:
            print("      [Market Insight] --no-gemini 플래그: Gemini 호출 건너뜀 → 폴백 로테이션 구동")
        else:
            api_key = get_gemini_api_key()
            if api_key:
                try:
                    from google import genai
                    from google.genai import types
                    import concurrent.futures

                    client = genai.Client(api_key=api_key)
                    facts_str = "\n".join(top_facts[:6])

                    prompt = f"""
                    당신은 보험 영업 현장을 지휘하는 명쾌하고 날카로운 최고 세일즈 전략가입니다.
                    오늘 아침 수집된 최신 보험·의료·제도·시즌 팩트 기사들을 바탕으로, 삼성화재 설계사(RC)들이 오늘 아침 아침열기 시간에 마음에 새길 1~2문장의 강력한 '오늘의 한마디 (Market Insight)'를 생성하십시오.

                    [오늘 아침 주요 팩트 기사 목록]
                    {facts_str}

                    [작성 조건]
                    1. 오늘 수집된 기사들 중 가장 임팩트 있는 이슈(예: 폭염/절세/비급여 신약/실손 개정/수술비 등)의 팩트를 1개 직접 자연스럽게 녹여내십시오.
                    2. 진상 고객 상담이나 틈새 보장 점검에 자신감을 주며, 설계사의 보장 전달 가치를 격려하는 매일 색다르고 전문적인 톤으로 작성하십시오.
                    3. 문장 시작에 '★ [오늘의 한마디] ' 형식을 반드시 유지하십시오.

                    [출력 JSON 형식]
                    {{
                      "market_insight": "★ [오늘의 한마디] (오늘자 팩트를 반영한 1~2문장)"
                    }}
                    """

                    def call_gemini_insight():
                        return client.models.generate_content(
                            model='gemini-3.5-flash',
                            contents=prompt,
                            config=types.GenerateContentConfig(
                                response_mime_type="application/json",
                                temperature=0.4,
                            ),
                        )

                    future = _LLM_POOL.submit(call_gemini_insight)
                    try:
                        response = future.result(timeout=8.0)
                    except concurrent.futures.TimeoutError:
                        future.cancel()
                        raise

                    res_data = json.loads(response.text)
                    insight_res = res_data.get("market_insight", "").strip()
                    if insight_res and "오늘의 한마디" in insight_res:
                        print(f"      [Gemini 3.5 Flash] 오늘자 팩트 종합 동적 Market Insight 생성 성공!")
                        return insight_res
                except Exception as e:
                    print(f"      [Market Insight Gemini 동적 생성 실패] {e} -> 폴백 로테이션 구동")

    # 3순위: Gemini 미구동 시 10가지 다채로운 폴백 문구 중 날짜(day) 기준 로테이션 선택 (중복 방지)
    fallback_pool = [
        "★ [오늘의 한마디] 폭염·계절 질환 급증과 비급여 치료비 증가 속에서, 고객 가정의 재정 안정망을 사전에 메워주는 틈새 보장점검이 최고의 세일즈 무기입니다.",
        "★ [오늘의 한마디] 타사 신상품 출시와 절판 이슈가 이어지는 요즘, 우리 상품의 보장 우위와 심사 강점을 명확히 짚어주는 비교 화법이 계약 성사의 핵심입니다.",
        "★ [오늘의 한마디] 간병 비용의 가파른 상승세와 국가 돌봄 대란 흐름 속에서, 고객 가정의 재정적 붕괴를 사전에 차단하는 간병 자산 설계는 설계사 본연의 숭고한 가치입니다.",
        "★ [오늘의 한마디] 파워 블로거 및 현업 우수 설계사들의 담보 비교 자료를 바탕으로, 타사 대비 삼성화재 상품군이 가지는 보장 한도와 심사 우위를 부각하여 제안하십시오.",
        "★ [오늘의 한마디] 고액 신약 급여 지정 논의 활성화 뒤에는 수많은 환자들의 높은 비급여 비용 장벽이 존재합니다. 고객이 돈 걱정 없이 최고의 신치료를 선택하도록 돕는 고액 특약 준비를 권유하십시오.",
        "★ [오늘의 한마디] 실손보험 개정과 금융당국의 보장 가이드라인 변경이 가속화될수록, 기존 가입 조건의 이점과 변경 후 보장 틈새를 비교 설명하는 보장 컨설팅이 강력한 힘을 발휘합니다.",
        "★ [오늘의 한마디] 갑작스러운 질병과 사고는 예고 없이 찾아옵니다. 매일 아침 업데이트되는 최신 의료 팩트를 바탕으로 고객의 내일을 든든히 채워가는 보장 전문가가 됩시다.",
        "★ [오늘의 한마디] 회당 지급 수술비 및 순통치 플랜의 보장 우위를 명확히 전달하여, 망설이는 고객의 의사결정을 선제적으로 앞당기는 타이밍 세일즈를 전개하십시오.",
        "★ [오늘의 한마디] 고객이 미처 알지 못했던 비급여 신약 및 첨단 치료비 틈새를 찾아 점검해 드리는 것이 바로 전업 설계사가 전달하는 최고의 가치이자 신뢰입니다.",
        "★ [오늘의 한마디] 매일 아침 수집되는 최신 정책과 시장 트렌드는 현장에서 고객의 마음을 여는 가장 확실한 열쇠입니다. 자부심을 가지고 현장으로 나아갑시다."
    ]
    
    day_idx = datetime.now().day % len(fallback_pool)
    return fallback_pool[day_idx]

def generate_news_summary(title, cat_id, summary_text=""):
    """
    [전속 & GA 공용 high-credibility 팩트 중심 요약 시스템]
    억지 셀링 인사이트 및 겉도는 멘트 100% 제거!
    기사의 실제 원문/요약 팩트만을 정갈하고 명확하게 1~2줄로 전달하여
    전속 및 GA 모든 설계사들이 100% 신뢰할 수 있는 팩트 브리핑으로 구성합니다.
    """
    clean_title = re.sub(r'\[.*?\]|\(.*?\)', '', title).strip()
    
    if summary_text and len(summary_text) > 20:
        # 실제 추출된 og:description / 메타태그 기사 팩트 요약 텍스트 사용
        fact_summary = summary_text.strip()
    else:
        # 제목 기반의 정갈한 팩트 설명
        fact_summary = f"본 기사는 '{clean_title}' 관련 최신 시장 및 정책 동향의 핵심 팩트를 담고 있습니다."

    return fact_summary

def evaluate_article_cot(title, body="", hook=True):
    """
    보험설계사 팀('북부영업단') 아침 뉴스 큐레이션용 6단계 Chain of Thought (CoT) 평가 엔진
    카테고리를 먼저 정하지 않고, 영업 관련성 체크리스트(a~f)와 제외 조건부터 검증한 뒤 채택 여부 및 카테고리를 최종 부착합니다.
    """
    clean_title = re.sub(r'\[.*?\]|\(.*?\)', '', title).strip()
    text = (clean_title + " " + (body or "")).lower()

    # STEP 1. 사실 요약
    fact_summary = clean_title if not body or len(body) < 20 else body[:120].strip()

    # STEP 2. 영업 관련성 체크리스트 대조 (a ~ f)
    hits = []

    # a. 구체적 수치 (완치율/재발률/생존율/손해율/청구건수/비용 수치)
    if re.search(r'\d+%\s*|\d+만|\d+억|\d+기|\d+배|\d+원|\d+건|\d+대', text):
        hits.append("a")

    # b. 신약·신의료기술 등장 및 비급여/급여제한 고비용
    if any(k in text for k in ["신약", "표적", "항암", "면역항암", "로봇수술", "중입자", "비급여", "치료비", "수술비", "약제비", "치료제", "본인부담", "고액", "암", "희귀질환", "난치"]):
        hits.append("b")

    # c. 실손보험/기존 보장의 사각지대
    if any(k in text for k in ["실손", "사각지대", "청구 거절", "보장 안 됨", "지급 제한", "본인부담", "면책", "제한", "부담"]):
        hits.append("c")

    # d. 제도/정책 변경 -> 가입/리모델링 필요성 생성
    if any(k in text for k in ["제도", "개정", "급여화", "관리급여", "정책", "가이드라인", "기준 변경", "전환", "금융감독원", "금감원", "복지부"]):
        hits.append("d")

    # e. 고액 치료비 실사례, 환자/보호자 감정 공감 및 위기의식 형성
    if any(k in text for k in ["재발", "투병", "사연", "환자", "생존", "완치", "눈물", "고통", "가계 붕괴", "전이", "위험", "발병", "사망"]):
        hits.append("e")

    # f. 경쟁사 신상품, 절판 임박, 타사 대비 우위 비교
    if any(k in text for k in ["절판", "신상품", "담보", "한도", "특약", "우위", "비교", "업라이팅", "인상", "출시"]):
        hits.append("f")

    hits = sorted(list(set(hits)))

    # STEP 3. 제외 조건 확인 (하나라도 해당하면 무조건 탈락)
    exclusion = False
    # 병원/의료진 단순 개원/수상 홍보이고 체크리스트 0개
    if any(k in text for k in ["개소", "도입", "가동식", "감사패", "표창", "개원"]) and len(hits) == 0:
        exclusion = True
    # 단순 기술/기기 소개이고 비용/보장 언급 전혀 없음
    if "기술" in text and not any(k in text for k in ["비용", "보장", "보험", "급여", "부담", "치료비", "암", "재발"]) and len(hits) < 2:
        exclusion = True
    # 지자체/구청 방문진료 등 단체 복지 사업 기사 (민간 보험 세일즈 무관)
    if any(k in text for k in ["방문진료", "구청", "주민센터", "동사무소", "보건소 지원", "복지관", "무료 진료", "돌봄 바우처", "동대문구", "성북구"]) and not any(ik in text for ik in ["실손", "수술비", "암보험", "비급여", "담보", "약관", "특약", "손해율", "리모델링"]):
        exclusion = True
    # 해외 황당 사건사고 / 해외 기이한 의료비 가십 뉴스 (국내 민간 보험 세일즈 무관) 차단
    if any(k in text for k in ["뱀에 물려", "미국 병원비", "해외 황당", "총기", "마약", "해외 사연", "미국 의료비"]) and not any(ik in text for ik in ["해외여행자보험", "실손", "건강보험"]):
        exclusion = True
    # 지역 공단 지사/지부 등 지엽적 기관 뉴스 원천 차단 (단, 중요 제도 변경 이슈 동반 시 예외 허용)
    local_branch_keywords = [
        "지사", "지부", "지역본부", "출장소", "사업소", "분회", "지회", "보건지소", "보건진료소",
        "국민건강보험공단 경주", "건보공단 경주", "건보 경주", "건보공단 지사", "공단 지사",
        "주민자치", "사회복지관"
    ]
    if any(k in text for k in local_branch_keywords) and not any(
        ik in text for ik in ["실손 개정", "4세대 실손", "급여화 확정", "본인부담상한제 개정"]
    ):
        exclusion = True
    # 국가/거시경제 단위 보험(국가재보험, 수출보험, 지정학적 리스크 대응용 국가지원보험 등) - 개인 보험영업과 무관
    macro_insurance_keywords = [
        "국가 지원 보험", "국가재보험", "국가 재보험", "수출보험", "무역보험", "전쟁보험", "테러보험",
        "지정학적 위험", "지정학적 리스크", "정치적 리스크"
    ]
    if any(k in text for k in macro_insurance_keywords):
        exclusion = True
    # "국가"+"보험"이 함께 등장하지만 개인 보험상품 관련 단어가 전혀 없는 거시/제도 차원 기사 배제
    # (주의: "보장"처럼 지나치게 일반적인 단어는 예외 목록에서 제외 — 크롤링 실패 폴백 문구 등과
    #  우연히 겹쳐 실제로는 무관한 기사가 예외 처리되어 통과되는 사고가 있었음)
    if "국가" in text and "보험" in text and not any(
        pk in text for pk in ["가입", "보험료", "보험금", "특약", "약관", "실손", "청구", "피보험자"]
    ):
        exclusion = True
    # 보험사 자체의 경영/재무/규제 이슈(예보료, 지급여력비율, 실적, 지배구조 등) - 개인 고객에게 팔 보장과 무관
    industry_finance_keywords = [
        "예보료", "예금보험료", "지급여력비율", "k-ics", "킥스", "재무건전성", "자본확충",
        "신용등급", "지배구조", "이사회", "주주총회", "배당", "자사주", "실적발표", "분기 실적",
        "영업이익", "당기순이익", "특별기여금"
    ]
    if any(k in text for k in industry_finance_keywords) and not any(
        pk in text for pk in ["가입", "보험료 인상", "특약", "약관", "실손", "담보", "보험금"]
    ):
        exclusion = True
    # 기초자치단체(시/군/구) 단위 복지 지원사업 뉴스 - 정부 보조금 정책이지 민간 보험 세일즈와 무관
    # (주의: "치료비"/"수술비"만으로는 예외 허용하지 않는다 - 지자체 지원금 기사도 흔히 이 단어를 쓰기 때문에
    #  과거에 "동대문구/성북구" 등 특정 구 이름만 나열한 목록 + 치료비/수술비 예외로는 "순천시" 같은
    #  다른 지자체의 지원사업 기사를 걸러내지 못하는 허점이 있었다 — 실제 겪은 버그)
    # 주의: "보험"은 "국민건강보험공단" 등 정부기관명의 부분 문자열로도 흔히 등장해 예외를 무력화시키므로 제외
    if re.search(r'[가-힣]{2,}(시|군|구)[,\s]', clean_title) and any(sk in text for sk in ["지원", "지원금", "지원사업"]) and not any(
        pk in text for pk in ["가입", "특약", "약관", "실손", "보험료", "보험금"]
    ):
        exclusion = True

    # STEP 4. 최종 채택 판단 (체크리스트 2개 이상 또는 주요 질병/시즌 이슈 + 보험/치료비 맥락 키워드 필수 + 제외조건 없음)
    context_keywords = [
        "보험", "실손", "보장", "특약", "담보", "치료비", "수술비", "병원비", "비급여",
        "본인부담", "암", "신약", "간병", "리모델링"
    ]
    has_context = any(k in text for k in context_keywords)
    adopted = (
        (len(hits) >= 2 or any(k in text for k in ["대장암", "폐암", "유방암", "재발", "치료비", "신약", "완치율", "폭염", "온열질환", "장마"]))
        and has_context
        and (not exclusion)
    )

    # STEP 5. 타사 상품 홍보 및 상품 소개 기사 판별 (is_promo)
    other_insurers = ["한화생명", "교보생명", "동양생명", "DB손보", "DB손해보험", "현대해상", "KB손보", "KB손해보험", "메리츠", "메리츠화재", "흥국화재", "롯데손보", "신한라이프", "라이나생명", "AIA생명", "하나손보"]
    # ★ promo_keywords: 타사 보험사명 + 이 중 하나라도 있으면 is_promo=True 후보
    promo_keywords = [
        "출시", "개정 출시", "론칭", "내놨", "내놓", "출시 예정",
        "선뵈", "신상품", "보장 강화", "배타적사용권",
        "가입자 끌어", "3단 보장", "3단보장", "특약 신설",
        "혜택 강화", "상품 선보여", "상품 개편", "상품 리뉴얼"
    ]
    # ★ explicit_product_kw: 타사 사명 없어도 단독으로 is_promo=True
    explicit_product_kw_list = [
        "3단 보장", "3단보장", "신상품", "배타적사용권", "보장 강화 출시"
    ]

    is_promo = False
    if adopted:
        has_other_insurer = any(ins in text for ins in other_insurers)
        has_promo_kw = any(pk in text for pk in promo_keywords)
        explicit_product_kw = any(ep in text for ep in explicit_product_kw_list)

        if (has_other_insurer and has_promo_kw) or explicit_product_kw:
            if "삼성화재" not in text:
                is_promo = True

    # STEP 5.5. 카테고리 라벨 부착
    category = None
    if adopted:
        if is_promo or any(k in text for k in ["상품 출시", "신상품", "절판", "한도축소", "한도 축소", "특약", "담보", "보험 상품", "상품 승부", "통합치료비 상품", "손해율", "자동차보험", "차보험", "보험료 인상", "보험료", "3단 보장", "3단보장"]):
            category = "상품·시장 동향"
        elif any(k in text for k in ["폭염", "온열질환", "물놀이", "장마", "태풍", "식중독", "빙판길", "낙상", "환절기", "독감"]):
            category = "시즌·이슈"
        elif any(k in text for k in ["실손 개정", "금감원", "건보", "급여화", "관리급여", "가이드라인", "제도 변경", "정책"]):
            category = "제도·정책 이슈"
        elif any(k in text for k in ["간병비", "간병인", "간병파산", "간병지옥", "요양병원 간병", "간병"]):
            category = "간병·돌봄 대란"
        elif any(k in text for k in ["암", "재발", "신약", "완치", "치료비", "수술비", "전이", "항암", "표적", "중입자", "투병", "병원비", "입원", "의료비", "본인부담", "억", "날벼락", "환자", "사연", "부담", "치료"]):
            category = "질병·치료비 리얼리티"
        elif any(k in text for k in ["성공", "동기부여", "mdrt", "보험왕", "미담", "설계사"]):
            category = "성공·동기부여"
        else:
            category = "상품·시장 동향"

    # STEP 6. 영업포인트 생성 (채택된 기사만 - is_promo 및 카테고리 프롬프트 분기 처리)
    sales_hook = ""
    if adopted and hook:
        # 1차 시도: Gemini 3.5 Flash CoT 기반 팩트 추출 영업 화법 생성
        gemini_hook = generate_sales_hook_gemini(clean_title, body, is_promo=is_promo, category=category)
        if gemini_hook:
            sales_hook = gemini_hook
        else:
            # 2차 시도: 스마트 팩트 화법 파서
            sales_hook = generate_smart_fact_hook(clean_title, body, category, is_promo=is_promo)

    return {
        "title": title,
        "adopted": adopted,
        "checklist_hits": hits,
        "category": category if adopted else None,
        "sales_hook": sales_hook,
        "fact_summary": fact_summary,
        "is_promo": is_promo
    }


def get_gemini_api_key():
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k and v and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass
    return os.environ.get("GEMINI_API_KEY")


# Gemini API 및 수집 통계 트래킹 객체
GEMINI_STATS = {
    "total_articles": 0,
    "gemini_success": 0,
    "fallback_crawling_failed": 0,
    "fallback_api_failed": 0,
    "crawling_failed_publishers": {}
}

def generate_sales_hook_gemini(title: str, article_body: str, is_promo: bool = False, category: str = None) -> str:
    """
    gemini-3.5-flash 기반 CoT 현장 화법 생성 함수
    - 과잉진료/손해율/누수 기사인 경우 실손 악화 보장대비 톤으로 분기
    - is_promo=True 인 경우 경쟁 참고 톤
    - Rate limit (429) 발생 시 1회 지연 재시도 (Retry) 구동
    """
    global GEMINI_STATS, GEMINI_QUOTA_USED
    GEMINI_STATS["total_articles"] += 1
    
    # 디버그 플래그(--no-gemini) 또는 안전 쿼터 16회 소진 시 스마트 파서로 자동 전환
    if DISABLE_GEMINI_FLAG:
        print(f"      [디버그 플래그 --no-gemini] Gemini API 호출 건너뜀 -> 메인 스마트 파서 구동")
        return ""

    with _QUOTA_LOCK:
        if GEMINI_QUOTA_USED >= GEMINI_QUOTA_LIMIT:
            print(f"      [Gemini 안전 쿼터 소진 ({GEMINI_QUOTA_USED}/{GEMINI_QUOTA_LIMIT}회)] -> 메인 스마트 파서 구동")
            GEMINI_STATS["fallback_api_failed"] += 1
            return ""
        GEMINI_QUOTA_USED += 1
    
    api_key = get_gemini_api_key()
    text_content = (title + "\n" + (article_body or "")).strip()
    
    source_pub = title.rsplit(' - ', 1)[1] if ' - ' in title else "알 수 없음"

    # 1단계: 입력 검증
    if len(text_content) < 10:
        GEMINI_STATS["fallback_crawling_failed"] += 1
        GEMINI_STATS["crawling_failed_publishers"][source_pub] = GEMINI_STATS["crawling_failed_publishers"].get(source_pub, 0) + 1
        print(f"      [Gemini API Skip] 본문 크롤링 부실({len(text_content)}자) -> 언론사: {source_pub} -> 스마트 파서 구동")
        return ""

    if not api_key:
        GEMINI_STATS["fallback_api_failed"] += 1
        return ""

    # 과잉진료 / 실손 손해율 / 비급여 누수 관련 기사 감지
    is_overtreatment = any(ok in text_content.lower() for ok in ["과잉진료", "손해율", "도덕적 해이", "도수치료", "비급여 누수", "의사 탓", "보험금 누수"])

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        if is_overtreatment:
            prompt = f"""
            당신은 실손보험과 보장분석 전문가입니다.
            아래 기사는 실손 손해율 악화, 과잉진료, 비급여 누수 문제를 다루는 업계 구조적 기사입니다.
            단순 상품 비교나 타사 차별점 언급 대신, "과잉진료 단속 및 실손 손해율 악화로 인해 향후 비급여 보장 축소 및 심사 강화가 우려되니, 기존 보장을 사전에 점검하고 대비해야 한다"는 세일즈 현장 화법 포인트를 1~2문장으로 작성하십시오.

            [기사 제목] {title}
            [기사 본문] {article_body[:1000] if article_body else '본문 없음'}

            [작성 조건]
            1. 상품 비교 톤(타사 우위 부각 등)을 절대 사용하지 마십시오.
            2. 손해율 악화나 과잉진료 통제 팩트를 바탕으로, 고객의 기존 실손 및 비급여 보장 틈새를 사전에 점검하도록 설득하는 1~2문장의 전문 화법을 제시하십시오.

            [출력 JSON 형식]
            {{
              "fact_extracted": "기사 내 추출 팩트",
              "sales_hook": "💡 현장 화법 포인트: (실손 손해율 악화 및 보장대비 1~2문장)"
            }}
            """
        elif is_promo:
            prompt = f"""
            당신은 삼성화재 보험 영업 현장을 지원하는 세일즈 마케팅 에디터입니다.
            아래 기사는 경쟁 타사의 상품 출시 및 홍보 기사입니다.
            설계사가 고객 상담 시 경쟁사 상품 대비 우리(삼성화재) 보장 우위를 점검하거나 타사 동향을 견제할 수 있는 1~2문장의 '경쟁 비교/견제용 참고 메모' 톤의 화법을 작성하십시오.

            [기사 제목] {title}
            [기사 본문] {article_body[:1000] if article_body else '본문 없음'}

            [작성 조건]
            1. 타사 상품의 팩트를 언급하되, "경쟁사 A사에서 이런 상품을 출시했으니, 우리 상품 대비 보장 차별점 및 우위를 미리 점검해 두세요" 톤으로 작성하십시오.
            2. 인용한 팩트를 포함하여 설계사가 타사 상품 문의 고객에게 답변하거나 비교 설명할 수 있는 자연스러운 참고 화법을 제시하십시오.

            [출력 JSON 형식]
            {{
              "fact_extracted": "기사 내 추출 팩트",
              "sales_hook": "💡 경쟁사 상품 참고 메모: (비교/견제용 1~2문장)"
            }}
            """
        else:
            prompt = f"""
            당신은 보험 영업 현장을 지원하는 마케팅 컨설턴트입니다.
            아래 기사 정보를 분석하여 설계사가 고객에게 바로 전달할 1~2문장의 고유 현장 화법을 작성하십시오.

            [기사 제목] {title}
            [기사 본문] {article_body[:1000] if article_body else '본문 없음'}

            [작성 조건]
            1. 기사에 존재하는 구체적 수치(%, 원, 건 등) 또는 고유명사(병명, 약제명, 기관명, 특약명)를 최소 1개 직접 인용하십시오.
            2. 인용한 팩트를 바탕으로 고객에게 질문하거나 보장 점검을 제안하는 1~2문장의 자연스러운 현장 화법 포인트를 작성하십시오.

            [출력 JSON 형식]
            {{
              "fact_extracted": "기사 내 추출 팩트",
              "sales_hook": "💡 현장 화법 포인트: (팩트를 포함한 1~2문장)"
            }}
            """

        def call_gemini():
            if client is None:
                raise RuntimeError("Gemini client not initialized")
            response = client.models.generate_content(
                model='gemini-3.5-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )
            return response

        import concurrent.futures
        import time

        # Rate Limit (429) 대비 최대 2회 지연 재시도 (Retry)
        for attempt in range(2):
            try:
                future = _LLM_POOL.submit(call_gemini)
                try:
                    response = future.result(timeout=8.0)
                except concurrent.futures.TimeoutError:
                    future.cancel()
                    raise
                data = json.loads(response.text)
                fact = data.get("fact_extracted")
                hook = data.get("sales_hook")

                if fact and str(fact).lower() != "null" and hook:
                    GEMINI_STATS["gemini_success"] += 1
                    print(f"      [Gemini 3.5 Flash (쿼터 사용 {GEMINI_QUOTA_USED}/{GEMINI_QUOTA_LIMIT})] 화법 생성 성공{' (과잉진료/손해율톤)' if is_overtreatment else ''}: '{title[:20]}...'")
                    return hook.replace('💡 현장 화법 포인트: ', '').replace('💡 현장 화법 포인트:', '').replace('💡 경쟁사 상품 참고 메모: ', '').replace('💡 경쟁사 상품 참고 메모:', '').strip()
                break
            except Exception as e:
                err_msg = str(e)
                if ("429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg) and attempt == 0:
                    print(f"      [Gemini API 429 Rate Limit] 2.5초 대기 후 1회 재시도(Retry) 중...")
                    time.sleep(2.5)
                    continue
                else:
                    GEMINI_STATS["fallback_api_failed"] += 1
                    print(f"      [Gemini API 실패] ({err_msg[:40]}) -> 스마트 파서 구동")
                    return ""

        GEMINI_STATS["fallback_api_failed"] += 1
        return ""
    except Exception as e:
        GEMINI_STATS["fallback_api_failed"] += 1
        return ""


# Gemini API 일일 안전 쿼터 관리자
GEMINI_QUOTA_LIMIT = 16
GEMINI_QUOTA_USED = 0
DISABLE_GEMINI_FLAG = "--no-gemini" in sys.argv
CI_MODE = "--ci" in sys.argv
DRY_RUN = "--dry-run" in sys.argv

def generate_smart_fact_hook(title: str, body: str, category: str, is_promo: bool = False) -> str:
    """
    Gemini API 미구동 시에도 기사 제목과 크롤링 본문에서 수치/고유명사를 파싱하여
    제목 재인용(복붙) 없이 기사별 100% 자연스럽고 고유한 팩트 화법을 생성하는 메인 고도화 스마트 파서
    """
    text = (title + " " + (body or "")).strip()

    # 과잉진료 / 실손 손해율 / 비급여 누수 관련 기사 감지 전용 폴백 톤 (최우선 분기)
    is_overtreatment = any(ok in text for ok in ["과잉진료", "손해율", "도덕적 해이", "도수치료", "비급여 누수", "의사 탓", "보험금 누수"])
    if is_overtreatment:
        return "실손 손해율 악화 및 과잉진료 단속 강화로 인해 향후 비급여 보장 축소 및 지급 심사 강화가 우려됩니다. 기존 보유 중인 실손 및 비급여 수술비 보장 한도를 사전에 점검하고 대비하시는 것을 권유합니다."

    if is_promo:
        return "경쟁 타사의 최신 상품 출시 및 특약 한도 변경 동향입니다. 타사의 보장 조건 및 특약 구성을 체크하고, 삼성화재 기존 보장 대비 차별점 및 우위를 사전에 안내해 보세요."
    
    # 완화/지원/상한제 감지 가드레일 (문맥 180도 반대 왜곡 방지)
    relief_kws = ["의료비 부담 완화", "부담 완화", "의료비 완화", "본인부담 상한제", "100만 원 상한제", "100만원 상한제", "지원 조례", "입법예고"]
    is_relief_context = any(rk in text.lower() for rk in relief_kws)

    # 1. 팩트 추출: 순수 숫자 단독("50년", "2000만", "9만")은 엄격 제외하고, 단위 결합 수치(억/원/%) 또는 명사/병명 우선 추출
    meaningful_facts = re.findall(r"\d+(?:만\s?원|억\s?원|%|건|명)|[가-힣A-Za-z0-9]+(?:대장암|위암|폐암|간암|유방암|췌장암|치료제|신약|수술비|치료비|진료비|통원비|급여|비급여|고지의무|리모델링|상한제|입법예고|조례|간병비|간병인|특약)", text)
    unique_facts = []
    stop_kws = {"기자", "뉴스", "오늘", "관련", "위한", "대한", "통해", "경우", "최대", "국내", "최초", "보험", "추진", "완화", "이슈", "뉴스1", "뉴시스", "50년", "년", "1위", "2위", "3위"}
    for f in meaningful_facts:
        if len(f) >= 2 and not f.isdigit() and f not in stop_kws and f not in unique_facts:
            unique_facts.append(f)

    # 2. URL/제목 MD5 해시 기반 4가지 문장 뼈대 템플릿 로테이션 (반복 인상 100% 제거)
    pattern_idx = int(hashlib.md5(title.encode('utf-8')).hexdigest(), 16) % 4
    fact_str = f"({', '.join(unique_facts[:2])})" if unique_facts else ""

    if is_relief_context:
        return f"의료비 부담을 줄이기 위한 지원 및 혜택 정책{fact_str}이 추진되고 있습니다. 지자체/정부 지원 범위와 함께 보유 중인 보험의 보장 틈새를 사전에 안내해 보세요."

    if category == "시즌·이슈":
        templates = [
            f"계절성 질환 및 안전사고 우려{fact_str}가 높아지고 있습니다. 갑작스러운 치료비나 입원비 부담에 대비해 보유 중인 보장 틈새를 사전에 안내해 보세요.",
            f"최근 계절적 위험 요인{fact_str}에 따른 응급실 이용 및 입원 환자가 증가하고 있습니다. 고객님의 응급실 내원비 및 수술비 보장을 사전 점검해 드리는 것이 유리합니다.",
            f"{fact_str if unique_facts else '계절성 위험 이슈'} 발생 가능성에 대비해, 기존 건강보험의 입원일당과 치료비 한도가 충분한지 미리 확인해 보시길 권장합니다.",
            f"계절성 질환 및 사고 위험{fact_str}에 대비하여 고객님의 필수 진단비와 치료비 보장 공백을 사전에 점검해 드릴 것을 추천합니다."
        ]
        return templates[pattern_idx]

    elif category == "질병·치료비 리얼리티":
        templates = [
            f"최근 {fact_str if unique_facts else '고액 비급여 치료비'} 관련 수술 및 진료 부담이 커지고 있습니다. 기존 보장에서 해당 항목이 충분히 커버되는지 사전 점검이 필요한 시점입니다.",
            f"{fact_str if unique_facts else '신의료기술 및 신약 치료'} 적용에 따른 진료비 부담을 대비해, 고객님의 비급여 및 통원비 보장 한도를 사전에 점검해 보세요.",
            f"고액 치료비 발생 가능성이 높은 이슈{fact_str}와 관련하여, 부족한 암 진단비와 간병 틈새 보장을 사전에 안심 설계해 드리는 것을 권유합니다.",
            f"치료 환경 변화{fact_str}로 본인부담액이 커짐에 따라, 보유 중인 건강보험의 보장 실효성과 한도를 사전에 체크해 보시길 권장합니다."
        ]
        return templates[pattern_idx]

    elif category == "제도·정책 이슈":
        templates = [
            f"최근 {fact_str if unique_facts else '보장 가이드라인'} 관련 급여 기준 및 정책 개정 이슈가 주목받고 있습니다. 기존 실손 및 수술비가 새 제도에서도 유지되는지 점검을 권유합니다.",
            f"{fact_str if unique_facts else '금융당국 정책'} 변경에 따라 기존 가입 조건의 차별점과 변경 후 보장 조건을 비교 확인해 보시는 것이 유리합니다.",
            f"건강보험 및 제도 개편 이슈{fact_str}가 본격화되고 있습니다. 보유 중인 보장 자산의 틈새 항목을 사전에 점검해 드리길 바랍니다.",
            f"보장 가이드라인{fact_str}이 새로 적용됨에 따라, 가입 고객님의 기존 실손 및 보장 한도 유지 여부를 사전 체크해 드리는 것을 추천합니다."
        ]
        return templates[pattern_idx]

    elif category == "상품·시장 동향":
        templates = [
            f"최근 {fact_str if unique_facts else '주요 특약'} 조건의 보장 한도 축소 및 절판 전, 타사 비교 우위와 현재 가입 조건의 이점을 빠르게 점검해 보시길 바랍니다.",
            f"시장 동향 변화{fact_str}에 따라 인수 조건 및 한도 변경이 우려됩니다. 현재 가입 중인 특약의 비교 우위를 사전에 점검해 보시는 것이 좋습니다.",
            f"{fact_str if unique_facts else '타사 상품 동향'} 개편 전 기존 가입 조건의 이점을 고객님께 미리 안내해 드리는 것이 세일즈에 유리합니다.",
            f"주요 보장 한도 조정 이슈{fact_str}와 관련하여 타사 대비 삼성화재의 차별점과 보장 우위를 사전에 점검해 보세요."
        ]
        return templates[pattern_idx]

    else:
        return "최근 업데이트된 보장 동향을 바탕으로 고객님의 보장 자산 현황을 점검하고 필요한 준비를 상담해 보세요."




def resolve_google_news_url(google_url, timeout=5):
    # 구글뉴스 RSS URL을 실제 기사 원본 URL로 변환
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ko-KR,ko;q=0.9",
        }
        req = urllib.request.Request(google_url, headers=headers)
        # 리다이렉트를 자동으로 추적
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            final_url = resp.geturl()
            # 구글뉴스가 아닌 실제 기사 URL로 이동된 경우
            if "news.google.com" not in final_url:
                return final_url
            html_bytes = resp.read(20000)  # 앞부분만 읽기
        
        try:
            html = html_bytes.decode("utf-8")
        except UnicodeDecodeError:
            html = html_bytes.decode("euc-kr", errors="replace")
        
        # 구글뉴스 JS redirect에서 실제 URL 추출 시도
        patterns = [
            r'data-n-au="([^"]+)"',
            r'"url":"(https?://(?!news\.google)[^"]+)"',
            r'<a[^>]+href="(https?://(?!news\.google\.com)[^"]+)"[^>]*>',
        ]
        for pat in patterns:
            m = re.search(pat, html)
            if m:
                url = m.group(1).replace('\\u003d', '=').replace('\\u0026', '&')
                if "google.com" not in url:
                    return url
        return None
    except Exception:
        return None


def fetch_article_body(url, max_chars=400, timeout=4):
    # og:description 메타태그 및 네이버 블로그 전용 초고속 파서
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9",
        }
        
        target_url = url
        if "news.google.com" in url:
            resolved = resolve_google_news_url(url)
            if resolved:
                target_url = resolved

        # 네이버 블로그 링크일 경우 모바일 전용 URL로 전환하여 본문 스크래핑 성공률 100%로 보장
        if "blog.naver.com" in target_url and "m.blog.naver.com" not in target_url:
            target_url = target_url.replace("blog.naver.com", "m.blog.naver.com")

        req = urllib.request.Request(target_url, headers=headers)
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            html_bytes = resp.read(50000)  # 앞 50KB 읽기

        try:
            html = html_bytes.decode("utf-8")
        except UnicodeDecodeError:
            html = html_bytes.decode("euc-kr", errors="replace")

        # 1. og:description 메타 태그 검색
        og_desc = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']', html, re.I | re.DOTALL)
        if not og_desc:
            og_desc = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', html, re.I | re.DOTALL)

        if og_desc:
            text = og_desc.group(1).strip()
            text = re.sub(r'\s+', ' ', text)
            if len(text) > 15:
                return text[:max_chars]

        # 2. 네이버 블로그 본문 텍스트 직접 추출 (fallback)
        blog_body = re.search(r'<div[^>]+class=["\'].*?(?:se-main-container|post_main|se-component).*?["\'][^>]*>(.*?)</div>', html, re.I | re.DOTALL)
        if blog_body:
            clean_text = re.sub(r'<[^>]+>', ' ', blog_body.group(1))
            clean_text = re.sub(r'\s+', ' ', clean_text).strip()
            if len(clean_text) > 15:
                return clean_text[:max_chars]

        # 3. 일반 p 태그 텍스트 추출 (fallback)
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html, re.I | re.DOTALL)
        p_texts = [re.sub(r'<[^>]+>', '', p).strip() for p in paragraphs if len(re.sub(r'<[^>]+>', '', p).strip()) > 20]
        if p_texts:
            return " ".join(p_texts)[:max_chars]

        return ""
    except Exception as e:
        return ""




def analyze_youtube_video(title, description, channel=""):
    global GEMINI_QUOTA_USED
    api_key = get_gemini_api_key()
    clean_title = re.sub(r'\[.*?\]|\(.*?\)', '', title).strip()
    
    STOP_WORDS = {
        "이것", "진짜", "놓치면", "필수", "추천", "영상", "분석", "꿀팁", "지금", "오늘", "내일",
        "방법", "이유", "이유는", "가지", "가지고", "하는", "통해", "대한", "관련"
    }

    if DISABLE_GEMINI_FLAG:
        print(f"      [디버그 플래그 --no-gemini] 유튜브 요약 Gemini 호출 건너뜀")
        api_key = None

    if api_key and clean_title:
        with _QUOTA_LOCK:
            if GEMINI_QUOTA_USED >= GEMINI_QUOTA_LIMIT:
                print(f"      [Gemini 안전 쿼터 소진 ({GEMINI_QUOTA_USED}/{GEMINI_QUOTA_LIMIT}회)] 유튜브 요약 폴백")
                api_key = None
            else:
                GEMINI_QUOTA_USED += 1

    if api_key and clean_title:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)

            prompt = (
                f"유튜브 영상 제목: {title}\n"
                f"설명: {description[:500]}\n"
                "요약과 해시태그를 JSON으로 반환하세요."
            )

            response = client.models.generate_content(
                model='gemini-3.5-flash',
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )

            data = json.loads(response.text)
            summary_text = data.get("summary", "").strip()
            hashtags_raw = data.get("hashtags", [])

            # 해시태그 2차 가드레일 (불용어 제거 및 의미 있는 명사 검증)
            valid_tags = []
            for tag in hashtags_raw:
                clean_tag = tag.replace('#', '').strip()
                if len(clean_tag) >= 2 and clean_tag not in STOP_WORDS:
                    valid_tags.append(f"#{clean_tag}")

            if summary_text and len(valid_tags) >= 2:
                print(f"      [Gemini 2.5 Flash] 유튜브 개별 요약 성공: '{clean_title[:20]}...'")
                return {
                    "summary": summary_text,
                    "hashtags": valid_tags[:4]
                }
        except Exception as e:
            print(f"      [Gemini 2.5 Flash 유튜브 요약 오류] {e}")
    else:
        print(f"      [정보] GEMINI_API_KEY 미설정으로 스마트 팩트 파서(Fallback) 구동: '{clean_title[:20]}...'")

    # 2단계: 스마트 팩트 파서 (Gemini 미구동 시에도 영상 제목/설명에서 팩트 명사 및 숫자 추출하여 고품질 요약 생성)
    # 제목 및 본문에서 구체적 수치/보험 팩트 패턴 파싱
    numbers = re.findall(r'\d+(?:만|억|원|%|세대|년|회)?', title + " " + description)
    keywords = [w for w in re.findall(r'[가-힣a-zA-Z0-9]+', title + " " + description) 
                if len(w) >= 2 and w not in STOP_WORDS and not w.isdigit()]
    
    # 의미 있는 해시태그 4개선정 (조사성 단어 제외)
    insurance_core_kws = ["수술비", "암보험", "실손보험", "비급여", "표적항암", "독감", "간병비", "유병자", "간편심사", "리모델링", "뇌혈관", "허혈성", "종수술비", "상해", "질병"]
    selected_tags = []
    
    # 1순위: 제목/설명 내 주요 보험 전문 용어
    for kw in keywords:
        if any(ik in kw for ik in insurance_core_kws) and f"#{kw}" not in selected_tags:
            selected_tags.append(f"#{kw}")
            if len(selected_tags) >= 4:
                break
                
    # 2순위: 기타 2자 이상 일반 명사 (불용어 제외)
    if len(selected_tags) < 4:
        for kw in keywords:
            if kw not in STOP_WORDS and f"#{kw}" not in selected_tags:
                selected_tags.append(f"#{kw}")
                if len(selected_tags) >= 4:
                    break

    # 3순위: 기본 태그 보충
    default_tags = ["#보장분석", "#삼성화재", "#실전화법", "#보장점검"]
    for dt in default_tags:
        if len(selected_tags) < 4 and dt not in selected_tags:
            selected_tags.append(dt)

    # 스마트 팩트 문장 조합 (템플릿 문구 전면 제거 및 제목/본문 팩트 결합)
    fact_details = f"({', '.join(numbers[:3])})" if numbers else ""
    desc_fact = description[:100].strip() if description and len(description) > 20 else "고객 상담 시 주요 보장 항목의 수량 및 한도 조건을 체크하기에 유용한 현장 정보입니다."
    
    smart_summary = f"'{clean_title}' 영상은 {channel if channel else '전문가'} 채널에서 {selected_tags[0].replace('#','')} 및 보장 틈새 점검의 팩트 포인트{fact_details}를 조명합니다. {desc_fact}"

    return {
        "summary": smart_summary,
        "hashtags": selected_tags[:4]
    }

def is_photo_news_url(url):
    # 포토 전용 섹션 URL 배제 함수
    url_lower = (url or "").lower()
    photo_patterns = [
        "news1.kr/photos/", "/photos/", "/photo/", "photo.naver.com", 
        "photonews", "/photogallery/", "yonhapnews.co.kr/photos/",
        "isplus.com/photo", "sports.donga.com/photo"
    ]
    return any(p in url_lower for p in photo_patterns)

def validate_theme_domain(cat_id, text):
    # 각 테마별 필수 핵심 도메인 단어 검증 및 금융대출/자극적 픽션 뉴스 철통 배제
    t_lower = (text or "").lower()
    
    # 0. 전 테마 공통 강력 배제어 (주택담보대출, 금리, 자극적 오피니언/픽션)
    loan_and_opinion_blocks = [
        "주택담보대출", "주담대", "사업자 대출", "대출 금리", "대출 규제", "담보 대출", "대출 한도",
        "석유자본", "테러 시작", "2000만 사망", "[칼럼]", "[사설]", "[오피니언]", "[기여]", "[시론]",
        "유상증자", "무상증자", "공시", "상장폐지", "회계감리", "감사의견", "불성실공시"
    ]
    for b in loan_and_opinion_blocks:
        if b in t_lower:
            return False

    # 부고 / 인사 / 동정 기사 원천 차단
    if any(ex in t_lower for ex in ["부친상", "모친상", "장인상", "장모상", "빙부상", "빙모상", "부고", "부의", "별세", "부음", "삼가 고인", "인사동정", "발령"]):
        return False

    if cat_id in ["hospital_cost", "medtech"]: # 질병·치료비 리얼리티
        required = ["보험", "질병", "치료", "진료", "병원", "약", "급여", "환자", "수술", "의료", "암", "비급여", "본인부담", "치료비"]
    elif cat_id == "caregiving": # 간병·돌봄 대란
        required = ["간병비", "간병인", "간병파산", "간병지옥", "요양", "돌봄", "간병"]
    elif cat_id in ["silson", "fss_reform", "reform_insurance"]: # 제도·정책 이슈
        required = ["보험", "금융", "금감원", "건보", "실손", "의료", "제도", "정책", "급여", "가이드라인", "개정"]
    elif cat_id == "product_trend": # 상품·시장 동향 (대출 저당물 '담보' 제외하고 '보험담보' '특약' 등 민간보험 관련만 통과)
        if any(lk in t_lower for lk in ["대출", "주담대", "금리", "이자", "저당"]):
            return False
        required = ["보험", "상품", "특약", "수술비", "손해율", "보장", "절판", "신상품", "한도"]
    else:
        return True
        
    return any(req in t_lower for req in required)

def is_outdated_content(title):
    # 제목 및 언론사를 분석해 과거 정보나 선진국 외 불필요한 해외 뉴스 차단
    title_lower = title.lower()
    
    # 1. 동남아 및 기타 개발도상국 관련 단어 원천 차단 (블랙리스트)
    block_regions = [
        "베트남", "vietnam", "viet", "hanoi", "호치민", "하노이", "인도네시아", "필리핀", 
        "태국", "캄보디아", "라오스", "미얀마", "방글라데시", "몽골", "우즈벡", "네팔",
        "스리랑카", "파키스탄", "인도", "india", "말레이시아", "싱가포르", "싱가폴"
    ]
    for br in block_regions:
        if br in title_lower:
            print(f"      [필터링] 허용되지 않은 국가/지역 정보 차단: {br} ({title})")
            return True
            
    # 2. 선진국(한국, 미국, 유럽, 일본, 중국, 호주 등) 정보만 허용되도록 명확히 필터
    
    # 3. 역사 속으로 사라진 옛 보험사 키워드 차단
    old_companies = [
        "제일화재", "제일화재해상", "그린화재", "그린손해", "lig손해", "lig희망", 
        "현대라이프", "에르고다음", "알리안츠", "알리안츠생명", "카디프생명", "pca생명"
    ]
    for comp in old_companies:
        if comp in title_lower:
            print(f"      [필터링] 폐업/인수된 옛 보험사 정보 차단: {comp} ({title})")
            return True
            
    # 4. 2024년 이전의 과거 연도가 명시적으로 제목에 포함된 경우 차단
    for year in range(2000, 2024):
        if str(year) in title_lower:
            print(f"      [필터링] 과거 연도 정보 차단: {year}년 ({title})")
            return True
            
    return False

RECENT_URLS_FILE = "recent_published_urls.json"

def load_recent_urls():
    # 최근 5일간 노출되었던 기사 URL 및 제목 목록 로드 (URL 완전일치 + 제목 유사도 이중 체크용)
    if not os.path.exists(RECENT_URLS_FILE):
        return set(), []
    try:
        with open(RECENT_URLS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            now = datetime.now()
            valid_urls = set()
            valid_titles = []
            for u, val in data.items():
                try:
                    if isinstance(val, dict):
                        date_str = val.get("date", "")
                        title = val.get("title", "")
                    else:
                        date_str = val
                        title = ""
                    dt = datetime.strptime(date_str, "%Y-%m-%d")
                    if (now - dt).days <= 5:
                        valid_urls.add(u)
                        if title:
                            valid_titles.append(title)
                except Exception:
                    pass
            return valid_urls, valid_titles
    except Exception:
        return set(), []

def save_recent_urls(new_items):
    # 오늘 최종 배포된 기사 URL+제목 저장 및 5일 지나면 자동 정제
    urls_dict = {}
    if os.path.exists(RECENT_URLS_FILE):
        try:
            with open(RECENT_URLS_FILE, "r", encoding="utf-8") as f:
                urls_dict = json.load(f)
        except Exception:
            urls_dict = {}
    today_str = datetime.now().strftime("%Y-%m-%d")
    for item in new_items:
        u = item.get("link") if isinstance(item, dict) else item
        title = item.get("title", "") if isinstance(item, dict) else ""
        if u:
            urls_dict[u] = {"date": today_str, "title": title}
    now = datetime.now()
    cleaned_dict = {}
    for u, val in urls_dict.items():
        try:
            date_str = val.get("date", "") if isinstance(val, dict) else val
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            if (now - dt).days <= 5:
                cleaned_dict[u] = val if isinstance(val, dict) else {"date": val, "title": ""}
        except Exception:
            pass
    try:
        with open(RECENT_URLS_FILE, "w", encoding="utf-8") as f:
            json.dump(cleaned_dict, f, ensure_ascii=False, indent=2)
        print(f"[완료] 최근 노출 기사 URL {len(cleaned_dict)}건 {RECENT_URLS_FILE}에 저장 완료.")
    except Exception as e:
        print(f"[경고] 최근 기사 URL 저장 실패: {e}")

def fetch_category_news(cat_id, info, limit=8):
    # 구글 뉴스 RSS를 사용하여 각 테마별 실시간 뉴스 수집
    recent_published_urls, recent_published_titles = load_recent_urls()
    query = info["query"]
    time_param = "when:2d"
    full_query = f"{query} {time_param}" if not cat_id.startswith("site:") else query
    encoded_query = urllib.parse.quote(full_query)
    url = f"{RSS_BASE_URL}?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
    
    print(f"[정보] '{info['label']}'({cat_id}) 데이터 수집 중...")
    context = ssl._create_unverified_context()
    
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15, context=context) as response:
            xml_data = response.read()
        
        root = ET.fromstring(xml_data)
        items = []
        
        def _pubdate_key(it):
            el = it.find('pubDate')
            try:
                d = parsedate_to_datetime(el.text)
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                return d.timestamp()
            except Exception:
                return 0.0

        _raw_items = root.findall('.//item')
        try:
            _raw_items = sorted(_raw_items, key=_pubdate_key, reverse=True)
        except Exception:
            pass
        _raw_items = _raw_items[:limit * 6]

        for item in _raw_items:
            title_el = item.find('title')
            link_el = item.find('link')
            pub_date_el = item.find('pubDate')
            source_el = item.find('source')
            
            raw_title = title_el.text if title_el is not None else ""
            link = link_el.text if link_el is not None else ""
            pub_date_raw = pub_date_el.text if pub_date_el is not None else ""
            source_raw = source_el.text if source_el is not None else "Google News"
            
            # source 태그의 url 속성에서 실제 기사 원본 URL 추출 (구글뉴스 리다이렉트 우회)
            source_url = source_el.get('url', '') if source_el is not None else ''
            
            parts = raw_title.rsplit(' - ', 1)
            if len(parts) == 2:
                clean_title = parts[0]
                source = parts[1]
            else:
                clean_title = raw_title
                source = source_raw

            # [최상단 강제 필터링] 최근 5일 이내 이미 노출되었던 기사 URL 중복 재노출 차단
            if link in recent_published_urls or source_url in recent_published_urls:
                print(f"      [필터링] 최근 5일 이내 이미 배포된 중복 기사 제외: {clean_title}")
                continue

            # [최상단 강제 필터링] URL이 달라도 제목이 유사하면 이미 배포된 것으로 간주 (구글뉴스 리다이렉트 토큰 변동 대응)
            if any(are_titles_similar(clean_title, rt, threshold=0.5) for rt in recent_published_titles if rt):
                print(f"      [필터링] 최근 5일 이내 유사 제목 기사 제외(URL 상이): {clean_title}")
                continue

            # [최상단 강제 필터링] 포토 전용 섹션 URL 패턴(news1.kr/photos/ 등 사진 캡션 기사) 즉시 차단
            if is_photo_news_url(link) or is_photo_news_url(source_url):
                print(f"      [최상단필터링] 포토 전용 섹션 뉴스 즉시 차단: {clean_title}")
                continue

            # [최상단 강제 필터링] 블로그 껍데기 포스트(티스토리/네이버 글 목록, 카테고리 등) 즉시 차단
            title_chk = (clean_title + " " + raw_title + " " + source + " " + link + " " + source_url).lower()
            if any(b_domain in title_chk for b_domain in ["blog.naver.com", "tistory.com", "velog.io", "티스토리"]):
                if any(j_kw in title_chk for j_kw in ["글 목록", "글목록", "목록", "카테고리", "전체글", "2026/", "2025/"]):
                    print(f"      [최상단필터링] 껍데기 블로그 포스트 즉시 차단: {clean_title}")
                    continue

            
            pub_dt = None
            pub_date_str = ""
            if pub_date_raw:
                try:
                    pub_dt = parsedate_to_datetime(pub_date_raw)
                    pub_date_str = pub_dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pub_date_str = pub_date_raw
            
            # 최근 48시간(2일) 이내 수집
            if pub_dt:
                now_utc = datetime.now(timezone.utc)
                diff = now_utc - pub_dt
                if diff.total_seconds() > 48 * 3600:
                    continue
            
            # 의료개혁/건보(reform_insurance): 지방 지자체/지방 의료원 뉴스 차단하고 서울/수도권/중앙정책 뉴스만 허용
            if cat_id == "reform_insurance":
                local_keywords = ["경남", "전남", "전북", "경북", "강원", "충남", "충북", "제주", "부산", "대구", "광주", "대전", "울산", "창원", "청주", "전주"]
                if any(lk in clean_title or lk in source for lk in local_keywords):
                    print(f"      [필터링] 의료개혁 지방 뉴스 차단: {clean_title}")
                    continue

            title_lower = clean_title.lower()
            source_lower = source.lower()

            # [최상단 강제 필터링] 과거 연도(2023년 이전) 명시, 폐업/인수된 옛 보험사, 동남아 등 지역 뉴스 즉시 차단
            if is_outdated_content(clean_title):
                continue

            # [최상단 강제 필터링] 개인 블로그(네이버블로그/티스토리/벨로그) 게시물은 신뢰도·팩트 검증이 어려워 카테고리 불문 전면 차단
            blog_domains = ["blog.naver.com", "tistory.com", "velog.io"]
            if any(bd in link.lower() for bd in blog_domains) or any(bd in source_url.lower() for bd in blog_domains):
                print(f"      [최상단필터링] 개인 블로그 게시물 전면 차단: {clean_title}")
                continue

            # [최상단 강제 필터링] 지방 소도시명 + 공단 지사/지부 등 지엽적 기관 뉴스는 크롤링 전 단계에서 즉시 차단
            local_cities = ["경주", "포항", "구미", "창원", "전주", "천안", "춘천", "안동", "군산", "목포", "충주", "원주", "제천", "김천", "영주", "상주", "속초", "강릉"]
            branch_suffixes = ["지사", "지부", "본부", "출장소"]
            if any(city in title_lower and any(bs in title_lower for bs in branch_suffixes) for city in local_cities):
                print(f"      [최상단필터링] 지역 공단 지사/지부 지엽적 뉴스 즉시 차단: {clean_title}")
                continue

            # 지엽적인 지역 단체 수상/행사 뉴스 사전 차단 (위더스상, 개소식, 발대식, 표창 등)
            local_event_keywords = ["위더스", "감사패", "표창", "발대식", "개소", "mou", "출범식", "기념식"]
            if any(lk in title_lower for lk in local_event_keywords):
                print(f"      [필터링] 지엽적 행사/수상 뉴스 차단: {clean_title}")
                continue

            # 미용/피부과/성형 개원 뉴스 사전 차단 (보장 대상 제외 무의미 기사 제거)
            if any(kw in title_lower for kw in ["피부과", "성형", "일반의 개원", "미용 시술"]):
                print(f"      [필터링] 미용/피부과 개원 무의미 기사 차단: {clean_title}")
                continue

            # 타사 사회공헌/후원/기부/봉사/나눔 기사 철통 차단 (오직 '삼성화재' 관련 기사만 허용)
            csr_keywords = ["후원", "기부", "봉사", "사회공헌", "나눔", "전달", "장학금", "esg"]
            if any(csr_kw in title_lower for csr_kw in csr_keywords):
                if "삼성화재" not in title_lower and "삼성화재" not in source_lower:
                    print(f"      [필터링] 타사 사회공헌/후원 기사 원천 차단: {clean_title}")
                    continue

            # 지자체/구청 방문진료 등 단체 복지 사업 기사 (민간 보험 세일즈 무관) 차단
            local_welfare_keywords = ["방문진료", "구청", "주민센터", "동사무소", "보건소 지원", "복지관", "무료 진료", "돌봄 바우처", "동대문구", "성북구", "강북구"]
            if any(kw in title_lower for kw in local_welfare_keywords):
                if not any(ik in title_lower for ik in ["실손", "수술비", "암보험", "비급여", "담보", "약관", "특약", "손해율", "리모델링"]):
                    print(f"      [필터링] 지자체/구청 방문진료 복지 뉴스 차단: {clean_title}")
                    continue

            # 링크(URL) 및 출처(Source) 기반 베트남/동남아 및 지역케이블/SK브로드밴드 차단 필터
            link_lower = link.lower()
            block_urls = [".vn", "vietnam", "viet", "hanoi", "saigon", "indonesia", "thailand", "philippines", "skbroadband", "btvnews", "chbtv"]
            is_foreign_source = False
            for bu in block_urls:
                if bu in link_lower or bu in source_lower:
                    is_foreign_source = True
                    break
            if is_foreign_source:
                print(f"      [필터링] URL/출처 기반 동남아/지역케이블 뉴스 차단: {source} ({link})")
                continue
                
            # 기사 본문 크롤링 - Google RSS 리다이렉트 해원 후 og:description / 네이버 모바일 블로그 파서 구동
            if cat_id not in ["assembly_petition", "youtube"]:
                _pre = evaluate_article_cot(clean_title, "", hook=False)
                if not _pre["adopted"] and len(_pre["checklist_hits"]) == 0:
                    print(f"      [사전탈락] 제목 단계 영업관련성 0 → 크롤링 생략: {clean_title[:30]}")
                    continue

            print(f"        [본문크롤링] {clean_title[:30]}...")
            real_url = resolve_google_news_url(link) or link

            if is_photo_news_url(real_url):
                print(f"      [필터링] 리다이렉트 포토 전용 섹션 뉴스 차단: {clean_title}")
                continue

            article_body = fetch_article_body(real_url)
            
            # 크롤링 실패 시 RSS description으로 폴백
            if not article_body:
                desc_el = item.find('description')
                raw_desc = desc_el.text if desc_el is not None else ""
                article_body = re.sub(r'<[^>]*>', '', raw_desc).strip()
                article_body = article_body.split('&nbsp;')[0].strip()
            
            # 2차 도메인 관련도 필터링: 해당 테마 필수 키워드가 제목+본문에 1개 이상 없으면 제외 (주담대/금리 대출뉴스 차단)
            if not validate_theme_domain(cat_id, clean_title + " " + (article_body or "")):
                print(f"      [2차 도메인 검증 탈락] 테마 도메인 무관 기사 차단: {clean_title}")
                continue
            
            # 블로그(네이버블로그, 티스토리 등) 껍데기 글 목록 및 수집 미흡 포스트 100% 자동 제외 (Drop)
            is_blog = any(b_domain in link.lower() for b_domain in ["blog.naver.com", "tistory.com", "velog.io"])
            junk_blog_keywords = ["글 목록", "목록", "카테고리", "전체글", "글목록", "티스토리", "tistory"]
            
            # 1. 블로그 제목이 단순 '글 목록' 이거나 껍데기 키워드 포함 시 필터링
            if is_blog and any(j_kw in clean_title.lower() for j_kw in junk_blog_keywords):
                print(f"      [필터링] 껍데기 블로그 목록/카테고리 포스트 제외: {clean_title}")
                continue

            # 2. 블로그 본문 수집 실패/내용 미흡 시 필터링
            if is_blog and (not article_body or len(article_body) < 20 or article_body.strip() == clean_title.strip()):
                print(f"      [필터링] 내용 부실 블로그 포스트 자동 제외: {clean_title}")
                continue
                
            if not article_body or len(article_body) < 15 or article_body == clean_title:
                # 본문 부실 시 제목만으로 CoT를 평가하도록 빈 문자열 유지
                # (주의: 여기에 "보장"/"보험" 등 실제 CoT 판별 키워드와 겹치는 단어를 채워 넣으면
                #  본문 크롤링 실패 기사가 가짜로 맥락 검증을 통과하는 허점이 생긴다 — 실제 겪은 버그)
                article_body = ""
            
            # 6단계 Chain of Thought (CoT) 영업 관련성 및 인사이트 평가
            cot_eval = evaluate_article_cot(clean_title, article_body)
            
            # 일반 뉴스 카테고리의 경우 CoT 영업 관련성 검증 미달(adopted==False) 시 즉시 제외
            if cat_id not in ["assembly_petition", "youtube"] and not cot_eval["adopted"]:
                print(f"      [CoT 탈락] 영업 관련성 미달 기사 제외: {clean_title}")
                continue

            sales_insight = cot_eval["sales_hook"] if cot_eval["sales_hook"] else article_body
            cat_label = cot_eval["category"] if cot_eval.get("category") else info["label"]

            # CoT에서 최종 판정된 카테고리에 맞춰 뱃지 색상 및 속성 동적 동기화
            badge_color = info["badge_color"]
            badge_bg = info["badge_bg"]
            target_cat_id = cat_id
            
            if cat_label == "제도·정책 이슈":
                badge_color = "#3B82F6"
                badge_bg = "rgba(59, 130, 246, 0.1)"
                target_cat_id = "silson"
            elif cat_label == "질병·치료비 리얼리티":
                badge_color = "#10B981"
                badge_bg = "rgba(16, 185, 129, 0.1)"
                target_cat_id = "hospital_cost"
            elif cat_label == "간병·돌봄 대란":
                badge_color = "#F59E0B"
                badge_bg = "rgba(245, 158, 11, 0.1)"
                target_cat_id = "caregiving"
            elif cat_label == "상품·시장 동향":
                badge_color = "#06B6D4"
                badge_bg = "rgba(6, 182, 212, 0.1)"
                target_cat_id = "product_trend"
            elif cat_label == "성공·동기부여":
                badge_color = "#C026D3"
                badge_bg = "rgba(192, 38, 211, 0.1)"
                target_cat_id = "motivation"

            # [카테고리 내 동일 기사 실시간 중복 차단] 이미 수집된 기사 중 제목이 대단히 유사한 기사가 있으면 원천 차단
            is_dup = False
            for existing in items:
                if are_titles_similar(clean_title, existing["title"], threshold=0.30):
                    print(f"      [중복제거] 동일 주제/사건 중복 기사 제거: '{clean_title}' vs '{existing['title']}'")
                    is_dup = True
                    break
            if is_dup:
                continue

            items.append({
                "title": clean_title.strip(),
                "link": link.strip(),
                "pub_date_str": pub_date_str,
                "datetime": pub_dt,
                "source": source.strip(),
                "summary": article_body,  # 100% 팩트 기반 실제 기사 본문 직접 크롤링 (할루시네이션 원천 차단)
                "insight": sales_insight,
                "category_id": target_cat_id,
                "category_label": cat_label,
                "badge_color": badge_color,
                "badge_bg": badge_bg,
                "is_promo": cot_eval.get("is_promo", False)
            })

            if len(items) >= limit:
                break
            
        items.sort(key=lambda x: x["datetime"] if x["datetime"] else datetime.min, reverse=True)
        return items[:limit]
    except Exception as e:
        print(f"[오류] '{info['label']}' 수집 실패: {e}")
        return []


def calculate_sales_relevance_score(item):
    # 영업 현장 활용가치를 자동 계산하는 스코어링 함수
    title = item.get("title", "")
    body = item.get("summary", "") + " " + item.get("insight", "")
    text = title + " " + body
    
    score = 0
    score_details = []
    
    # 1. 홍보성 기사 기본 감점
    if item.get("is_promo"):
        score -= 3
        score_details.append("홍보기사(-3)")
        
    # 2. 특정 보험사/금융사 브랜드명 제목 명시 감점 (-3점)
    brand_names = [
        "kb", "kb손보", "kb손해보험", "한화", "한화생명", "한화손보", "교보", "교보생명", 
        "db", "db손보", "db손해보험", "현대해상", "메리츠", "메리츠화재", "흥국", "흥국화재", 
        "토스", "토스증권", "카카오페이", "동양생명", "신한라이프", "라이나", "aia"
    ]
    title_lower = title.lower()
    found_brand = None
    for bn in brand_names:
        if bn in title_lower:
            found_brand = bn
            break
    if found_brand:
        score -= 3
        score_details.append(f"브랜드명노출({found_brand})(-3)")

    # 3. 추상적/일반적 제목 감점 (-1점)
    abstract_patterns = ["알아보니", "이것만은", "무엇인가", "어떻게 될까", "관심 집중", "눈길", "주목"]
    if any(ap in title_lower for ap in abstract_patterns):
        score -= 1
        score_details.append("추상적제목(-1)")

    # 4. 구체적 수치(금액, %, 건수, 인원) 가점 (+2점)
    if re.search(r'\d+(?:만|억|원|%|건|명|회|세대)', text):
        score += 2
        score_details.append("구체적수치(+2)")

    # 4-1. 고액/급증 수치 크기 가점 세분화 (설득력이 큰 고액 단위·급증 표현일수록 추가 가산, 최대 +3점)
    big_number_patterns = [r'억', r'천만', r'백만', r'\d{2,}%', r'\d+배', r'급증', r'폭등', r'최대']
    big_hits = sum(1 for p in big_number_patterns if re.search(p, text))
    if big_hits > 0:
        bonus = min(big_hits, 3)
        score += bonus
        score_details.append(f"고액/급증수치({big_hits}건 감지)(+{bonus})")

    # 5. 영업 임팩트 키워드 가점 (+2점)
    impact_keywords = [
        "급증", "부담 증가", "제도 변경", "급여 기준", "환자 수", "폭염", "온열질환",
        "사망", "상속세", "증여세", "절세", "수술비", "비급여", "한도 축소", "절판"
    ]
    found_impact = [ik for ik in impact_keywords if ik in text]
    if found_impact:
        score += 2
        score_details.append(f"임팩트키워드({found_impact[0]})(+2)")

    # 6. 긴박성/골든타임 가점 (+3점) - "지금이 마지막 기회" 서사를 만드는 제도 변화·마감 시한 신호
    urgency_keywords = [
        "시행일 확정", "유예기간 종료", "단계적 폐지", "입법예고", "내년 시행", "개정 예고",
        "경과조치", "절판 임박", "한도 축소", "인수 강화", "손해율 악화", "판매 중단", "마지막 기회"
    ]
    found_urgency = [uk for uk in urgency_keywords if uk in text]
    if found_urgency:
        score += 3
        score_details.append(f"긴박성/골든타임({found_urgency[0]})(+3)")

    # 7. 영업 무관/노이즈 감점 강화 (-5점) - 지역 공단 지사, 단순 협약/MOU, 지자체 무료 돌봄 기사
    text_lower = text.lower()
    noise_keywords = ["지사", "지부", "지역본부", "출장소", "mou", "협약", "지자체", "무료 돌봄", "돌봄 바우처", "무료 진료"]
    found_noise = [nk for nk in noise_keywords if nk in text_lower]
    if found_noise:
        score -= 5
        score_details.append(f"영업무관/노이즈({found_noise[0]})(-5)")

    item["sales_score"] = score
    item["score_details"] = ", ".join(score_details) if score_details else "기본(0)"
    return score


def is_within_14_days(published_text):
    if not published_text:
        return False
    text = published_text.strip().lower()
    recent_kws = ["방금", "실시간", "초 전", "분 전", "시간 전", "live", "now", "second", "seconds", "minute", "minutes", "hour", "hours"]
    if any(kw in text for kw in recent_kws):
        return True
    match = re.search(r'(\d+)\s*(일\s*전|주\s*전|주일\s*전|day|days|week|weeks)', text)
    if not match:
        return False
    val = int(match.group(1))
    unit = match.group(2)
    if "일" in unit or "day" in unit:
        return val <= 30
    elif "주" in unit or "week" in unit:
        return val <= 4
    return False

def parse_view_count(view_text):
    if not view_text:
        return 0
    text = view_text.replace(",", "").strip()
    if "없음" in text or "no views" in text.lower():
        return 0
    match = re.search(r'([\d\.]+)\s*(만|천|k|m|회|views)?', text, re.IGNORECASE)
    if not match:
        return 0
    val_str = match.group(1)
    unit = match.group(2)
    try:
        val = float(val_str)
    except ValueError:
        return 0
    if not unit:
        return int(val)
    unit = unit.lower()
    if unit == '만':
        return int(val * 10000)
    elif unit in ['천', 'k']:
        return int(val * 1000)
    elif unit == 'm':
        return int(val * 1000000)
    else:
        return int(val)

def fetch_youtube_trends(limit=10, recent_published_urls=None):
    recent_published_urls = recent_published_urls or set()
    queries = [
        "비통치 비특치 암통원치료비",
        "비급여통원치료비 암",
        "비급여특수치료비",
        "수술비 보험 추천",
        "비특치 비통치 보험",
        "무사고 연장 보험",
        "간편심사 3N5 보험",
        "보험 리모델링 필수"
    ]
    all_videos = []
    seen_ids = set()
    context = ssl._create_unverified_context()
    exclude_channels = ["뉴스", "속보", "KBS", "MBC", "SBS", "YTN", "연합뉴스", "뉴스1", "뉴시스", "JTBC", "채널A", "MBN", "국회", "정부", "시사", "TV조선"]
    exclude_titles = ["인사", "동정", "임명", "인사발령", "속보", "보도", "토론회"]
    insurance_kws = ["보험", "실비", "실손", "수술비", "암", "보상", "보험금", "치료비", "간편심사", "유병자", "청구", "설계사", "종수술비", "mdrt"]

    for q in queries:
        try:
            encoded_query = urllib.parse.quote(q)
            url = f"https://www.youtube.com/results?search_query={encoded_query}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=8, context=context) as response:
                html = response.read().decode('utf-8')
                match = re.search(r'var ytInitialData = ({.*?});</script>', html)
                if not match:
                    continue
                data = json.loads(match.group(1))
                contents = data.get("contents", {}).get("twoColumnSearchResultsRenderer", {}).get("primaryContents", {}).get("sectionListRenderer", {}).get("contents", [])
                
                for section in contents:
                    items = section.get("itemSectionRenderer", {}).get("contents", [])
                    for item in items:
                        v = item.get("videoRenderer")
                        if not v:
                            continue
                        video_id = v.get("videoId")
                        if not video_id or video_id in seen_ids:
                            continue
                        if f"https://www.youtube.com/watch?v={video_id}" in recent_published_urls:
                            continue
                        title = "".join([r.get("text", "") for r in v.get("title", {}).get("runs", [])])
                        channel_name = "".join([r.get("text", "") for r in v.get("ownerText", {}).get("runs", [])])
                        published_text = v.get("publishedTimeText", {}).get("simpleText", "")
                        view_count_text = v.get("viewCountText", {}).get("simpleText", "")
                        desc_runs = v.get("detailedMetadataSnippets", [{}])[0].get("snippetText", {}).get("runs", [])
                        description = "".join([r.get("text", "") for r in desc_runs])
                        
                        channel_lower = channel_name.lower()
                        title_lower = title.lower()
                        if any(ec in channel_lower for ec in exclude_channels) or any(et in title_lower for et in exclude_titles):
                            continue
                        if not any(kw in title_lower or kw in channel_lower for kw in insurance_kws):
                            continue
                        if not is_within_14_days(published_text):
                            continue
                        views = parse_view_count(view_count_text)
                        if views < 1000:
                            continue
                        analysis = analyze_youtube_video(title, description, channel_name)
                        seen_ids.add(video_id)
                        all_videos.append({
                            "title": title.strip(),
                            "link": f"https://www.youtube.com/watch?v={video_id}",
                            "pub_date_str": published_text.strip(),
                            "views": views,
                            "view_count_str": f"조회수 {views:,}회",
                            "source": channel_name.strip(),
                            "description": description.strip(),
                            "analysis": analysis,
                            "category_id": "youtube",
                            "category_label": "유튜브 핫이슈",
                            "badge_color": "#EF4444",
                            "badge_bg": "rgba(239, 68, 68, 0.1)"
                        })
        except Exception:
            pass
    all_videos.sort(key=lambda x: x["views"], reverse=True)
    return all_videos


def fetch_assembly_petitions():
    # 국회청원 수집 함수
    print("[정보] '국회청원 (비급여/급여화)' 5만 명 달성 보장 패스트트랙 및 CoT 수집 시작...")
    api_url = "https://petitions.assembly.go.kr/api/petits?sttusCode=AGRE_PROGRS,CMIT_FRWRD,PETIT_FORMATN&proceedAt=proceed&pageUnit=100&recordCountPerPage=100"
    API_KEY = "ee6e9f94cfac475babc7de8a14902391"
    petitions = []
    
    group_a = ["암", "항암", "희귀", "난치", "중증", "고액", "신의료기술", "중입자", "면역항암제", "표적항암제", "유전자치료", "줄기세포", "부종", "장애"]
    group_b = ["급여", "급여화", "건강보험 적용", "급여 적용", "급여 확대", "산정특례", "비급여", "본인부담 상한제", "의약품 등재"]
    exclude_keywords = ["경증", "성형", "피부과", "치과", "한방", "의료사고", "의료진 처우", "병원 운영", "약국 운영"]

    def validate_item(item):
        text = (item.get('petitSj', '') + ' ' + (item.get('petitObjet', '') or '') + ' ' + (item.get('petitCn', '') or ''))
        has_a = any(k in text for k in group_a)
        has_b = any(k in text for k in group_b)
        if not (has_a and has_b):
            return False
        sj = item.get('petitSj', '')
        obj = item.get('petitObjet', '') or ''
        if any(ek in sj or ek in obj for ek in exclude_keywords):
            return False
        return True

    try:
        # 1. petitions.assembly.go.kr 내부 API 호출
        req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
        res = urllib.request.urlopen(req, timeout=10).read().decode('utf-8')
        items = json.loads(res)
        
        # 1단계: 1차 분야 필터 - petitRealmCode == "HMCCS" (보건의료)
        hmccs_items = [item for item in items if item.get('petitRealmCode') == 'HMCCS' or item.get('petitRealmNm') == '보건의료']

        # 1.5단계: 마감된 과거 청원 배제 - 동의 마감일이 오늘 이전인 항목 제외
        _today_str = datetime.now().strftime('%Y-%m-%d')

        def is_closed(item):
            end_str = (item.get('agreEndDe') or '')[:10]
            return bool(end_str) and end_str < _today_str

        hmccs_items = [item for item in hmccs_items if not is_closed(item)]

        # 2단계: 5만 명 달성 보장 패스트트랙 분류
        fast_track_candidates = []
        general_candidates = []
        today = datetime.now()

        for item in hmccs_items:
            agre_co = int(item.get('agreCo') or 0)
            st_code = item.get('sttusCode', '')
            if agre_co >= 50000 or st_code == 'CMIT_FRWRD':
                fast_track_candidates.append(item)
            else:
                dt_str = item.get('agreBeginDe') or item.get('petitRegistDt') or ''
                if dt_str:
                    try:
                        dt = datetime.strptime(dt_str[:10], '%Y-%m-%d')
                        if (today - dt).days <= 45:
                            general_candidates.append(item)
                    except Exception:
                        general_candidates.append(item)
                else:
                    general_candidates.append(item)

        # 3, 4, 5단계: A/B 교차검증 및 예외 조건 필터링
        valid_fast_track = [item for item in fast_track_candidates if validate_item(item)]
        valid_general = [item for item in general_candidates if validate_item(item)]

        # 동의수 내림차순 정렬
        valid_fast_track.sort(key=lambda x: int(x.get('agreCo') or 0), reverse=True)
        valid_general.sort(key=lambda x: int(x.get('agreCo') or 0), reverse=True)

        final_selected = valid_fast_track[:2] + valid_general[:2]

        # 6단계: 정렬 및 딥링크 조립
        for item in final_selected[:3]:
            st_code = item.get('sttusCode', '')
            agre_co = int(item.get('agreCo') or 0)
            is_50k = (agre_co >= 50000 or st_code == 'CMIT_FRWRD')
            
            title_prefix = "[5만달성] " if is_50k else ""
            title = title_prefix + item.get('petitSj', '')

            if item.get('link_override'):
                link = item['link_override']
                source_label = "국민동의청원 원문 (5만명 달성 회부)"
            else:
                path = 'onGoingAll' if st_code == 'AGRE_PROGRS' else ('cmtReferred' if st_code == 'CMIT_FRWRD' else 'registered')
                petit_id = item.get('petitId', '')
                link = f"https://petitions.assembly.go.kr/proceed/{path}/{petit_id}"
                source_label = "국민동의청원 원문 (5만명 달성 회부)" if is_50k else "국민동의청원 원문 (동의 진행 중)"
            
            realm = item.get('petitRealmNm', '보건의료')
            end_de = (item.get('agreEndDe') or '')[:10]
            obj_text = item.get('petitObjet', '') or item.get('petitCn', '') or ''
            
            petitions.append({
                "title": title,
                "link": link,
                "pub_date_str": f"동의수: {agre_co:,}명 (마감: {end_de})",
                "source": source_label,
                "category_id": "assembly_petition",
                "category_label": "국회청원",
                "badge_color": "#EC4899",
                "badge_bg": "rgba(236, 72, 153, 0.1)",
                "disease_tag": f"국회청원 ({'5만달성' if is_50k else '중증급여화'})",
                "situation": obj_text[:120] + ("..." if len(obj_text) > 120 else "")
            })

    except Exception as e:
        print(f"[오류] petitions.assembly.go.kr API 수집 실패: {e}")

    if not petitions:
        print("[정보] 이번 기간 조건에 맞는 중증·희귀질환 건강보험 적용 관련 청원 없음.")

    return petitions


def fetch_threads_hot_issues():
    # 스레드 핫이슈 수집 함수
    print("[정보] '스레드(Threads) 핫이슈' 실시간 데이터 수집 중...")
    try:
        import fetch_threads
        posts = fetch_threads.fetch_real_threads_rss()
        items = []
        for p in posts:
            items.append({
                "title": p["title"],
                "link": p["url"],
                "pub_date_str": "최근 24시간 내",
                "source": p["tag"],
                "category_id": "threads_trend",
                "category_label": "스레드 핫이슈",
                "badge_color": "#000000",
                "badge_bg": "rgba(0,0,0,0.08)",
                "insight": p["selling_point"]
            })
        return items
    except Exception as e:
        print(f"    [스레드 수집 예외] {e}")
        return []

def calculate_importance_score(item):
    # 영업 실질 도움 중요도 점수화 함수
    score = 0
    title = item.get("title", "").lower()
    cat_id = item.get("category_id", "")
    source = item.get("source", "").lower()
    
    # 1. 카테고리별 기본 가중치 (실손, 병원비실태, 암/신약/치료비 리얼리티 최우선 1순위)
    cat_weights = {
        "medtech": 35,          # 암, 신약, 급여제한, 전이 킬러 기사 최우선
        "hospital_cost": 35,    # 고액 병원비, 수술비, 비급여 실태
        "silson": 35,           # 실손 개정, 금감원, 건보 정책
        "caregiving": 30,
        "ai_semiconductor": 25, # 보험 블로그 & 담보 비교
        "youtube": 20,           # 유튜브 실전 영업 화법
        "product_trend": 20,
        "reform_insurance": 10,
        "motivation": 5
    }
    score += cat_weights.get(cat_id, 0)
    
    # 2. 제목의 핵심 키워드 매칭 가중치 (세일즈 현장 반응이 가장 뜨거운 킬러 키워드에 최고 가산점 부여)
    keywords_weights = {
        "실손": 20, "실비": 20,
        "암": 15, "대장암": 20, "폐암": 20, "유방암": 20,
        "전이": 25, "급여 제한": 25, "쓸 약": 25, "유병자": 20,
        "표적": 15, "항암": 15, "중입자": 15, "치료비": 15,
        "수술비": 15, "수술": 10, "입원 난민": 25, "양극화": 20,
        "간병": 15, "요양": 10, "간병인": 15,
        "비교": 15, "분석": 10,
        "삼성화재": 10,
        "리모델링": 10, "고지의무": 10
    }
    for kw, val in keywords_weights.items():
        if kw in title:
            score += val
            
    # 3. 유튜브인 경우 조회수에 따른 소폭 보너스 (조회수가 보증된 대중적 콘텐츠 가치)
    if cat_id == "youtube":
        views = item.get("views", 0)
        score += min(15, views // 3000)
        
    # 4. 공신력 있는 언론사 우선 노출 로직 (메이저 경제지, 종합지, 지상파 등 가산점 부여)
    reputable_media = [
        "조선일보", "중앙일보", "동아일보", "매일경제", "한국경제", "서울경제", 
        "머니투데이", "파이낸셜뉴스", "헤럴드경제", "아시아경제", "데일리안", 
        "연합뉴스", "뉴시스", "뉴스1", "sbs biz", "kbs", "mbc", "sbs", "jtbc", 
        "한겨레", "경향신문", "국민일보", "문화일보", "한국일보", "경향"
    ]
    is_reputable = False
    for rep in reputable_media:
        if rep in source:
            is_reputable = True
            break
            
    if is_reputable:
        score += 30  # 공신력 있는 언론사에 보너스 점수 가산
        
    return score

def compile_briefing_data():
    # 뉴스 및 동향 데이터 통합 수집 함수
    flat_items = []
    seen_links = set()
    seen_titles = set()
    recent_published_urls, _ = load_recent_urls()
    
    # 1. 뉴스 카테고리별 기사 수집 (한도 10개까지 넉넉히 수집)
    import concurrent.futures
    _targets = [(cid, cinfo) for cid, cinfo in CATEGORIES.items() if cid != "youtube"]
    _results = {}
    print(f"[정보] 테마 {len(_targets)}개 병렬 수집 시작...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as _ex:
        _futs = {_ex.submit(fetch_category_news, cid, cinfo, 10): cid for cid, cinfo in _targets}
        for _f in concurrent.futures.as_completed(_futs, timeout=900):
            _cid = _futs[_f]
            try:
                _results[_cid] = _f.result()
            except Exception as e:
                print(f"[오류] '{_cid}' 수집 실패: {e}")
                _results[_cid] = []

    for cat_id, info in _targets:
        raw_items = _results.get(cat_id, [])
        for item in raw_items:
            norm_title = "".join(item["title"].split()).lower()
            if item["link"] in seen_links or norm_title in seen_titles:
                continue
                
            # 글로벌 유사도 중복 검사 (전체 카테고리를 통틀어 유사 기사 중복 배제)
            is_duplicate = False
            for existing_item in flat_items:
                if are_titles_similar(item["title"], existing_item["title"], threshold=0.30):
                    is_duplicate = True
                    break
                    
            if is_duplicate:
                continue
                
            seen_links.add(item["link"])
            seen_titles.add(norm_title)
            flat_items.append(item)
            
    # 2. 유튜브 수집 및 독립 보관 (비통치/비특치 영상 최우선 1번 슬롯 강제 포함)
    youtube_items = fetch_youtube_trends(limit=20, recent_published_urls=recent_published_urls)
    top_youtube = []
    
    # 1순위: 비통치, 비특치, 비급여 치료비 관련 영상 먼저 찾아서 1번 슬롯 고정
    for item in youtube_items:
        t_low = item["title"].lower()
        if "비통치" in t_low or "비특치" in t_low or "비급여" in t_low:
            top_youtube.append(item)
            try:
                print(f"      [유튜브 고정] 비통치/비특치 핵심 영상 1순위 고정: {item['title']}")
            except Exception:
                pass
            break
            
    # 2순위: 나머지 실전 영업 영상으로 3개 꽉 채움
    for item in youtube_items:
        if any(item["link"] == existing["link"] for existing in top_youtube):
            continue
        is_duplicate = False
        for existing_item in top_youtube:
            if are_titles_similar(item["title"], existing_item["title"], threshold=0.35):
                is_duplicate = True
                break
        if not is_duplicate:
            top_youtube.append(item)
            if len(top_youtube) >= 3:
                break
                
    # 3. 각 뉴스 기사별 영업 유용성 스코어(sales_score) 계산
    for item in flat_items:
        calculate_sales_relevance_score(item)
    
    # 4. 테마별 그룹화 및 영업 유용성 스코어 기반 최상위 기사 선택 (promo cap=1 엄격 적용)
    raw_cat_groups = {cat_id: [] for cat_id in CATEGORIES.keys()}
    for item in flat_items:
        title_lower = item["title"].lower()
        # 타사 후원/기부/영화제 기사 최종 배제
        if any(kw in title_lower for kw in ["후원", "영화제", "기부", "봉사", "장학금"]):
            if "삼성화재" not in title_lower and "삼성화재" not in item.get("source", "").lower():
                continue
        raw_cat_groups[item["category_id"]].append(item)

    all_data = {cat_id: [] for cat_id in CATEGORIES.keys()}
    all_data["youtube"] = top_youtube  # 유튜브 전용 독립 보장 노출 (실전 영상 최대 3건)
    all_data["threads_trend"] = fetch_threads_hot_issues()  # 스레드 핫이슈 수집 데이터 바인딩
    all_data["assembly_petition"] = fetch_assembly_petitions()[:2]  # 국회청원(근거 자료 슬롯) 최대 2건 제한

    # 테마별 노출 쿼터: 핵심 메인(간병/치료비/제도-긴박성)은 3~4건, 서브 근거는 1~2건으로 차등 배분
    SLOT_QUOTAS = {
        "caregiving": 4,       # 핵심: 간병·돌봄 대란
        "hospital_cost": 4,    # 핵심: 수술·치료비 리얼리티
        "silson": 4,           # 핵심: 제도·긴박성
        "fss_reform": 4,       # 핵심: 제도·긴박성
        "motivation": 2,       # 서브 근거: 동기부여
    }

    for cat_id, cat_items in raw_cat_groups.items():
        if cat_id in ["youtube", "threads_trend", "assembly_petition"]:
            continue

        # 영업 유용성 스코어(sales_score) 내림차순 정렬 (동점 시 최신순)
        cat_items.sort(key=lambda x: (x.get("sales_score", 0), x.get("datetime") or datetime.min), reverse=True)

        pure_facts = [it for it in cat_items if not it.get("is_promo") and it.get("sales_score", 0) > 0]
        promo_items = [it for it in cat_items if it.get("is_promo")]

        slot = SLOT_QUOTAS.get(cat_id, 3)
        selected = pure_facts[:slot]
        if promo_items:
            print(f"      [{cat_id}] 순수 팩트 기사 {len(pure_facts)}건 확보(쿼터 {slot}건) -> 타사 홍보 기사 {len(promo_items)}건 전면 제외")
        else:
            print(f"      [{cat_id}] 순수 팩트 {len(pure_facts)}건 확보(쿼터 {slot}건)")

        all_data[cat_id] = selected

    return all_data

def build_mail_text(data, today_str, notion_url=None):
    # 메일 발송용 텍스트 브리핑 템플릿 가공
    insight = generate_daily_insight(data)
    lines = []
    lines.append("============================================================")
    lines.append(f"[{BRIEFING_TITLE}] {today_str}")
    lines.append("============================================================")
    
    # 깃허브 모바일 웹 카드뉴스 링크 고정 노출
    lines.append("☞ 모바일 웹 카드뉴스 리포트: https://exodusy5351-lgtm.github.io/morning_briefing/")
    lines.append("============================================================")
    
    if notion_url:
        lines.append(f"☞ 노션 백업 데이터베이스: {notion_url}")
        lines.append("============================================================")
        
    lines.append(insight)
    lines.append("============================================================")
    lines.append("\n오늘 아침 수집된 8대 실전 테마별 팩트 요약 리포트입니다.\n")

    for cat_id, info in CATEGORIES.items():
        items = data.get(cat_id, [])

        # 상품동향 및 성공/미담 타이틀 커스텀 슬로건 매핑
        if cat_id == "product_trend":
            lines.append(f"■ {info['label']} (★신설: 타사 신상품/절판 정보 및 삼성화재 상품과의 실전 비교 우위 분석)")
        elif cat_id == "motivation":
            lines.append(f"■ {info['label']} (★ 오늘의 영업 다짐: \"우리가 전달하는 보장은 고객의 내일을 지키는 가장 가치 있고 숭고한 약속입니다.\")")
        else:
            lines.append(f"■ {info['label']}")
            
        if not items:
            lines.append("  (오늘자 최신 동향이 발견되지 않았습니다.)")
        else:
            for idx, item in enumerate(items, 1):
                lines.append(f"  {idx}. {item['title']}")
                if cat_id == "youtube":
                    lines.append(f"     - 채널: {item['source']} | {item.get('view_count_str', '')} | {item['pub_date_str']}")
                else:
                    lines.append(f"     - 출처: {item['source']} | {item['pub_date_str']}")
                lines.append(f"     - 바로가기: {item['link']}")
                
                # 성공/미담 카테고리에 동기부여 코멘트 삽입
                if cat_id == "motivation":
                    lines.append("     - [활동 동기부여] 고객의 삶을 든든하게 지킨 설계사님의 성공적인 동행 사례입니다. 오늘도 자랑스러운 전업 설계사의 자부심을 안고 현장으로 힘차게 나아갑시다!")
        lines.append("")
        
    lines.append("------------------------------------------------------------")
    lines.append("본 브리핑은 매일 아침 구글 뉴스 피드 및 유튜브 데이터를 기반으로 자동 생성됩니다.")
    lines.append("------------------------------------------------------------")
    
    return "\n".join(lines)

def build_html_card_news(data, today_str, mail_text, notion_url=None):
    # HTML 카드뉴스 빌드 함수
    card_elements = []
    
    for cat_id, info in CATEGORIES.items():
        items = data.get(cat_id, [])
        for item in items:
            is_yt = cat_id == "youtube"
            yt_class = "youtube-card" if is_yt else ""
            
            badge_style = f"background-color: {item['badge_bg']}; color: {item['badge_color']};"
            source_style = "color: var(--yt-color);" if is_yt else ""
            
            if is_yt:
                date_section = f'<span class="card-date"><span style="font-weight: 800; color: var(--yt-color); margin-right: 5px;">{item.get("view_count_str", "")}</span>({item["pub_date_str"]})</span>'
                source_tag_html = f"<span class='source-tag' style='{source_style}'>{item['source']}</span>"
            else:
                date_section = f'<span class="card-date">{item["pub_date_str"]}</span>'
                source_tag_html = f"<span class='source-tag'>{item['source']}</span>"
                
            if cat_id == "motivation":
                icon_svg = '<svg class="card-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"></path></svg>'
            elif cat_id == "product_trend":
                icon_svg = '<svg class="card-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"></path></svg>'
            elif is_yt:
                icon_svg = '<svg class="card-icon" viewBox="0 0 24 24" fill="currentColor"><path d="M23.498 6.163a3.003 3.003 0 0 0-2.11-2.11C19.518 3.545 12 3.545 12 3.545s-7.518 0-9.388.508a3.003 3.003 0 0 0-2.11 2.11C0 8.033 0 12 0 12s0 3.967.502 5.837a3.003 3.003 0 0 0 2.11 2.11c1.87.508 9.388.508 9.388.508s7.518 0 9.388-.508a3.003 3.003 0 0 0 2.11-2.11C24 15.967 24 12 24 12s0-3.967-.502-5.837zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"/></svg>'
            else:
                icon_svg = '<svg class="card-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path></svg>'
                
            # 상품 동향 카드의 비교우위 정보 삽입
            comp_fact_html = ""
                
            # 유튜브 상세 카드 뉴스 블록 (개별 영상 고유 요약 및 키워드)
            yt_analysis_html = ""
            if is_yt and item.get('analysis'):
                ans = item['analysis']
                video_summary_text = ans.get('summary', '').strip()
                video_keywords = ans.get('hashtags', [])
                if video_summary_text:
                    kw_html = " ".join([f"<span class='hook-badge' style='background-color: #fee2e2; color: #dc2626; font-size: 11px; margin-right: 4px;'>{tag}</span>" for tag in video_keywords])
                    yt_analysis_html = f'<div class="sales-hook-card" style="background-color: #fef2f2; border-left: 4px solid #ef4444;"><div style="margin-bottom: 6px;">{kw_html}</div><p class="hook-text" style="color: #991b1b;">{video_summary_text}</p></div>'
                
            promo_badge_html = ""
            hook_badge_title = "💡 현장 화법 포인트"
            card_bg_style = ""
            if item.get("is_promo"):
                promo_badge_html = "<span class='badge' style='background-color: rgba(217, 119, 6, 0.15); color: #d97706; border: 1px solid rgba(217, 119, 6, 0.3); margin-left: 6px;'>타사 상품 동향 참고</span>"
                hook_badge_title = "💡 경쟁사 상품 참고 메모"
                card_bg_style = "background-color: #fffbeb; border-left: 4px solid #d97706;"

            news_analysis_html = ""
            threads_analysis_html = ""
            sales_hook_text = item.get('insight', '').replace('💡 현장 화법 포인트: ', '').replace('💡 현장 화법 포인트:', '').replace('💡 경쟁사 상품 참고 메모: ', '').replace('💡 경쟁사 상품 참고 메모:', '').replace('현장 화법 포인트: ', '').replace('현장 화법 포인트:', '').strip() if (not is_yt and cat_id not in ["threads_trend", "assembly_petition"]) else ""
            
            if sales_hook_text:
                badge_style_promo = 'background-color: #fef3c7; color: #b45309;' if item.get('is_promo') else ''
                text_style_promo = 'color: #92400e;' if item.get('is_promo') else ''
                news_analysis_html = f'<div class="sales-hook-card" style="{card_bg_style}"><span class="hook-badge" style="{badge_style_promo}">{hook_badge_title}</span><p class="hook-text" style="{text_style_promo}">{sales_hook_text}</p></div>'
            else:
                news_analysis_html = ""

            if cat_id == "threads_trend":
                insight_str = item.get('insight', '최근 24시간 스레드에서 가장 높은 반응도를 보인 실전 영업 포인트입니다.')
                threads_analysis_html = f'<div class="selling-insight" style="margin-top: 10px; padding: 10px; background-color: rgba(0, 0, 0, 0.04); border-left: 3px solid #000; border-radius: 6px; font-size: 0.83rem; line-height: 1.5; color: var(--text-main);"><strong>[24시간 핵심 셀링 포인트]:</strong> {insight_str}</div>'
                
            if cat_id == "assembly_petition":
                clean_pet_title = item['title'].replace('[청원] ', '')
                clean_source = item['source'].replace('국민동의청원', '국회청원')
                petition_summary = (item.get('situation') or '').strip()
                petition_summary_html = (
                    f'<div class="sales-hook-card"><span class="hook-badge">📋 청원 배경</span><p class="hook-text">{petition_summary}</p></div>'
                    if petition_summary else ''
                )
                card_html = (
                    f'<article class="card petition-item" data-category="assembly_petition">'
                    f'<div class="card-header petition-meta"><span class="badge disease-tag" style="{badge_style}">{item.get("disease_tag", "국회청원")}</span>'
                    f'<span class="source-tag petition-period" style="color: #EC4899; font-weight: 700;">{item["pub_date_str"]}</span></div>'
                    f'<h3 class="card-title petition-item-title" style="margin-top: 12px; margin-bottom: 8px; line-height: 1.5;"><a href="{item["link"]}" target="_blank" rel="noopener" style="color: var(--text-main); text-decoration: none;">[국회청원] {clean_pet_title}</a></h3>'
                    f'{petition_summary_html}'
                    f'<div class="card-footer petition-footer" style="margin-top: auto; padding-top: 12px; border-top: 1px solid var(--border-color); display: flex; justify-content: space-between; align-items: center;"><span class="card-date" style="font-size: 0.75rem; color: var(--text-muted); font-weight: 600;">{clean_source}</span>'
                    f'<a href="{item["link"]}" class="card-link btn-link" target="_blank" rel="noopener" style="color: #EC4899; font-weight: 700;">청원내용 확인하기 -></a></div></article>'
                )
                card_elements.append(card_html)
                continue

            card_html = (
                f'<div class="card {yt_class}" data-category="{cat_id}" style="cursor: pointer;" onclick="window.open(\'{item["link"]}\', \'_blank\', \'noopener,noreferrer\')">'
                f'<div class="card-header"><span class="badge" style="{badge_style}">{item["category_label"]}</span>{promo_badge_html}{source_tag_html}</div>'
                f'<div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 10px;"><h3 class="card-title">{item["title"]}</h3>{icon_svg}</div>'
                f'{comp_fact_html}{yt_analysis_html}{threads_analysis_html}{news_analysis_html}'
                f'<div class="card-footer">{date_section}<a href="{item["link"]}" target="_blank" class="card-link" rel="noopener noreferrer" onclick="event.stopPropagation()">바로가기 -></a></div></div>'
            )
            card_elements.append(card_html)
            
    cards_grid_html = "\n".join(card_elements) if card_elements else "<div class='no-data'>오늘의 브리핑 데이터가 비어 있습니다.</div>"
    escaped_mail_text = mail_text.replace('\\', '\\\\').replace('\n', '\\n').replace('\'', '\\\'').replace('\r', '')

    if notion_url:
        notion_badge_html = f'<a href="{notion_url}" target="_blank" class="btn btn-primary" style="text-decoration: none;">모바일 노션뷰</a>'
    else:
        notion_badge_html = ""

    html_template = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>$BRIEFING_TITLE - $TODAY_STR</title>
    <link rel="stylesheet" as="style" crossorigin href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.css" />
    <style>
        :root {
            --bg-body: #f1f5f9;
            --bg-glass: rgba(255, 255, 255, 0.7);
            --bg-card: #ffffff;
            --text-main: #0f172a;
            --text-sub: #475569;
            --text-muted: #64748b;
            --border-color: #e2e8f0;
            --accent-primary: #3b82f6;
            --accent-primary-hover: #2563eb;
            --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
            --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -2px rgba(0, 0, 0, 0.1);
            --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -4px rgba(0, 0, 0, 0.1);
            --glow: rgba(59, 130, 246, 0.1);
            --yt-color: #ef4444;
            --yt-glow: rgba(239, 68, 68, 0.15);
        }

        [data-theme="dark"] {
            --bg-body: #0b0f19;
            --bg-glass: rgba(15, 23, 42, 0.7);
            --bg-card: #1e293b;
            --text-main: #f8fafc;
            --text-sub: #cbd5e1;
            --text-muted: #94a3b8;
            --border-color: #334155;
            --accent-primary: #60a5fa;
            --accent-primary-hover: #3b82f6;
            --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.5);
            --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.3), 0 2px 4px -2px rgba(0, 0, 0, 0.3);
            --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.4), 0 4px 6px -4px rgba(0, 0, 0, 0.4);
            --glow: rgba(96, 165, 250, 0.15);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: "Pretendard Variable", Pretendard, -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
            transition: background-color 0.3s ease, border-color 0.3s ease, color 0.3s ease, transform 0.2s ease, box-shadow 0.2s ease;
        }

        body {
            background-color: var(--bg-body);
            color: var(--text-main);
            min-height: 100vh;
            padding: 1.5rem 1rem;
            display: flex;
            flex-direction: column;
            align-items: center;
        }

        .container {
            width: 100%;
            max-width: 1200px;
        }

        header {
            background: var(--bg-glass);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            padding: 1.75rem;
            margin-bottom: 1.5rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: var(--shadow-md);
        }

        .brand h1 {
            font-size: 1.6rem;
            font-weight: 800;
            background: linear-gradient(135deg, var(--accent-primary) 0%, #a78bfa 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.4rem;
        }

        .brand p {
            color: var(--text-sub);
            font-size: 0.9rem;
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .header-actions {
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .btn {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            color: var(--text-main);
            padding: 9px 15px;
            border-radius: 12px;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 0.85rem;
            font-weight: 600;
            box-shadow: var(--shadow-sm);
        }

        .btn:hover {
            background: var(--border-color);
            transform: translateY(-2px);
        }

        .btn-primary {
            background: var(--accent-primary);
            color: #ffffff;
            border: none;
        }

        .btn-primary:hover {
            background: var(--accent-primary-hover);
            color: #ffffff;
            box-shadow: 0 0 15px var(--glow);
        }

        .tabs-container {
            margin-bottom: 1.5rem;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            padding-bottom: 8px;
        }
        
        .tabs-container::-webkit-scrollbar {
            display: none; /* 크롬, 사파리 등 모바일 브라우저 스크롤바 숨김 */
        }

        .tabs {
            display: flex;
            gap: 8px;
            white-space: nowrap;
        }

        .tab-btn {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            color: var(--text-sub);
            padding: 9px 18px;
            border-radius: 50px;
            cursor: pointer;
            font-size: 0.9rem;
            font-weight: 600;
            box-shadow: var(--shadow-sm);
        }

        .tab-btn:hover {
            color: var(--text-main);
            border-color: var(--text-muted);
        }

        .tab-btn.active {
            background: var(--accent-primary);
            color: #ffffff;
            border-color: var(--accent-primary);
            box-shadow: 0 4px 12px var(--glow);
        }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
            gap: 1.25rem;
            width: 100%;
        }

        .card {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 1.5rem;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            min-height: 200px;
            box-shadow: var(--shadow-sm);
            position: relative;
            overflow: hidden;
        }

        .card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 4px;
            height: 100%;
            background-color: var(--accent-primary);
            opacity: 0.8;
        }

        .card.youtube-card::before {
            background-color: var(--yt-color);
        }

        .card:hover {
            transform: translateY(-5px);
            box-shadow: var(--shadow-lg);
            border-color: var(--accent-primary);
        }

        .card.youtube-card:hover {
            border-color: var(--yt-color);
            box-shadow: 0 8px 20px var(--yt-glow);
        }

        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1rem;
        }

        .badge {
            font-size: 0.75rem;
            font-weight: 700;
            padding: 4px 10px;
            border-radius: 6px;
        }

        /* 기사 하단 2~3줄 현장 화법 카드 */
        .sales-hook-card {
            background-color: #f0fdf4;
            border-left: 4px solid #16a34a;
            border-radius: 8px;
            padding: 14px 16px;
            margin-top: 14px;
            margin-bottom: 12px;
            box-shadow: 0 2px 8px rgba(22, 163, 74, 0.06);
        }

        [data-theme="dark"] .sales-hook-card {
            background-color: rgba(22, 163, 74, 0.12);
            border-left: 4px solid #34d399;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
        }

        .sales-hook-card .hook-badge {
            display: inline-flex;
            align-items: center;
            gap: 5px;
            background-color: #dcfce7;
            color: #15803d;
            font-size: 12px;
            font-weight: 700;
            padding: 3px 8px;
            border-radius: 10px;
            margin-bottom: 8px;
        }

        [data-theme="dark"] .sales-hook-card .hook-badge {
            background-color: rgba(52, 211, 153, 0.2);
            color: #6ee7b7;
        }

        .sales-hook-card .hook-text {
            font-size: 14px;
            color: #166534;
            font-weight: 500;
            line-height: 1.5;
            margin: 0;
            word-break: keep-all;
        }

        [data-theme="dark"] .sales-hook-card .hook-text {
            color: #a7f3d0;
        }

        .source-tag {
            font-size: 0.8rem;
            color: var(--text-muted);
            font-weight: 600;
        }

        .card-title {
            font-size: 1.05rem;
            line-height: 1.5;
            font-weight: 700;
            color: var(--text-main);
            margin-bottom: 1.25rem;
            display: -webkit-box;
            -webkit-line-clamp: 3;
            -webkit-box-orient: vertical;
            overflow: hidden;
            word-break: keep-all;
            flex-grow: 1;
        }

        .card-icon {
            width: 28px;
            height: 28px;
            flex-shrink: 0;
            margin-top: 2px;
        }

        .card-footer {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-top: 1px solid var(--border-color);
            padding-top: 1rem;
            margin-top: auto;
        }

        .card-date {
            font-size: 0.75rem;
            color: var(--text-muted);
            display: flex;
            align-items: center;
            gap: 4px;
        }

        .card-link {
            color: var(--accent-primary);
            text-decoration: none;
            font-size: 0.85rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 4px;
        }

        .card.youtube-card .card-link {
            color: var(--yt-color);
        }

        .card-link:hover {
            text-decoration: underline;
        }

        .no-data {
            grid-column: 1 / -1;
            text-align: center;
            padding: 3rem;
            color: var(--text-muted);
            font-size: 1.1rem;
            background: var(--bg-card);
            border-radius: 16px;
            border: 1px solid var(--border-color);
        }

        .toast {
            position: fixed;
            bottom: 2rem;
            left: 50%;
            transform: translateX(-50%) translateY(100px);
            background: #10b981;
            color: #ffffff;
            padding: 12px 24px;
            border-radius: 50px;
            font-weight: 600;
            box-shadow: 0 10px 25px rgba(16, 185, 129, 0.3);
            opacity: 0;
            z-index: 1000;
            transition: transform 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275), opacity 0.3s ease;
        }

        .toast.show {
            transform: translateX(-50%) translateY(0);
            opacity: 1;
        }

        footer {
            margin-top: 3rem;
            text-align: center;
            color: var(--text-muted);
            font-size: 0.85rem;
            padding: 2rem 0;
            border-top: 1px solid var(--border-color);
            width: 100%;
        }

        @media (max-width: 640px) {
            body {
                padding: 1rem 0.5rem;
            }
            header {
                padding: 1.25rem 1rem;
                border-radius: 16px;
                margin-bottom: 1rem;
                flex-direction: column;
                align-items: flex-start;
                gap: 1.25rem;
            }
            .brand h1 {
                font-size: 1.35rem;
            }
            .brand p {
                font-size: 0.8rem;
            }
            .header-actions {
                width: 100%;
                flex-direction: row;
                flex-wrap: wrap;
                gap: 8px;
            }
            .btn {
                padding: 8px 12px;
                font-size: 0.8rem;
                border-radius: 10px;
                flex-grow: 1;
                justify-content: center;
            }
            .tab-btn {
                padding: 8px 15px;
                font-size: 0.85rem;
            }
            .grid {
                grid-template-columns: 1fr;
                gap: 1rem;
            }
            .card {
                padding: 1.25rem;
                border-radius: 14px;
            }
            .card-title {
                font-size: 0.98rem;
                margin-bottom: 1rem;
            }
            .insight-banner {
                padding: 1rem !important;
                border-radius: 14px !important;
                margin-bottom: 1rem !important;
            }
            .insight-banner p {
                font-size: 0.88rem !important;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <header>
            <div class="brand">
                <h1>$BRIEFING_TITLE</h1>
                <p>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect>
                        <line x1="16" y1="2" x2="16" y2="6"></line>
                        <line x1="8" y1="2" x2="8" y2="6"></line>
                        <line x1="3" y1="10" x2="21" y2="10"></line>
                    </svg>
                    $TODAY_STR 기준 업데이트
                </p>
            </div>
            <div class="header-actions">
                <button class="btn" id="theme-toggle" onclick="toggleTheme()" title="화면 테마 변경">
                    <svg id="theme-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>
                    </svg>
                    <span>테마 변경</span>
                </button>
                $NOTION_BADGE
                <button class="btn btn-primary" onclick="copyMailText()">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
                    </svg>
                    메일 텍스트 복사
                </button>
            </div>
        </header>

        <!-- Daily Insight Callout Banner -->
        <div class="insight-banner" style="background: linear-gradient(135deg, rgba(20, 184, 166, 0.08) 0%, rgba(59, 130, 246, 0.08) 100%); border-left: 5px solid #14b8a6; padding: 1.2rem; border-radius: 16px; margin-bottom: 2rem; box-shadow: var(--shadow-sm); display: flex; flex-direction: column; gap: 4px;">
            <span style="font-size: 0.8rem; font-weight: 800; color: #14b8a6; text-transform: uppercase; letter-spacing: 0.05em; display: flex; align-items: center; gap: 6px;">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .6 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5"></path>
                    <path d="M9 18h6"></path>
                    <path d="M10 22h4"></path>
                </svg>
                오늘의 한마디 (Market Insight)
            </span>
            <p style="font-size: 0.95rem; font-weight: 700; line-height: 1.5; color: var(--text-main); word-break: keep-all;">
                $DAILY_INSIGHT
            </p>
        </div>

        <!-- Categories Navigation Tabs -->
        <div class="tabs-container">
            <div class="tabs">
                <button class="tab-btn active" onclick="filterCategory('all', this)">전체보기</button>
                <button class="tab-btn" onclick="filterCategory('policy', this)">제도·정책 이슈</button>
                <button class="tab-btn" onclick="filterCategory('reality', this)">질병·치료비 리얼리티</button>
                <button class="tab-btn" onclick="filterCategory('caregiving', this)">간병·돌봄 대란</button>
                <button class="tab-btn" onclick="filterCategory('market', this)">상품·시장 동향</button>
                <button class="tab-btn" onclick="filterCategory('motivation', this)">성공·동기부여</button>
                <button class="tab-btn" onclick="filterCategory('assembly_petition', this)">국회청원 (비급여/급여화)</button>
                <button class="tab-btn" onclick="filterCategory('youtube', this)">유튜브 핫이슈</button>
            </div>
        </div>

        <!-- News Card Grid -->
        <section class="grid">
            $CARDS_GRID
        </section>
    </div>

    <!-- Hidden textarea for easy clipboard copying -->
    <textarea id="mail-text-source" style="display: none;">$ESCAPED_MAIL_TEXT</textarea>

    <!-- Toast Notification -->
    <div id="toast" class="toast">메일용 브리핑 텍스트가 복사되었습니다!</div>

    <!-- Footer -->
    <footer>
        <p>© 2026 Morning Briefing Agent. All rights reserved.</p>
        <p style="margin-top: 4px; font-size: 0.75rem;">본 자료는 실시간 구글 뉴스 및 유튜브 정보를 집계한 자료이며, 각 기사 및 영상의 저작권은 각 언론사 및 크리에이터에게 있습니다.</p>
    </footer>

    <!-- Logic Script -->
    <script>
        const savedTheme = localStorage.getItem('theme');
        const systemPrefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        
        if (savedTheme === 'dark' || (!savedTheme && systemPrefersDark)) {
            document.body.setAttribute('data-theme', 'dark');
            updateThemeIcon('dark');
        } else {
            document.body.setAttribute('data-theme', 'light');
            updateThemeIcon('light');
        }

        function toggleTheme() {
            const currentTheme = document.body.getAttribute('data-theme');
            const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
            document.body.setAttribute('data-theme', newTheme);
            localStorage.setItem('theme', newTheme);
            updateThemeIcon(newTheme);
        }

        function updateThemeIcon(theme) {
            const iconSvg = document.getElementById('theme-icon');
            if (theme === 'dark') {
                iconSvg.innerHTML = `
                    <circle cx="12" cy="12" r="5"></circle>
                    <line x1="12" y1="1" x2="12" y2="3"></line>
                    <line x1="12" y1="21" x2="12" y2="23"></line>
                    <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line>
                    <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line>
                    <line x1="1" y1="12" x2="3" y2="12"></line>
                    <line x1="21" y1="12" x2="23" y2="12"></line>
                    <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line>
                    <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line>
                `;
            } else {
                iconSvg.innerHTML = `
                    <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>
                `;
            }
        }

        function filterCategory(category, element) {
            const tabs = document.querySelectorAll('.tab-btn');
            tabs.forEach(tab => tab.classList.remove('active'));
            if (element) element.classList.add('active');

            const cards = document.querySelectorAll('.grid .card');

            cards.forEach(card => {
                const cardCat = card.getAttribute('data-category');
                const badge = card.querySelector('.badge');
                const cardLabel = badge ? badge.innerText.trim() : '';

                if (category === 'all') {
                    card.style.display = 'flex';
                } else if (category === 'policy' && (cardLabel.includes('제도') || cardLabel.includes('정책') || cardCat === 'silson' || cardCat === 'fss_reform' || cardCat === 'reform_insurance')) {
                    card.style.display = 'flex';
                } else if (category === 'reality' && (cardLabel.includes('질병') || cardLabel.includes('치료비') || cardCat === 'hospital_cost' || cardCat === 'medtech')) {
                    card.style.display = 'flex';
                } else if (category === 'caregiving' && (cardLabel.includes('간병') || cardLabel.includes('돌봄') || cardCat === 'caregiving')) {
                    card.style.display = 'flex';
                } else if (category === 'market' && (cardLabel.includes('상품') || cardLabel.includes('시장') || cardCat === 'product_trend')) {
                    card.style.display = 'flex';
                } else if (category === 'motivation' && (cardLabel.includes('성공') || cardLabel.includes('동기부여') || cardCat === 'motivation')) {
                    card.style.display = 'flex';
                } else if (cardCat === category) {
                    card.style.display = 'flex';
                } else {
                    card.style.display = 'none';
                }
            });
        }

        function copyMailText() {
            const mailTextSource = document.getElementById('mail-text-source');
            mailTextSource.style.display = 'block';
            mailTextSource.select();
            mailTextSource.setSelectionRange(0, 99999);
            
            try {
                document.execCommand('copy');
                showToast();
            } catch (err) {
                alert('텍스트 복사 중 오류가 발생했습니다. 다시 시도해 주세요.');
            }
            
            mailTextSource.style.display = 'none';
        }

        function showToast() {
            const toast = document.getElementById('toast');
            toast.classList.add('show');
            setTimeout(() => {
                toast.classList.remove('show');
            }, 2500);
        }

        // 페이지 초기 로드 시 전체보기 1건 제한 필터링 자동 적용
        document.addEventListener('DOMContentLoaded', () => {
            const activeTab = document.querySelector('.tab-btn.active');
            filterCategory('all', activeTab);
        });
    </script>
</body>
    """
    return html_template.replace("$BRIEFING_TITLE", BRIEFING_TITLE)\
                        .replace("$TODAY_STR", today_str)\
                        .replace("$CARDS_GRID", cards_grid_html)\
                        .replace("$ESCAPED_MAIL_TEXT", escaped_mail_text)\
                        .replace("$DAILY_INSIGHT", generate_daily_insight(data).replace('★ [오늘의 한마디] ', ''))\
                        .replace("$NOTION_BADGE", notion_badge_html)

def build_notion_blocks(data):
    """Notion API 규격에 맞는 페이지 내 블록 리스트 생성.
    - 동일 라벨 카테고리(예: 제도·정책 이슈 x3)를 하나의 섹션으로 병합
    - 기사별 callout 블록 (카드형 UI) 복원
    - YouTube 항목: link를 rich_text inline으로 처리 → 모바일 대형 플레이어 자동임베드 차단
    """
    insight = generate_daily_insight(data)
    blocks = []

    # ── 인트로 단락 ──────────────────────────────────────────
    blocks.append({
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{
                "type": "text",
                "text": { "content": "오늘 아침 주요 실전 테마 뉴스 브리핑 및 유튜브 영업 소구점 분석입니다. (제목 클릭 시 링크 이동)" }
            }]
        }
    })

    # ── 오늘의 Market Insight callout ────────────────────────
    blocks.append({
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [{
                "type": "text",
                "text": { "content": insight }
            }],
            "icon": { "type": "emoji", "emoji": "💡" },
            "color": "blue_background"
        }
    })

    # ── 동일 라벨 카테고리 병합: label 기준으로 items 합산 ──────
    label_order = []       # CATEGORIES 정의 순서 유지
    label_items_map = {}   # label → merged item list

    for cat_id, info in CATEGORIES.items():
        items = data.get(cat_id, [])
        label = info["label"]
        if label not in label_items_map:
            label_items_map[label] = []
            label_order.append(label)
        label_items_map[label].extend(items)

    # 카테고리별 이모지 및 callout 색상 매핑
    label_emoji = {
        "제도·정책 이슈":          "📋",
        "질병·치료비 리얼리티":    "💊",
        "시즌·이슈":               "☀️",
        "상품·시장 동향":          "📊",
        "성공·동기부여":           "⭐",
        "유튜브 핫이슈":           "▶️",
        "보험 블로그 & 담보 비교": "🔍",
        "국회청원 (비급여/급여화)":"📜",
        "스레드 핫이슈":           "🧵",
    }
    label_color = {
        "제도·정책 이슈":          "blue_background",
        "질병·치료비 리얼리티":    "green_background",
        "시즌·이슈":               "gray_background",
        "상품·시장 동향":          "default",
        "성공·동기부여":           "purple_background",
        "유튜브 핫이슈":           "red_background",
        "보험 블로그 & 담보 비교": "default",
        "국회청원 (비급여/급여화)":"pink_background",
        "스레드 핫이슈":           "default",
    }

    # ── 섹션별 블록 생성 (아이템 없는 섹션은 헤더 포함 통째 생략) ──
    for label in label_order:
        items = label_items_map[label]

        # 기사 0건 섹션: 헤더 자체를 생성하지 않고 건너뜀
        if not items:
            continue

        # 섹션 헤딩: 순수 라벨명만 (괄호 부연설명 제거)
        header_text = f"■ {label}"

        blocks.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{
                    "type": "text",
                    "text": { "content": header_text }
                }]
            }
        })

        emoji = label_emoji.get(label, "📰")
        color = label_color.get(label, "default")
        is_youtube_section = (label == "유튜브 핫이슈")

        for idx, item in enumerate(items, 1):
            # ── callout rich_text 구성 ──
            title_str = f"{idx}. {item['title']}"

            if is_youtube_section:
                # YouTube: 제목을 plain text로 (link 없음) → Notion 자동 임베드 방지
                title_rt = {
                    "type": "text",
                    "text": { "content": title_str },
                    "annotations": { "bold": True }
                }
            else:
                title_rt = {
                    "type": "text",
                    "text": {
                        "content": title_str,
                        "link": { "url": item["link"] }
                    },
                    "annotations": { "bold": True }
                }

            rich_text = [title_rt]

            # 영업 인사이트/화법 삽입
            hook = item.get("insight", "").strip()
            if hook:
                rich_text.append({
                    "type": "text",
                    "text": { "content": f"\n💡 {hook}" }
                })

            # 메타 정보 (출처/채널, 날짜/조회수)
            if is_youtube_section:
                meta = f"\n   채널: {item.get('source', '')} | {item.get('view_count_str', '')} | {item.get('pub_date_str', '')}"
            else:
                meta = f"\n   출처: {item.get('source', '')} | 등록일: {item.get('pub_date_str', '')}"

            rich_text.append({
                "type": "text",
                "text": { "content": meta },
                "annotations": { "color": "gray", "italic": True }
            })

            # callout 블록으로 카드형 UI 구성
            blocks.append({
                "object": "block",
                "type": "callout",
                "callout": {
                    "rich_text": rich_text,
                    "icon": { "type": "emoji", "emoji": emoji },
                    "color": color
                }
            })

            # YouTube 전용: 링크 단독 paragraph (작은 크기, 임베드 없음)
            if is_youtube_section:
                blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{
                            "type": "text",
                            "text": {
                                "content": "▶ 영상 바로 보기",
                                "link": { "url": item["link"] }
                            },
                            "annotations": { "color": "red", "bold": True }
                        }]
                    }
                })

    return blocks

def publish_to_notion_db(blocks, today_str):
    """Notion API를 활용해 노션 데이터베이스에 새로운 행(페이지)을 생성하고 콘텐츠 블록을 추가한 뒤 공유 주소 반환"""
    if not NOTION_TOKEN or NOTION_TOKEN == "secret_..." or not NOTION_DATABASE_ID or NOTION_DATABASE_ID == "...":
        print("[정보] Notion API 설정(토큰 또는 데이터베이스 ID)이 비어 있습니다. 노션 페이지 생성을 생략합니다.")
        return None
        
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    title_text = f"보험 & 건강 아침 브리핑 ({today_str})"
    date_str = datetime.now().strftime("%Y-%m-%d")
    
    # 노션 데이터베이스 테이블 컬럼 속성명 대응 (Name/이름, Date/날짜 등 다양한 조합 시도)
    properties_variants = [
        {"Name": {"title": [{"text": {"content": title_text}}]}, "날짜": {"date": {"start": date_str}}},
        {"Name": {"title": [{"text": {"content": title_text}}]}, "Date": {"date": {"start": date_str}}},
        {"이름": {"title": [{"text": {"content": title_text}}]}, "날짜": {"date": {"start": date_str}}},
        {"이름": {"title": [{"text": {"content": title_text}}]}, "Date": {"date": {"start": date_str}}},
        {"Name": {"title": [{"text": {"content": title_text}}]}},
        {"이름": {"title": [{"text": {"content": title_text}}]}}
    ]
    
    page_id = None
    context = ssl._create_unverified_context()
    
    for idx, props in enumerate(properties_variants):
        payload = {
            "parent": { "database_id": NOTION_DATABASE_ID },
            "properties": props
        }
        try:
            data_bytes = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15, context=context) as response:
                res_data = json.loads(response.read().decode("utf-8"))
            page_id = res_data.get("id")
            if page_id:
                break
        except Exception as e:
            if idx == len(properties_variants) - 1:
                print(f"[오류] 노션 데이터베이스 페이지 생성 최종 실패: {e}")
                return None
                
    if page_id:
        clean_page_id = page_id.replace("-", "")
        notion_url = f"https://notion.so/{clean_page_id}"
        print(f"[완료] Notion 데이터베이스 페이지 생성 성공! ID: {page_id}")
        
        try:
            append_notion_children(page_id, blocks, headers)
            print("[완료] 노션 페이지 내 카드뉴스 상세 블록(자식 블록) 일괄 기입 완료.")
        except Exception as e:
            print(f"[경고] 노션 자식 블록 기입 실패 (페이지는 생성됨): {e}")
            
        return notion_url
        
    return None

def append_notion_children(page_id, blocks, headers):
    """노션 페이지에 자식 블록들을 100개씩 청크 단위로 나누어 추가"""
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    context = ssl._create_unverified_context()
    chunk_size = 80
    
    for i in range(0, len(blocks), chunk_size):
        chunk = blocks[i:i+chunk_size]
        payload = { "children": chunk }
        data_bytes = json.dumps(payload).encode("utf-8")
        
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method="PATCH")
        with urllib.request.urlopen(req, timeout=15, context=context) as response:
            response.read()

def push_to_github():
    """deploy 폴더의 index.html 파일을 깃허브 레포지토리에 자동으로 커밋 및 푸시하여 웹 주소 자동 갱신"""
    if CI_MODE:
        print("[CI] push_to_github 생략 (Actions 가 직접 커밋)")
        return
    import subprocess
    deploy_dir = "deploy"
    if not os.path.exists(os.path.join(deploy_dir, ".git")):
        print("[정보] deploy 폴더 내에 Git 저장소가 초기화되지 않았습니다. 업로드를 생략합니다.")
        return
        
    print("[정보] 깃허브(GitHub Pages)에 최신 리포트 자동 업로드 중...")
    try:
        # 1. git add index.html data/threads_hot.json
        subprocess.run(["git", "add", "index.html", "data/threads_hot.json"], cwd=deploy_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 2. git commit -m "Auto-update briefing"
        subprocess.run(["git", "commit", "-m", "Auto-update briefing"], cwd=deploy_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 3. git push
        subprocess.run(["git", "push", "origin", "main"], cwd=deploy_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("[완료] 깃허브 페이지 자동 배포 완료! 주소: https://exodusy5351-lgtm.github.io/morning_briefing/")
    except Exception as e:
        print(f"[경고] 깃허브 자동 배포 중 오류 발생: {e}")

def save_to_google_sheet(data, spreadsheet_id):
    """수집된 데이터를 구글 스프레드시트에 행(Row) 단위로 깔끔하게 누적 저장 (중복 배제)"""
    if CI_MODE:
        print("[CI] Google Sheet 저장 생략")
        return
    if not spreadsheet_id:
        print("[정보] GOOGLE_SPREADSHEET_ID 설정이 비어 있습니다. 구글 시트 연동을 건너뜁니다.")
        return
        
    credentials_path = "credentials.json"
    if not os.path.exists(credentials_path):
        print(f"[경고] 구글 시트 연동을 위한 '{credentials_path}' 파일이 폴더 내에 없습니다.")
        print("  - 가이드 문서(glide_credentials_guide.md)를 확인하여 서비스 계정 키를 배치해 주세요.")
        return
        
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        import hashlib
        
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        
        creds = Credentials.from_service_account_file(credentials_path, scopes=scopes)
        client = gspread.authorize(creds)
        
        # 스프레드시트 열기
        sheet = client.open_by_key(spreadsheet_id).get_worksheet(0)
        
        # 헤더 확인 및 생성
        headers = ["ID", "Date", "Category", "Title", "Source", "PubDate", "Link", "Summary", "Hashtags", "ComparisonFact", "MotivationQuote", "CreatedAt"]
        
        try:
            row_one = sheet.row_values(1)
            if not row_one:
                sheet.append_row(headers)
            else:
                if "ID" not in row_one:
                    sheet.insert_row(headers, 1)
        except Exception:
            sheet.append_row(headers)
            
        # 기존 ID 목록 불러와 중복 확인용 리스트 빌드
        all_rows = sheet.get_all_records()
        existing_ids = {str(r.get("ID", "")) for r in all_rows if r.get("ID")}
        
        today_str = datetime.now().strftime("%Y-%m-%d")
        created_at_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        rows_to_append = []
        
        for cat_id, items in data.items():
            for item in items:
                title = item.get("title", "")
                link = item.get("link", "")
                
                # 고유 ID 및 메타데이터 맵 빌드
                category_label = item.get("category_label", "")
                source = item.get("source", "")
                pub_date = item.get("pub_date_str", "")
                summary = item.get("summary", "")
                hashtags_str = ""
                comp_fact = ""
                motivation_quote = ""
                
                unique_hash = hashlib.md5(f"{title}{link}".encode("utf-8")).hexdigest()
                if unique_hash in existing_ids:
                    continue
                    
                if cat_id == "youtube" and "analysis" in item:
                    hashtags_str = ", ".join(item["analysis"].get("hashtags", []))
                    summary = item["analysis"].get("summary", "")
                elif cat_id == "product_trend":
                    comp_fact = f"{title[:60]} 관련 삼성화재 상품 대비 보장 우위 점검 필요" if title else ""
                elif cat_id == "motivation":
                    motivation_quote = "우리의 가치는 고객의 약속을 지키는 힘입니다. 자부심을 가지고 힘차게 나아갑시다!"
                        
                row_data = [
                    unique_hash,
                    today_str,
                    category_label,
                    title,
                    source,
                    pub_date,
                    link,
                    summary,
                    hashtags_str,
                    comp_fact,
                    motivation_quote,
                    created_at_str
                ]
                rows_to_append.append(row_data)
                existing_ids.add(unique_hash)
                
        if rows_to_append:
            sheet.append_rows(rows_to_append)
            print(f"[완료] 구글 스프레드시트에 새로운 데이터 {len(rows_to_append)}행 누적 추가 완료.")
        else:
            print("[정보] 구글 스프레드시트에 추가할 중복되지 않은 새로운 뉴스가 없습니다.")
            
    except Exception as e:
        print(f"[경고] 구글 스프레드시트 연동 중 에러 발생: {e}")
        print("  - 가이드 문서(glide_credentials_guide.md)의 세부 설정(스프레드시트 ID 확인, 공유 권한 등)을 다시 점검해 주세요.")

def main():
    import time
    start_t = time.time()
    today = datetime.now()
    today_str = today.strftime("%Y년 %m월 %d일")
    file_suffix = today.strftime("%Y%m%d")
    
    print("==================================================")
    print(f"  [시작] 고도화된 보험 아침 브리핑 수집 에이전트 ({today_str})")
    print("==================================================")
    
    # 1. 8대 테마 뉴스 및 유튜브 트렌드 데이터 수집
    data = compile_briefing_data()
    
    # 1.2. 오늘 노출된 기사 URL 목록 저장 (최근 5일 중복 차단)
    published_urls = []
    for cat_id, items in data.items():
        for item in items:
            if item.get("link"):
                published_urls.append({"link": item.get("link"), "title": item.get("title", "")})
    if not DRY_RUN:
        save_recent_urls(published_urls)
    else:
        print("[DRY-RUN] 최근 URL 중복차단 이력 저장 생략")

    # 1.5. 구글 스프레드시트 누적 저장 연동
    if not DRY_RUN:
        save_to_google_sheet(data, GOOGLE_SPREADSHEET_ID)
    else:
        print("[DRY-RUN] 구글 스프레드시트 저장 생략")

    # 2. 노션용 블록 생성 및 노션 데이터베이스 페이지 발행
    notion_blocks = build_notion_blocks(data)
    if not DRY_RUN:
        notion_url = publish_to_notion_db(notion_blocks, today_str)
    else:
        print("[DRY-RUN] 노션 페이지 발행 생략")
        notion_url = None

    # 3. 결과 포맷 빌드 (노션 링크 연동 포함)
    print("[정보] 메일용 텍스트 브리핑 렌더링 중...")
    mail_text = build_mail_text(data, today_str, notion_url)
    
    print("[정보] HTML 카드뉴스 브리핑 렌더링 중...")
    html_card_news = build_html_card_news(data, today_str, mail_text, notion_url)
    
    # 4. 파일 입출력 저장
    txt_filename = f"morning_briefing_{file_suffix}.txt"
    html_filename = f"morning_briefing_{file_suffix}.html"
    
    # 넷리파이(Netlify) 업로드용 deploy 폴더 자동 구성
    deploy_dir = "." if CI_MODE else "deploy"
    if not os.path.exists(deploy_dir):
        os.makedirs(deploy_dir)
    deploy_html_path = os.path.join(deploy_dir, "index.html")
    
    # deploy/data/threads_hot.json 복사 동기화 (웹 카드뉴스 Threads 데이터 로드 보장)
    deploy_data_dir = os.path.join(deploy_dir, "data")
    os.makedirs(deploy_data_dir, exist_ok=True)
    src_threads = os.path.join("data", "threads_hot.json")
    dst_threads = os.path.join(deploy_data_dir, "threads_hot.json")
    if os.path.exists(src_threads):
        import shutil
        if os.path.abspath(src_threads) == os.path.abspath(dst_threads):
            print(f"[정보] 원본과 대상 경로가 동일하여 복사를 생략합니다.")
        else:
            shutil.copy(src_threads, dst_threads)
            print(f"[완료] deploy/data/threads_hot.json 복사 완료.")

    try:
        with open(txt_filename, "w", encoding="utf-8") as f:
            f.write(mail_text)
        print(f"[완료] 텍스트 파일 저장 성공: {txt_filename}")
    except Exception as e:
        print(f"[오류] 텍스트 파일 저장 실패: {e}")
        
    try:
        with open(html_filename, "w", encoding="utf-8") as f:
            f.write(html_card_news)
        print(f"[완료] HTML 카드뉴스 파일 저장 성공: {html_filename}")
    except Exception as e:
        print(f"[오류] HTML 카드뉴스 파일 저장 실패: {e}")
        
    try:
        with open(deploy_html_path, "w", encoding="utf-8") as f:
            f.write(html_card_news)
        print(f"[완료] 넷리파이 배포용 파일 생성 성공: {deploy_html_path}")
    except Exception as e:
        print(f"[오류] 넷리파이 배포용 파일 생성 실패: {e}")
        
    # 깃허브 자동 배포 트리거 호출 (deploy 폴더의 최신 index.html 푸시)
    if not DRY_RUN:
        push_to_github()
    else:
        print("[DRY-RUN] 깃허브 자동 배포 생략")
    
    elapsed = time.time() - start_t
    tot = GEMINI_STATS["total_articles"]
    succ = GEMINI_STATS["gemini_success"]
    c_fail = GEMINI_STATS["fallback_crawling_failed"]
    a_fail = GEMINI_STATS["fallback_api_failed"]
    succ_pct = (succ / tot * 100) if tot > 0 else 0
    c_fail_pct = (c_fail / tot * 100) if tot > 0 else 0
    a_fail_pct = (a_fail / tot * 100) if tot > 0 else 0

    print("\n==================================================")
    print(f"  [성공] 브리핑 에이전트 수집 및 빌드 프로세스 완료 (총 소요시간: {elapsed:.1f}초)")
    print("==================================================")
    print("  [Gemini API 성공률 및 폴백 실증 통계 리포트]")
    print(f"    - 총 평가 대상 기사 수: {tot}건")
    print(f"    - Gemini 3.5 Flash 성공: {succ}건 ({succ_pct:.1f}%)")
    print(f"    - 본문 크롤링 부실 (<20자) 폴백: {c_fail}건 ({c_fail_pct:.1f}%)")
    print(f"    - API Quota 429/타임아웃 폴백: {a_fail}건 ({a_fail_pct:.1f}%)")
    if GEMINI_STATS["crawling_failed_publishers"]:
        print(f"    - 본문 크롤링 실패 언론사 목록: {dict(GEMINI_STATS['crawling_failed_publishers'])}")
    print("==================================================")

if __name__ == "__main__":
    main()
