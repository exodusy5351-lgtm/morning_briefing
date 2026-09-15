"""브리핑 필터·화법 회귀 테스트 — 실제 운영에서 겪은 오탐/미탐 사례 모음.
실행: python test_briefing_filters.py  (네트워크·Gemini 호출 없음)
"""
import contextlib
import io
import sys

sys.argv = [sys.argv[0], "--dry-run", "--no-gemini"]
with contextlib.redirect_stdout(io.StringIO()):
    import briefing_agent as b
import google.genai as genai_mod


def adopted(title, body=""):
    with contextlib.redirect_stdout(io.StringIO()):
        return b.evaluate_article_cot(title, body, hook=False)["adopted"]


def score(title):
    return b.calculate_sales_relevance_score({"title": title, "summary": ""})


fails = []

# 1. 영업에 쓸 수 있는데 체크리스트 1개 적중으로 탈락하던 기사 (2026-09-15 실행 로그)
MUST_ADOPT = [
    "똑같은 수술도 로봇이 하면 336배 비싸",
    "“절개·봉합은 단순처치 아닌 수술”…법원, 심평원 ‘비급여 환불’ 취소",
    "첫째 제왕절개 안 알렸다고 둘째 태아보험 해지?…금감원 제동",
    "대학병원 진단에도 의료자문 요구…금감원, 보험사 과잉 자문 '제동'",
    "할인받은 진료비, 실손보험에서 받을 수 있을까?",
    "\"체외충격파 '주 1회', 꼭 7일 간격 아니다\"…금감원, 보상기준 안내",
    "간병비 줄이는 '에이지테크', 연금 장수위험은 키울 수 있다",
    "가족 간병은 못 받는다?…문턱 높인 간병보험",
    "[은지 아빠의 인생 2막 준비학교] 100세 시대의 그늘… 폭증하는 간병비 ②",
    "“하지정맥류 입원 불필요?”…실손 심사 강화에 의료계 반발",
    "방광암 치료환경 변화 '성큼'…'키트루다+파드셉', 약가 협상 타결",
]
for t in MUST_ADOPT:
    if not adopted(t):
        fails.append(f"미탐(채택돼야 함): {t}")
    elif score(t) <= 0:
        fails.append(f"점수 0 이하(최종 컷에서 탈락): {t}")

# 2. 영업과 무관한데 통과했거나 통과할 뻔한 기사 (과거 사고 사례 포함)
MUST_REJECT = [
    "상반기 車보험 총손익 37.7%↓…보험손익 6년만 적자전환",
    "車보험료 올랐는데 1848억 적자…‘나이롱환자’ 잡으면 내려갈까",
    "부산 배달·대리기사 산재보험료 부담 줄인다…본인부담 80% 지원",
    "보험업계, 예보료율 최대 3배 인상안에 ‘반발’...“IFRS17 반영해야”",
    "고흥군, 저소득 어르신 틀니·임플란트 본인부담금 지원 확대",
    "사우디, 국가 재보험사 설립 추진…지정학적 리스크 대응",
    "금감원 임직원, 6년간 주식투자 규정 위반 147명 적발...징계는 단 4명",
    "'제식구 감싸기' 비판 부른 금감원… 주식투자 위반 징계 고작 2.7%",
    "정책금융기관 기후금융 42조원 공급 … 연간 목표 74% 달성",
    "동의대 학생팀, 가족간병 보험 개선안으로 전보련서 금감원장상 “보험은 삶의 질을 높이는 산업”",
]
for t in MUST_REJECT:
    if adopted(t):
        fails.append(f"오탐(탈락해야 함): {t}")

# 3. 스마트 파서 화법
h = b.generate_smart_fact_hook("상반기 車보험 총손익 37.7%↓…보험손익 6년만 적자전환", "", "상품·시장 동향")
if "(7%)" in h or "절판" in h:
    fails.append(f"화법: 소수점 절단/무관한 절판 문구: {h}")
h1 = b.generate_smart_fact_hook("에트카마, 유방암 1차 치료 3상 실패…ESR1 변이 조기 전환은 ‘유효’", "", "질병·치료비 리얼리티")
h2 = b.generate_smart_fact_hook("바이오엔테크 폐암 신약, 생존기간 18.5개월…표준치료 크게 앞서", "", "질병·치료비 리얼리티")
if h1 == h2 or "유방암" not in h1 or "폐암" not in h2:
    fails.append(f"화법: 서로 다른 기사가 같은 문장/병명 누락: {h1} | {h2}")
if "136만 원" not in b.generate_smart_fact_hook("226만 명이 평균 136만 원 돌려받아", "", "질병·치료비 리얼리티"):
    fails.append("화법: 금액 언급 누락")
