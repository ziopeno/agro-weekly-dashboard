"""
Agro Weekly 자동 업데이트 스크립트
- Gemini AI (gemini-2.0-flash) + Google Search Grounding으로 지난 주 농업화학 뉴스 수집
- 수집된 뉴스를 index.html의 newsDatabase에 자동 삽입
- GitHub Actions에서 매일 08:00 KST에 실행
"""

import os
import re
import json
import sys
import time 
import anthropic
from datetime import datetime, timedelta

# ── 설정 ──────────────────────────────────────────────────────────────────
HTML_FILE = "index.html"            # 업데이트할 HTML 파일 경로
RESULT_FILE = "update_result.txt"  # GitHub Actions Step Summary용 결과 파일
MODEL='claude-sonnet-4-6'
MAX_TOKENS = 8000
TARGET_ARTICLE_COUNT = 15          # 수집할 기사 수

TAG_MAP = {"등록": "reg", "개발": "dev", "영업": "sales", "기획": "plan"}


def get_previous_week_range() -> tuple[str, str]:
    target_env = os.environ.get("TARGET_DATE", "").strip()
    if target_env:
        try:
            ref = datetime.strptime(target_env, "%Y-%m-%d")
        except ValueError:
            ref = datetime.utcnow() + timedelta(hours=9)
    else:
        ref = datetime.utcnow() + timedelta(hours=9)

    # 지난 주 월요일~일요일
    this_monday = ref - timedelta(days=ref.weekday())
    last_monday = this_monday - timedelta(days=7)
    last_sunday = last_monday + timedelta(days=6)

    return last_monday.strftime("%Y-%m-%d"), last_sunday.strftime("%Y-%m-%d")


def get_date_key(end_date: str) -> str:
    target_env = os.environ.get("TARGET_DATE", "").strip()
    if target_env:
        return target_env  # 수동 입력 날짜를 키로 사용
    # 자동 실행 시 KST 오늘 날짜 (cron이 월요일 08:00 KST에 실행되므로 월요일 날짜가 키)
    now_kst = datetime.utcnow() + timedelta(hours=9)
    return now_kst.strftime("%Y-%m-%d")


def build_prompt(start_date: str, end_date: str) -> str:
    return f"""당신은 글로벌 농업화학(Agro-Chemical) 업계 전문 뉴스 큐레이터입니다.

{start_date}부터 {end_date}까지 발생한 주요 글로벌 농업화학 뉴스를 웹에서 검색하고,
가장 중요한 {TARGET_ARTICLE_COUNT}건을 아래 JSON 배열 형식으로만 응답하세요.

⚠️ 중요 지침:
1. 실제로 웹에서 검색하여 실존하는 기사만 포함하세요.
2. "link" 필드는 반드시 해당 기사의 직접 URL (기사 permalink)을 넣으세요.
   - 홈페이지 주소 금지 (예: https://www.reuters.com 이런 형태 금지)
   - 구글 검색 URL 절대 금지 (예: https://www.google.com/search?q=... 금지)
   - 직접 URL을 찾을 수 없는 기사는 목록에서 제외하세요.
3. 기사를 못 찾으면 그 항목을 제외하세요.
4. JSON 외 다른 텍스트, 마크다운 코드블록 없이 순수 JSON 배열만 출력하세요.

검색 키워드 예시: "agrochemical news {start_date}", "pesticide regulation {end_date}",
"crop protection industry", "Bayer Syngenta BASF Corteva FMC news", "agrow weekly"

JSON 형식:
[
  {{
    "tag": "reg | dev | sales | plan 중 하나",
    "dept": "등록 | 개발 | 영업 | 기획 중 하나",
    "title": "번호. 기사 핵심 제목 (한국어, 50자 이내)",
    "body": [
      "핵심 내용 요약 1 (한국어, 한 문장)",
      "핵심 내용 요약 2 (한국어, 한 문장)"
    ],
    "source": "출처 매체명 (영문 원문 그대로)",
    "link": "기사 직접 URL (예: https://www.agropages.com/news/detail-12345.htm)"
  }}
]

tag 분류 기준:
- reg: 농약 등록/승인/규제/법률/MRL
- dev: 신기술/신제품/R&D/M&A/설비투자
- sales: 기업 실적/매출/투자유치/IPO
- plan: 시장 동향/정책/무역/환경/작물 재배면적

반드시 다양한 지역(북미, 유럽, 아시아, 중남미)과 카테고리(reg/dev/sales/plan)를 균형 있게 포함하세요.
"""
def call_claude_with_search(prompt: str) -> list[dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")

    client = anthropic.Anthropic(api_key=api_key)

    print("🔍 Claude AI 웹 검색 시작...")
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}]
    )

    full_text = " ".join(
        block.text for block in response.content if block.type == "text"
    )

    print(f"📝 응답 수신 완료 (토큰: input={response.usage.input_tokens}, output={response.usage.output_tokens})")

    json_text = re.sub(r"```json\s*|```\s*", "", full_text.strip()).strip()

    try:
        articles = json.loads(json_text)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", json_text)
        if match:
            articles = json.loads(match.group(0))
        else:
            raise ValueError(f"JSON 파싱 실패. 응답 원문:\n{full_text[:500]}")

    if not isinstance(articles, list):
        raise ValueError("응답이 JSON 배열 형식이 아닙니다.")

    return articles


