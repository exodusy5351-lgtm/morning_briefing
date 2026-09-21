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


# 5. Gemini 재시도(503 → 504 후 성공) + 서킷 브레이커
class _Resp:
    text = '{"ok": 1}'


class _FakeClient:
    calls = 0

    def __init__(self, **kw):
        self.models = self

    def generate_content(self, **kw):
        _FakeClient.calls += 1
        if _FakeClient.calls < 3:
            raise RuntimeError("503 UNAVAILABLE" if _FakeClient.calls == 1 else "504 DEADLINE_EXCEEDED")
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

# 6. 금감원 게시판 파싱·필터·화법 (실제 목록 HTML 구조 그대로)
SAMPLE = """<tbody>
<tr><td class="num"> 20880 </td><td class="title"><a href="/fss/bbs/B0000188/view.do?nttId=230052&menuNo=200218&pageIndex=1">풍성해야 할 한가위, ‘가짜 투자’에 눈물 흘리지 않으려면「</a></td><td>민생침해대응총괄국</td><td> 2026-09-21 </td><td>x</td></tr>
<tr><td class="num"> 20879 </td><td class="title"><a href="/fss/bbs/B0000188/view.do?nttId=230051&menuNo=200218&pageIndex=1">무료 강연, 박람회에서 보험 가입 시 소비자 유의사항</a></td><td>소비자피해예방국</td><td> 2026-09-21 </td><td>x</td></tr>
<tr><td class="num"> 20870 </td><td class="title"><a href="/fss/bbs/B0000188/view.do?nttId=229239&menuNo=200218&pageIndex=1">2026년 제49회 보험계리사 및 손해사정사 최종 합격자 발표</a></td><td>보험감독국</td><td> 2026-09-18 </td><td>x</td></tr>
</tbody>"""
rows = b.parse_fss_board(SAMPLE)
if len(rows) != 3 or rows[1] != ("230051", "무료 강연, 박람회에서 보험 가입 시 소비자 유의사항", "소비자피해예방국", "2026-09-21"):
    fails.append(f"금감원: 목록 파싱 실패 {rows}")
elif rows[0][1].endswith("「"):
    fails.append(f"금감원: 제목 끝 여는 괄호 미제거 {rows[0][1]}")
else:
    keep = [b.fss_relevant(r[1]) for r in rows]
    if keep != [False, True, False]:
        fails.append(f"금감원: 관련성 필터 오류(가짜투자/보험가입유의/계리사합격 = F,T,F 기대) {keep}")
if "유의사항" not in b.fss_hook(rows[1][1], "보도자료") or "소비자경보" not in b.fss_hook("보험 사칭 주의", "소비자경보"):
    fails.append("금감원: 화법 분기 오류")

# 7. 빈출 키워드: 라벨 매칭 + 최근 30일 창 계산 (경계일 포함, 30일 초과분 제외)
lbl = b.trend_labels("소세포폐암 신약 급여 적용 …간병비 급증, CAR-T 치료제")
if not {"암", "신약", "급여 적용", "간병", "CAR-T"} <= set(lbl) or "치매·요양" in lbl:
    fails.append(f"키워드: 라벨 매칭 오류 {lbl}")
from datetime import date
hist = {"2026-09-21": ["신약 급여 적용"], "2026-08-23": ["간병비 폭증"], "2026-08-22": ["간병 지옥", "간병 파산"]}
top = b.top_keywords(hist, date(2026, 9, 21), days=30, n=5)
if ("간병", 1) not in top or any(k == "간병" and c > 1 for k, c in top):
    fails.append(f"키워드: 30일 창 경계 오류(8/23은 포함, 8/22는 제외) {top}")
if top[0] != ("간병", 1) or top[1][0] != "급여 적용":
    fails.append(f"키워드: 정렬(건수 내림차순, 동률은 가나다순) 오류 {top}")
if b.build_trend_html([]) != "" or "<b>1</b>암<em>50</em>" not in b.build_trend_html([("암", 50)]):
    fails.append("키워드: 티커 HTML 오류")

# 8. 금감원 공식 자료와 같은 사건의 뉴스 기사 판별
b.FSS_TITLES[:] = ["무료 강연, 박람회에서 보험 가입 시 소비자 유의사항"]
SAME = ["무료 강연 뒤 보험 청약…금감원, 암행점검 강화",
        "“케이크 무료 강연이라더니 종신보험 권유”…금감원, 불건전 보험영업 경고",
        "박람회서 보험 가입했다면…금감원 “무료 강연 영업 주의”"]
