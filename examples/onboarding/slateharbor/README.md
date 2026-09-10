# Slateharbor: 지난 결정을 다시 설명하지 않고 작업 이어가기

**[첫 체험 노트북 열기](../../notebooks/00_start_here.ipynb)**

가상의 협업 SaaS에서 결정 문서·Python 구현·운영 설정을 함께 찾아봅니다.
예시 한 문장을 저장하는 체험을 넘어, **150개 파일과 약 1,000개 실제 청크**에서
필요한 근거를 골라 새 작업에 가져오는 과정입니다. 한국어 설명과 영어 식별자를 섞었습니다.

## 받기와 실행

이 저장소를 내려받거나 유지보수자가 만든 `slateharbor-first-user.zip`을 풀어 사용하세요.
노트북과 `examples/onboarding/slateharbor/`의 상대 경로를 유지해야 합니다.
이 노트북은 파일 하나만 다운로드해서 실행하는 형식이 아닙니다.

묶음 루트에서 Python 3.12 이상으로 실행합니다:

```bash
uv venv .venv
uv pip install --python .venv/bin/python "memtomem[code]>=0.5.0" jupyterlab ipykernel
uv run --python .venv/bin/python --no-project jupyter lab examples/notebooks/00_start_here.ipynb
```

Windows는 `.venv/bin/python` 대신 `.venv/Scripts/python.exe`를 사용합니다.
설치한 가상환경의 Python 커널을 선택한 뒤 **Run All**을 실행하세요.
시작 셀의 Python 경로/버전을 확인하면 커널 설치 혼동을 줄일 수 있습니다.
패키지 설치에는 인터넷이 필요하지만 실습에는 API 키·임베딩 모델·외부 DB가 필요 없습니다.
이 실습은 **0.5.0 이상**에서 동작합니다. ZIP만 가지고 있어도 위 명령 그대로 실행됩니다.
저장소를 받았다면 소스에서 설치해도 같습니다:

```bash
uv pip install --python .venv/bin/python -e 'packages/memtomem[code]' jupyterlab ipykernel
```

실제로 실행해 검증한 버전은 [VALIDATION.md](VALIDATION.md)에 적혀 있습니다. 상한은 두지 않습니다:
`lab.py`는 CLI가 렌더링한 텍스트를 되파싱하지 않고 검색 결과의 `chunk_id`로 색인에서 본문을 직접
읽으므로, 출력 형식이 바뀌어도 영향을 받지 않고 색인 스키마가 바뀌면 조용히 통과하는 대신 오류를 냅니다.

## 어떤 경험을 하나요?

1. **legacy callback을 지워도 될까?** 채택 결정, Python 정책, production JSON을 함께 찾습니다.
2. **재시도는 몇 번이지?** 알림 5회, 결제 webhook 3회, 작업 2회와 폐기된 설정을 구분합니다.
3. **다음 대화에서 어디부터 시작하지?** worker lease 복구 인계를 새 CLI 프로세스에서 찾습니다.
4. **원본을 바꾸면?** 실습 복사본의 JSON을 수정·재색인하고 바뀐 근거를 확인합니다.
5. **내 프로젝트에도?** 결정·이유·검색어 세 값만 바꾼 뒤 실제 AI 도구에 연결합니다.

첫 검색 결과까지 먼저 체험하고 나머지는 추가로 따라갈 수 있습니다.
출력은 실제 검색 결과입니다. LLM 답변이나 코딩 생산성 향상을 측정한 결과가 아닙니다.

## 프로젝트 지도

`project/` 아래에 인증, 결제, 알림, 백그라운드 작업, 파일 관리, 리포트의 6개 영역이 있습니다.
각 영역에는 25개 파일이 있습니다. 6월 pilot, 7월 장애 관찰, 8월 채택 결정과 후속 작업을 연결했습니다.

| 형식 | 파일 | 역할 |
|---|---:|---|
| `.md` | 60 | 결정, 이전 결정, 장애 회고, 운영 절차, 인계, 제안, 계약, 배포, 지원, 변경 기록 |
| `.py` | 42 | 정책 함수, 설정 검증, 판단 핸들러, 관찰 요약, 재현 입력, 요청 모델, 테스트 |
| `.json` | 30 | production·staging·과거 설정, 요청 예시, 관찰 기록 |
| `.yaml` | 12 | canary 점검과 경보 정의 |
| `.toml` | 6 | 로컬 정책 프로파일 |

주요 정책 코드는 표준 라이브러리로 실행되는 작은 구현입니다.
실제 SaaS 서버나 인증·결제 시스템 전체를 구축한 것은 아닙니다.
운영 정의 파일은 읽고 검색하기 위한 합성 자료이며 배포하지 않습니다.