def validate_and_clean(articles: list[dict]) -> list[dict]:
    """기사 데이터 유효성 검사 및 정제"""
    cleaned = []
    for i, article in enumerate(articles, 1):
        # 필수 필드 확인
        if not all(k in article for k in ["tag", "dept", "title", "body", "source", "link"]):
            print(f"  ⚠️  기사 #{i} 필수 필드 누락, 건너뜀")
            continue

        # tag 값 정규화
        tag = article["tag"].strip().lower()
        if tag not in TAG_MAP.values():
            # dept로 역추정
            dept_to_tag = {v: k for k, v in TAG_MAP.items()}
            tag = dept_to_tag.get(article["dept"], "plan")
        article["tag"] = tag

        # 제목에 번호 추가 (없으면)
        title = article["title"].strip()
        if not re.match(r"^\d+\.", title):
            title = f"{len(cleaned)+1}. {title}"
        article["title"] = title

        # body가 리스트인지 확인
        if isinstance(article["body"], str):
            article["body"] = [article["body"]]
        article["body"] = [b.strip() for b in article["body"] if b.strip()]

        # link 기본값
        link = article.get("link", "").strip()
        # 구글 검색 URL / 홈페이지 주소 / 빈값 거부
        is_google_search = "google.com/search" in link or link.startswith("https://www.google.com")
        is_empty = not link or link in ["", "#", "N/A"]
        is_homepage = link.rstrip("/") in [
            "https://www.agropages.com",
            "https://www.agrow.com",
            "https://www.reuters.com",
            "https://www.bloomberg.com",
        ]
        article["link"] = "#" if (is_google_search or is_empty or is_homepage) else link

        cleaned.append(article)

    return cleaned

def inject_into_html(articles: list[dict], date_key: str) -> bool:
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    marker = "const newsDatabase = {"
    if marker not in html:
        raise ValueError(f"HTML에서 '{marker}'를 찾을 수 없습니다.")

    if f'"{date_key}"' in html:
        # 기존 주 키가 있으면 배열 끝에 추가
        new_items = ",\n        ".join(
            json.dumps(a, ensure_ascii=False) for a in articles
        )
        # 해당 주 배열의 마지막 } 앞에 삽입
        pattern = rf'("{date_key}":\s*\[)([\s\S]*?)(\])'
        def append_articles(m):
            existing = m.group(2).rstrip()
            separator = ",\n        " if existing.strip() else "\n        "
            return m.group(1) + existing + separator + new_items + "\n    " + m.group(3)
        html_updated = re.sub(pattern, append_articles, html, count=1)
        print(f"📎 {date_key} 기존 항목에 {len(articles)}건 추가")
    else:
        # 해당 주 키가 없으면 새로 생성
        new_entry = json.dumps(articles, ensure_ascii=False, indent=4)
        new_entry = "\n".join("    " + line if line.strip() else line for line in new_entry.splitlines())
        new_block = f'    "{date_key}": {new_entry},\n'
        html_updated = html.replace(marker, marker + "\n" + new_block, 1)
        print(f"🆕 {date_key} 신규 항목 생성")

    # 타임스탬프 업데이트
    now_kst = (datetime.utcnow() + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
    html_updated = re.sub(r"마지막 업데이트: [\d\-]+ [\d:]+", f"마지막 업데이트: {now_kst}", html_updated)

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html_updated)

    return True

