# 입문 데모와 마케팅 원고

모든 예제는 합성 시나리오입니다. 고객 사례, 생산성 향상 측정치,
실제 Claude/Codex 실행 증거로 소개하지 마세요. STM은 이번 데모에 필요 없습니다.

## 코딩 에이전트: 90초 화면 녹화

1. 0–15초: 문제 제시 — “지난 세션에서 재시도 횟수를 왜 제한했을까요?”
   [샘플 README](retry-policy/README.md)의 격리 설치를 먼저 완료합니다.
2. 15–40초: `python examples/onboarding/retry-policy/demo.py` 실행.
   빈 저장소 → 결정 저장 → 새 CLI 프로세스에서 이유 검색을 설명합니다.
   실제 명령 출력만 보여 주세요. 편집으로 만든 출력을 실행 결과처럼 쓰지 않습니다.
3. 40–65초: ADR 원문을 열고 이전 클라이언트 호환성과 롤백 플래그를 짚습니다.
   검색 결과만 믿지 않고 원문을 확인하는 흐름을 강조합니다.
4. 65–90초: PASS 4개와 임시 저장소 정리를 보여 주고 샘플 링크로 안내합니다.
   “이것은 CLI 검증입니다. AI 세션 데모는 클라이언트 연결 후 별도로 진행합니다.”

실제 클라이언트 녹화는 README의 저장/검색 프롬프트를 사용합니다.
새 대화를 열고 도구 호출·근거 경로를 화면에서 확인한 경우에만
‘새 AI 세션에서 확인’이라고 설명합니다. 개인정보나 키는 촬영하지 않습니다.

## LangGraph: 2분 화면 녹화

1. 0–20초: 05 노트북의 설치와 임시 저장소 설명. API 키가 필요 없음을 안내합니다.
2. 20–60초: 새 thread의 방문 횟수는 초기화되지만 같은 사용자의 선호는 유지됩니다.
   다른 사용자 namespace는 빈 결과입니다. namespace가 접근 통제는 아니라는 점을 덧붙입니다.
3. 60–100초: 06에서 BM25 검색 → 초안 → 명시적 승인 → Markdown 저장을 보여 줍니다.
   기본 응답은 템플릿이며 LLM 생성이 아닙니다.
4. 100–120초: 각각 PASS 6개와 `SKIP LLM`을 보여 줍니다.
   선택형 Responses API는 유료이며 기본 꺼짐, 생성 초안은 자동 저장하지 않습니다.

## 짧은 소개문 (게시 전 공개 링크 확인)

**KO / 코딩:** “코드는 남았는데 결정의 이유는 사라졌나요?
memtomem 합성 데모로 재시도 정책의 이유와 ADR 출처를 다시 찾아보세요.
로컬 CLI 실습부터 시작하고, 연결한 AI 클라이언트에서 직접 확인하세요.”

**EN / Coding:** “The code survived. Did the reason? Try a synthetic memtomem
demo that retrieves a retry-policy decision and its ADR source. Start with the
local CLI proof, then verify the workflow in your connected AI client.”

**KO / LangGraph:** “대화 상태와 오래 남길 기억은 다릅니다.
API 키 없이 실행하는 한국어 노트북 두 개로 thread 상태, 사용자 기억,
근거 검색과 승인 후 저장을 구분해 보세요.”

**EN / LangGraph:** “Conversation state is not durable memory. Two self-contained
Korean notebooks demonstrate thread state, user memory, source retrieval, and
approved writes—without a model or API key in the required path.”

CTA: https://memtomem.com/use-cases/vibe-coding/ 또는
https://memtomem.com/use-cases/langgraph/ (한국어는 `/ko/` 접두사).
Core 실습 파일 공개 후 웹을 배포하고 링크를 확인하기 전에는 게시하지 않습니다.