## 검색 단위와 실습 경계

- 자연스러운 정책 제목·함수·설정 키를 각각 검색할 수 있도록 실습에 한해
  `indexing.min_chunk_tokens=0`, `indexing.target_chunk_tokens=0`으로 인접 병합을 끕니다.
  기본 병합 설정의 청크 수와 같다고 주장하지 않습니다. 노트북 표는 실제 SQLite 색인에서 측정합니다.
- 검색은 BM25입니다. 같은 식별어를 찾는 데 적합하며 한국어 질문을 영어 표현으로 자동 번역하지 않습니다.
- `source_filter`는 파일명 부분 일치로 종류를 좁힙니다. 특정 정답 파일 한 개를 몰래 선택하지 않습니다.
  전체 검색에서 단서를 찾은 뒤 결정·구현·production이라는 문서 종류를 명시적으로 확인합니다.
- 0.5.0은 결과 수를 제한한 뒤 출처 필터를 적용합니다. 실측: `--source-filter production.json`에
  `--top-k 1`은 0건, `--top-k 5`는 rank 5로 1건을 돌려줍니다. 그래서 도우미는 출처 조건이 있을 때
  최대 100개 후보를 요청하고 그중 상위 5개(본문 표시는 1~3개)를 보여줍니다.
  검증의 Top-5는 이 명시된 실습 경로의 결과이며 제품 기본 설정 벤치마크가 아닙니다.
- 본문은 검색 결과의 `chunk_id`로 실습용 색인에서 직접 읽습니다. `--format json`의 `content`는
  200자에서 잘리고, `--format context`는 사람이 읽기 위한 표현이라 본문 안에 결과 머리글과
  똑같이 생긴 줄이 들어갈 수 있어 위치로 자르는 것이 안전하지 않기 때문입니다.
- 검색 순위는 현재 정책의 권위를 보장하지 않습니다. `current`, `superseded`, `proposed` 상태와 개정 근거를 읽습니다.
- `lab.py`는 원본을 임시 홈으로 복사하며, 부모 프로세스의 HOME·환경·작업 경로를 바꾸지 않습니다.
  CLI 자식마다 Python socket의 `connect`, `connect_ex`, `sendto`, `create_connection`을 차단합니다.
  이는 해당 API에 대한 검증이며 모든 네이티브 네트워크 경로를 가로채는 운영체제 방화벽은 아닙니다.
- 변경 실습은 복사본만 수정합니다. 마지막 셀은 임시 저장소를 정리합니다.
  실제 AI 도구에서 사용하려면 마지막 안내에 따라 본인이 선택한 저장소에 별도로 색인합니다.

## 데이터 출처와 유지보수

모든 조직·사건·정책은 이 예제를 위해 작성한 합성 데이터입니다. 실제 사용자 기억을 읽어 생성하지 않았습니다.
수치와 정책을 실제 운영 지침으로 사용하지 마세요. 저장소와 같은 Apache-2.0 라이선스로 제공합니다.

- `scenarios.py`: 36개 정책의 이유·장애·복구·후속 작업을 작성한 원본 명세.
- `generate.py`: 파일을 재현 가능하게 생성합니다. 실행 중 LLM을 호출하지 않습니다.
- `manifest.json`: 파일 경로와 SHA-256. 배포 원본의 변경을 확인합니다.
- `evaluation.json`: 결과를 보기 전에 지정한 18개 검색 질의와 기대 출처. **색인 대상 밖**에 있습니다.
- `validate.py`: 구조·참조·정책 동작·실제 검색·재색인·격리를 검증합니다.
- `build_notebook.py`: 검토 가능한 문장과 코드에서 노트북을 재생성합니다.

샘플을 수정하려면 명세를 고친 뒤 재생성합니다. 노트북 코드에서 정답 명세나 평가 파일을 읽지 않습니다.

```bash
python examples/onboarding/slateharbor/generate.py
python examples/onboarding/slateharbor/build_notebook.py
python examples/onboarding/slateharbor/generate.py --check
python examples/onboarding/slateharbor/build_notebook.py --check
python examples/onboarding/slateharbor/validate.py --json
```

검증 명령은 위의 memtomem 환경에서 실행합니다. 새 커널 검증에는 `nbclient ipykernel`을 설치하세요.

```bash
python tools/check_beginner_notebooks.py --notebook 00_start_here.ipynb --json
python tools/build_slateharbor_bundle.py --output /tmp/slateharbor-first-user.zip
```

ZIP에는 노트북, 샘플, 생성·검증 도구, 라이선스만 들어갑니다. 개인 설정·실행 출력·실습 DB는 포함하지 않습니다.
실행 증거와 미검증 항목은 [VALIDATION.md](VALIDATION.md)에 기록합니다.