DIFFERENT = ["실손보험 청구 간소화 시행…금감원 소비자 유의사항 안내",   # 일반어만 겹침
             "무료 진료 봉사 나선 병원",                                  # 고유어 1개만 겹침
             "박람회 참가 보험사 늘었다",                                 # 고유어 1개만 겹침
             "폐암 신약 급여 적용 논의"]
for t in SAME:
    if not b.fss_overlap(t):
        fails.append(f"금감원 중복: 같은 사건인데 통과 {t}")
for t in DIFFERENT:
    if b.fss_overlap(t):
        fails.append(f"금감원 중복: 다른 기사가 제외됨 {t}")
b.FSS_TITLES[:] = ["금감원, 실손보험 유의사항 안내"]  # 고유어가 1개뿐이면 오탐 위험 -> 판별하지 않음
if b.fss_overlap("실손보험 청구 급증, 금감원 대책"):
    fails.append("금감원 중복: 고유어 1개짜리 공식 자료로 뉴스를 제외함")
b.FSS_TITLES.clear()

# 9. 타사·타업권 상품 홍보 기사는 홍보로 판별(=최종 제외), 정책·삼성화재·일반 기사는 오탐 없음
def promo(t):
    with contextlib.redirect_stdout(io.StringIO()):
        return b.evaluate_article_cot(t, "", hook=False)["is_promo"]

for t in ["“보험료 0원으로 내 자산 지킨다”…수협, Sh디지털안심공제 출시",
          "흥국화재, 주행거리 할인 특약 개편···최대 48% 상향",
          "NH농협손보, 암 진단비 3천만원 신규 암보험 출시…가입 이벤트",
          "OO저축은행, 실손 연계 신상품 출시",
          "[이달의 신상품] 삼성생명, ‘팩 건강보험 케어’ 출시…최대 67만원 보상",   # 삼성생명은 타사(삼성화재만 자사)
          "[이달의 신상품] 새 암보험 출시…진단비 최대 3천만원"]:               # 대괄호 태그만 있어도 홍보
    if not promo(t):
        fails.append(f"홍보: 타사 상품 홍보인데 통과 {t}")
for t in ["삼성화재, 신규 암보험 출시",                                  # 자사 상품은 홍보 아님
          "실손보험 4세대 개편…금감원 소비자 유의사항 안내",              # 회사명 없는 제도 기사
          "건강보험 산정특례 개편 논의"]:
    if promo(t):
        fails.append(f"홍보: 홍보가 아닌 기사를 홍보로 오판 {t}")
for t in MUST_ADOPT:                                                     # 채택돼야 하는 기사가 홍보로 뒤바뀌지 않았는지
    if promo(t):
        fails.append(f"홍보: 기존 채택 기사가 홍보로 오판됨 {t}")

# 10. 지자체(군·시·구·도) 예산·시설·복지 기사는 CoT 단계에서 차단 (수집 단계와 같은 목록을 공유)
for t in ["[단독]단양군립노인요양병원, 새 수탁업체 한 달 만에 간병비 22% 인상…군민 부담 키우고 군비로 메우나",
          "순천시, 치매 어르신 간병비 지원 확대",
          "경기도, 요양병원 간병비 지원 시범사업 확대…도민 부담 줄인다",
          "제주도 도민 대상 암 치료비 지원 확대",
          "충남도립 요양병원 간병비 인상…도비 투입 논란"]:
    if adopted(t):
        fails.append(f"지자체: 지자체 기사가 통과 {t}")
    # "○○시/군/구" 이름만 있는 기사는 CoT 정규식이 막고, 수집 단계 함수는 키워드·도 이름을 본다 (역할 분담)
    if not t.startswith("순천시") and not b.is_local_gov_news(t):
        fails.append(f"지자체: 수집 단계 판별이 놓침 {t}")
for t in ["실손보험 4세대 전환 앞두고 경기도 소재 병원 비급여 실태 점검",   # 도 이름만 있고 지원 표현 없음
          "간병비 부담에 가입 늘어…삼성화재 간병인 특약 출시"]:
    if b.is_local_gov_news(t):
        fails.append(f"지자체: 일반 기사를 지자체 기사로 오판 {t}")

if fails:
    print("\n".join(["[FAIL] " + f for f in fails]))
    sys.exit(1)
print(f"[OK] 채택 {len(MUST_ADOPT)}건 · 차단 {len(MUST_REJECT)}건 · 화법/유튜브/Gemini 재시도/금감원/공식자료 중복/타사 홍보/지자체/빈출 키워드 검증 통과")