def write_result_summary(date_key: str, articles: list[dict], success: bool):
    """GitHub Actions Step Summary용 결과 파일 작성"""
    lines = []
    if success:
        lines.append(f"| 항목 | 내용 |")
        lines.append(f"|------|------|")
        lines.append(f"| 날짜 키 | `{date_key}` |")
        lines.append(f"| 수집 기사 수 | **{len(articles)}건** |")
        tag_counts = {}
        for a in articles:
            tag_counts[a["tag"]] = tag_counts.get(a["tag"], 0) + 1
        for tag, cnt in tag_counts.items():
            dept = {"reg": "등록", "dev": "개발", "sales": "영업", "plan": "기획"}.get(tag, tag)
            lines.append(f"| {dept} | {cnt}건 |")
        lines.append("")
        lines.append("### 수집된 기사 목록")
        for a in articles:
            dept_emoji = {"reg": "📋", "dev": "🔬", "sales": "💰", "plan": "🌍"}.get(a["tag"], "📌")
            lines.append(f"- {dept_emoji} {a['title']}")
    else:
        lines.append("⚠️ 이번 주 데이터가 이미 존재하거나 업데이트할 내용이 없습니다.")

    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    print("=" * 60)
    print("🌾 Agro Weekly 자동 업데이트 시작")
    print("=" * 60)

    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    if dry_run:
        print("🧪 DRY RUN 모드: HTML 파일은 수정하지 않습니다.")

    # 1. 날짜 범위 계산
    start_date, end_date = get_previous_week_range()
    date_key = get_date_key(end_date)
    print(f"📅 수집 기간: {start_date} ~ {end_date}")
    print(f"🔑 데이터 키: {date_key}")

    # 2. 이미 데이터가 있는지 미리 확인
    if not dry_run and os.path.exists(HTML_FILE):
        with open(HTML_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        if f'"{date_key}"' in content:
            print(f"ℹ️  {date_key} 데이터가 이미 존재합니다. 종료합니다.")
            write_result_summary(date_key, [], False)
            sys.exit(0)

    # 3. Gemini API 호출
    prompt = build_prompt(start_date, end_date)
    try:
        raw_articles = call_claude_with_search(prompt)
        print(f"✅ {len(raw_articles)}건 수집 완료")
    except Exception as e:
        print(f"❌ Claude API 호출 실패: {e}")
        sys.exit(1)

    # 4. 데이터 정제
    articles = validate_and_clean(raw_articles)
    print(f"✅ 유효 기사: {len(articles)}건")
    for a in articles:
        print(f"  - [{a['tag']}] {a['title'][:60]}...")

    # 5. HTML 주입 (dry run이 아닐 때만)
    if dry_run:
        print("\n🧪 DRY RUN: 결과 미리보기")
        print(json.dumps(articles, ensure_ascii=False, indent=2)[:1000])
        print("... (dry run 모드로 HTML 수정 생략)")
        write_result_summary(date_key, articles, True)
        sys.exit(0)

    if not os.path.exists(HTML_FILE):
        print(f"❌ {HTML_FILE} 파일을 찾을 수 없습니다.")
        sys.exit(1)

    try:
        injected = inject_into_html(articles, date_key)
        if injected:
            print(f"\n✅ {HTML_FILE}에 {len(articles)}건 성공적으로 추가되었습니다!")
            write_result_summary(date_key, articles, True)
        else:
            write_result_summary(date_key, [], False)
    except Exception as e:
        print(f"❌ HTML 주입 실패: {e}")
        sys.exit(1)

    print("=" * 60)
    print("🎉 Agro Weekly 업데이트 완료!")
    print("=" * 60)


if __name__ == "__main__":
    main()