h = b.generate_smart_fact_hook("신약 허가받아도 건보 적용까지 2년…\"8개월로 줄여야\"", "", "제도·정책 이슈")
if "보험금 청구" in h or "심사" in h:
    fails.append(f"화법: 급여 기사에 심사 문구: {h}")
h = b.generate_smart_fact_hook("똑같은 수술도 로봇이 하면 336배 비싸", "로봇수술 특약", "상품·시장 동향")
if "수술" not in h:
    fails.append(f"화법: 수술비 기사에 무관한 상품 문구: {h}")
cg = {b.generate_smart_fact_hook(t, "", "간병·돌봄 대란") for t in ["간병 기사 A", "간병 기사 B", "간병 기사 C"]}
if len(cg) < 3:
    fails.append(f"화법: 같은 카테고리 카드끼리 문장 중복: {cg}")
t = "“하지정맥류 입원 불필요?”…실손 심사 강화에 의료계 반발"
with contextlib.redirect_stdout(io.StringIO()):
    cat = b.evaluate_article_cot(t, "", hook=False)["category"]
if cat != "제도·정책 이슈":
    fails.append(f"분류: 실손 심사 강화 기사가 {cat}")
if "실손 관련 새 치료" in b.generate_smart_fact_hook(t, "", "질병·치료비 리얼리티"):
    fails.append("화법: 병명이 아닌 단어가 치료 화법 주어로 쓰임")
for cat in ["간병·돌봄 대란", "제도·정책 이슈", "시즌·이슈", "상품·시장 동향", "기타"]:
    if not b.generate_smart_fact_hook("간병비 월 400만원 시대", "", cat):
        fails.append(f"화법: 빈 문장 ({cat})")

# 4. 유튜브 스마트 요약
with contextlib.redirect_stdout(io.StringIO()):
    yt1 = b.analyze_youtube_video(
        "암 진료비 지원받는 7가지 방법ㅣ문용화 교수님 1부ㅣ닥터딩요",
        '문용화 교수님 채널 : https://www.youtube.com/@KO-CANCER/videos "암을 이겨내는 사람들의 비밀 책" 구매 링크 교보문고: ...',
        "닥터딩요")["summary"]
    yt2 = b.analyze_youtube_video(
        "6천만원짜리 전기택시인데 보험료 236만원… 이게 가능합니다",
        "개인택시 사보험, 도대체 얼마나 차이가 날까요? 이번 영상에서는 말로만 “보험료가 내려갑니다”라고 설명하지 않습니다.",
        "이영민의 개인택시")["summary"]
if "http" in yt1 or "구매 링크" in yt1 or "(7, 1)" in yt1:
    fails.append(f"유튜브: 설명란 홍보문구/무의미 숫자 노출: {yt1}")
if "6천만원" not in yt2 or "236만원" not in yt2 or "차이가 날까요" not in yt2:
    fails.append(f"유튜브: 수치/설명 문장 누락: {yt2}")


# 5. Gemini 재시도(503 두 번 후 성공) + 서킷 브레이커
class _Resp:
    text = '{"ok": 1}'


class _FakeClient:
    calls = 0

    def __init__(self, **kw):
        self.models = self

    def generate_content(self, **kw):
        _FakeClient.calls += 1
        if _FakeClient.calls < 3:
            raise RuntimeError("503 UNAVAILABLE")
        return _Resp()


orig_client, orig_sleep = genai_mod.Client, b.time.sleep
genai_mod.Client, b.time.sleep = _FakeClient, (lambda s: None)
try:
    with contextlib.redirect_stdout(io.StringIO()):
        if b.gemini_generate("k", "p").text != '{"ok": 1}' or _FakeClient.calls != 3:
            fails.append(f"Gemini: 503 재시도 실패 (호출 {_FakeClient.calls}회)")
        _FakeClient.calls = -100  # 이후 계속 실패
        for _ in range(b.GEMINI_BREAKER_LIMIT):
            try:
                b.gemini_generate("k", "p")
            except Exception:
                pass
        before = _FakeClient.calls
        try:
            b.gemini_generate("k", "p")
        except RuntimeError as e:
            if "서킷" not in str(e) or _FakeClient.calls != before:
                fails.append("Gemini: 서킷 브레이커 미동작")
finally:
    genai_mod.Client, b.time.sleep = orig_client, orig_sleep
    b._GEMINI_FAIL_STREAK[0] = 0

if fails:
    print("\n".join(["[FAIL] " + f for f in fails]))
    sys.exit(1)
print(f"[OK] 채택 {len(MUST_ADOPT)}건 · 차단 {len(MUST_REJECT)}건 · 화법/유튜브/Gemini 재시도 검증 통과")
