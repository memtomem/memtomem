# MCP 클라이언트별 설정 가이드

memtomem MCP 서버를 4개 주요 AI 코딩 도구에 연결하는 방법.
[standalone-test-guide.md](standalone-test-guide.md)의 설치/인덱싱 완료 후 이 가이드를 따른다.

> 모든 가이드에서 memtomem 소스 경로는 `/tmp/memtomem-test/memtomem`으로 가정한다.

---

## 1. Claude Code

### MCP 설정 구조

| 범위 | 설정 파일 | 설명 |
|------|----------|------|
| 사용자 전역 | `~/.claude.json` | 모든 프로젝트에서 사용 가능 |
| 프로젝트 | `.mcp.json` (프로젝트 루트) | 이 프로젝트에서만 사용 |

### 1-1. MCP 서버 등록 (CLI)

```bash
# 프로젝트 스코프로 등록 (권장 — 테스트 격리)
claude mcp add memtomem-test -s project -- \
  uv run --directory /tmp/memtomem-test/memtomem memtomem-server

# 또는 사용자 전역으로 등록
claude mcp add memtomem -s user -- \
  uv run --directory /tmp/memtomem-test/memtomem memtomem-server
```

### 1-2. 수동 설정 (`.mcp.json`)

프로젝트 루트에 `.mcp.json` 생성:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uv",
      "args": ["run", "--directory", "/tmp/memtomem-test/memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_CONTEXT_WINDOW__ENABLED": "true",
        "MEMTOMEM_CONTEXT_WINDOW__WINDOW_SIZE": "2"
      }
    }
  }
}
```

### 1-3. 연결 확인

Claude Code 대화창에서:

```
"mem_status 호출해줘"
```

기대 출력:
```
DB: ~/.memtomem/memtomem.db
Embedding: ollama / bge-m3 (1024d)
Chunks: 8
Namespaces: default (8)
```

### 1-4. STM 프록시 설정

```bash
uv run mm stm init
```

위자드 Step 1에서 **Claude Code** 자동 감지됨.
프록시 설정 완료 후 Claude Code 재시작.

확인:
```
"stm_proxy_stats() 호출해줘"
```

### 1-5. 도구 호출 테스트

```
# 기본 검색
"캐싱 전략 관련 메모리 검색해줘"
→ mem_search("캐싱 전략")

# 컨텍스트 확장
"첫 번째 결과 주변 맥락 보여줘"
→ mem_do(action="expand", params={"chunk_id": "...", "window": 2})

# 메모리 추가
"API 게이트웨이를 Kong으로 선정했다고 기록해줘"
→ mem_add(content="...")

# mem_do 액션 (61개)
"사용 가능한 액션 목록 보여줘"
→ mem_do(action="help")
```

### 1-6. 정리

```bash
claude mcp remove memtomem-test   # 프로젝트 스코프 제거
# 또는
claude mcp remove memtomem        # 사용자 전역 제거
```

---

## 2. Gemini CLI

### MCP 설정 구조

| 범위 | 설정 파일 | 설명 |
|------|----------|------|
| 사용자 전역 | `~/.gemini/settings.json` | 모든 프로젝트에서 사용 가능 |
| 프로젝트 | `.gemini/settings.json` (프로젝트 루트) | 이 프로젝트에서만 사용 |

### 2-1. MCP 서버 등록 (CLI)

```bash
gemini mcp add memtomem -- \
  uv run --directory /tmp/memtomem-test/memtomem memtomem-server
```

### 2-2. 수동 설정 (`~/.gemini/settings.json`)

파일이 없으면 새로 생성. 기존 파일이 있으면 `mcpServers`에 추가:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uv",
      "args": ["run", "--directory", "/tmp/memtomem-test/memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_CONTEXT_WINDOW__ENABLED": "true",
        "MEMTOMEM_CONTEXT_WINDOW__WINDOW_SIZE": "2"
      },
      "timeout": 30000,
      "trust": true
    }
  }
}
```

**Gemini CLI 전용 필드:**

| 필드 | 기본값 | 설명 |
|------|--------|------|
| `timeout` | `30000` | 서버 응답 대기 시간 (ms) |
| `trust` | `false` | `true`면 도구 실행 시 확인 없이 자동 승인 |
| `cwd` | — | 서버 실행 작업 디렉토리 |
| `includeTools` | — | 허용할 도구 이름 목록 (화이트리스트) |
| `excludeTools` | — | 제외할 도구 이름 목록 (블랙리스트) |

**환경변수 문법:**
- POSIX: `$VARIABLE_NAME` 또는 `${VARIABLE_NAME}`
- Windows: `%VARIABLE_NAME%`
- `*TOKEN*`, `*SECRET*`, `*KEY*` 등 민감 패턴은 자동 마스킹됨

### 2-3. 프로젝트 스코프 설정

프로젝트 루트에 `.gemini/settings.json` 생성:

```bash
mkdir -p .gemini
cat > .gemini/settings.json << 'EOF'
{
  "mcpServers": {
    "memtomem": {
      "command": "uv",
      "args": ["run", "--directory", "/tmp/memtomem-test/memtomem", "memtomem-server"],
      "trust": true
    }
  }
}
EOF
```

### 2-4. 연결 확인

```bash
gemini
```

Gemini CLI 세션에서:

```
@memtomem mem_status 호출해줘
```

또는 도구를 직접 호출:

```
mem_status 실행해줘
```

### 2-5. 도구 호출 테스트

```
# 검색
"캐싱 전략 관련 메모리 검색해줘"

# 컨텍스트 확장 검색
"쿠버네티스 메모리를 주변 맥락 포함해서 검색 (context_window=2)"

# 메모리 추가
"Docker multi-stage 빌드로 3분→45초 최적화했다고 기록"

# mem_expand
"방금 검색 결과의 첫 번째 chunk_id로 expand 해줘 (window=3)"
```

### 2-6. STM 프록시 설정

```bash
uv run mm stm init
```

Step 1에서 Gemini CLI가 자동 감지되지 않을 수 있다. 이 경우:

1. 위자드에서 `.mcp.json` 옵션 선택
2. 생성된 설정을 `~/.gemini/settings.json`에 수동 복사
3. 기존 `mcpServers`와 병합

**수동 STM 설정 (`~/.gemini/settings.json`):**

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uv",
      "args": ["run", "--directory", "/tmp/memtomem-test/memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_STM_PROXY__ENABLED": "true",
        "MEMTOMEM_STM_PROXY__CONFIG_PATH": "~/.memtomem/stm_proxy.json"
      },
      "trust": true
    }
  }
}
```

> STM이 활성화되면 프록시된 도구(예: `fs__read_file`)가 memtomem 서버를 통해 제공된다. 원본 서버 항목은 제거하거나, 이름이 겹치지 않게 조정해야 한다.

### 2-7. 정리

```bash
# CLI로 제거
gemini mcp remove memtomem

# 또는 수동 제거
# ~/.gemini/settings.json에서 memtomem 항목 삭제
```

---

## 3. Google Antigravity

### MCP 설정 구조

| 범위 | 설정 파일 | 접근 방법 |
|------|----------|----------|
| 사용자 전역 | `mcp_config.json` | GUI: Agent → `...` → MCP Servers → Manage → View raw config |
| 프로젝트 | `.antigravity/mcp.json` | 프로젝트 루트에 수동 생성 |

> Antigravity는 CLI 명령 없이 GUI에서 MCP 서버를 관리한다.

### 3-1. GUI로 MCP 서버 추가

1. Antigravity에서 Agent 세션 열기
2. 상단 `...` (Additional options) 클릭
3. **MCP Servers** 선택
4. **Manage MCP Servers** 클릭
5. **View raw config** 클릭 → `mcp_config.json` 편집기 열림

### 3-2. 설정 작성 (`mcp_config.json`)

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uv",
      "args": ["run", "--directory", "/tmp/memtomem-test/memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_CONTEXT_WINDOW__ENABLED": "true",
        "MEMTOMEM_CONTEXT_WINDOW__WINDOW_SIZE": "2"
      }
    }
  }
}
```

저장 후 **Manage MCP Servers** 화면을 새로고침하면 memtomem이 활성 서버 목록에 나타난다.

### 3-3. 프로젝트 스코프 설정

프로젝트 루트에 `.antigravity/mcp.json` 생성:

```bash
mkdir -p .antigravity
cat > .antigravity/mcp.json << 'EOF'
{
  "mcpServers": {
    "memtomem": {
      "command": "uv",
      "args": ["run", "--directory", "/tmp/memtomem-test/memtomem", "memtomem-server"]
    }
  }
}
EOF
```

> 여러 프로젝트에서 동시에 사용할 때는 포트 충돌 방지를 위해 프로젝트별 설정을 권장.

### 3-4. 연결 확인

Agent 세션에서:

```
mem_status 호출해줘
```

MCP Servers 패널에서 memtomem 서버가 "Connected" 상태인지 확인.

### 3-5. 도구 호출 테스트

```
# 검색
"캐싱 전략 관련 메모리 검색해줘"

# 컨텍스트 확장
"첫 번째 결과의 주변 맥락을 보고 싶어"

# 메모리 추가
"Redis 마이그레이션 계획 완료했다고 기록해줘"

# mem_do 액션
"사용 가능한 액션 목록 보여줘"
```

### 3-6. STM 프록시 설정

```bash
uv run mm stm init
```

Antigravity는 `mm stm init`에서 자동 감지되지 않는다. 수동 설정:

1. 위자드에서 프록시할 서버 선택 + 설정 저장
2. `~/.memtomem/stm_proxy.json` 생성됨
3. Antigravity의 `mcp_config.json`을 수동 수정:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uv",
      "args": ["run", "--directory", "/tmp/memtomem-test/memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_STM_PROXY__ENABLED": "true",
        "MEMTOMEM_STM_PROXY__CONFIG_PATH": "~/.memtomem/stm_proxy.json"
      }
    }
  }
}
```

4. 프록시되는 원본 서버(예: filesystem)는 `mcp_config.json`에서 제거
5. MCP Servers 패널에서 새로고침

### 3-7. 정리

1. **Manage MCP Servers** → **View raw config** 열기
2. memtomem 항목 삭제
3. 저장 후 새로고침

---

## 4. Cursor

### MCP 설정 구조

| 범위 | 설정 파일 | 접근 방법 |
|------|----------|----------|
| 사용자 전역 | `~/.cursor/mcp.json` | 직접 편집 또는 Settings → Features → MCP |
| 프로젝트 | `.cursor/mcp.json` (프로젝트 루트) | 직접 편집 |

### 4-1. GUI로 MCP 서버 추가

1. Cursor에서 `Cmd+,` (Settings) 열기
2. **Features** → **MCP** 섹션 찾기
3. **+ Add new MCP Server** 클릭
4. 이름: `memtomem`
5. Type: **command** (stdio)
6. Command: `uv run --directory /tmp/memtomem-test/memtomem memtomem-server`

### 4-2. 수동 설정 — 전역 (`~/.cursor/mcp.json`)

파일이 없으면 새로 생성:

```bash
mkdir -p ~/.cursor
cat > ~/.cursor/mcp.json << 'EOF'
{
  "mcpServers": {
    "memtomem": {
      "command": "uv",
      "args": ["run", "--directory", "/tmp/memtomem-test/memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_CONTEXT_WINDOW__ENABLED": "true",
        "MEMTOMEM_CONTEXT_WINDOW__WINDOW_SIZE": "2"
      }
    }
  }
}
EOF
```

### 4-3. 수동 설정 — 프로젝트 (`.cursor/mcp.json`)

프로젝트 루트에:

```bash
mkdir -p .cursor
cat > .cursor/mcp.json << 'EOF'
{
  "mcpServers": {
    "memtomem": {
      "command": "uv",
      "args": ["run", "--directory", "/tmp/memtomem-test/memtomem", "memtomem-server"]
    }
  }
}
EOF
```

> 프로젝트 스코프가 전역보다 우선한다. 같은 이름의 서버가 양쪽에 있으면 프로젝트 설정이 적용됨.

### 4-4. 연결 확인

1. Cursor 재시작 (MCP 서버는 재시작 시 로드됨)
2. Settings → Features → MCP에서 memtomem 서버 상태 확인
3. 도구 목록에 `mem_search`, `mem_add`, `mem_do` 등이 보이면 성공

Cursor 채팅(Agent 모드)에서:

```
mem_status 호출해줘
```

> Cursor는 Agent 모드(`Cmd+I` 또는 채팅에서 Agent 선택)에서만 MCP 도구를 호출할 수 있다. 일반 Chat 모드에서는 도구 호출 불가.

### 4-5. 도구 호출 테스트

Cursor Agent 모드에서:

```
# 검색
"캐싱 전략 관련 메모리 검색해줘"

# 컨텍스트 확장
"첫 번째 결과를 expand 해줘 (window=2)"

# 메모리 추가
"API 게이트웨이를 Kong으로 선정했다고 기록"

# mem_do
"태그 목록 보여줘"
```

### 4-6. STM 프록시 설정

```bash
uv run mm stm init
```

Step 1에서 **Cursor** 자동 감지됨 (`~/.cursor/mcp.json` 읽기).
프록시 설정 완료 후:

1. Cursor 재시작
2. Settings → Features → MCP에서 프록시된 도구 확인
3. 원본 서버가 제거되고 프록시된 도구(예: `fs__read_file`)가 memtomem을 통해 제공됨

**수동 STM 설정이 필요한 경우:**

`~/.cursor/mcp.json` 수정:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uv",
      "args": ["run", "--directory", "/tmp/memtomem-test/memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_STM_PROXY__ENABLED": "true",
        "MEMTOMEM_STM_PROXY__CONFIG_PATH": "~/.memtomem/stm_proxy.json"
      }
    }
  }
}
```

### 4-7. 정리

```bash
# 전역 설정에서 제거
# ~/.cursor/mcp.json에서 memtomem 항목 삭제

# 프로젝트 설정 제거
rm .cursor/mcp.json
```

또는 Settings → Features → MCP에서 memtomem 서버 삭제.

---

## 클라이언트별 비교 요약

| | Claude Code | Gemini CLI | Antigravity | Cursor |
|--|-------------|-----------|-------------|--------|
| **설정 파일** | `~/.claude.json` | `~/.gemini/settings.json` | `mcp_config.json` (GUI) | `~/.cursor/mcp.json` |
| **프로젝트 스코프** | `.mcp.json` | `.gemini/settings.json` | `.antigravity/mcp.json` | `.cursor/mcp.json` |
| **CLI 등록** | `claude mcp add` | `gemini mcp add` | GUI만 | GUI 또는 수동 |
| **JSON 포맷** | `{mcpServers: {...}}` | `{mcpServers: {...}}` | `{mcpServers: {...}}` | `{mcpServers: {...}}` |
| **env 지원** | O | O (`$VAR` 문법) | O | O |
| **추가 필드** | — | `timeout`, `trust`, `includeTools`, `excludeTools`, `cwd` | — | — |
| **STM 자동 감지** | O | X (수동 복사) | X (수동 복사) | O |
| **도구 호출 모드** | 대화 자동 | `@server` 또는 자동 | Agent 세션 | Agent 모드 필수 |
| **재시작 필요** | STM 변경 시 | 설정 변경 시 | 새로고침 | 설정 변경 시 |

---

## 공통 주의사항

### 환경변수 설정

모든 클라이언트에서 `env` 블록으로 memtomem 설정을 전달할 수 있다:

```json
{
  "env": {
    "MEMTOMEM_CONTEXT_WINDOW__ENABLED": "true",
    "MEMTOMEM_CONTEXT_WINDOW__WINDOW_SIZE": "2",
    "MEMTOMEM_SEARCH__DEFAULT_TOP_K": "10",
    "MEMTOMEM_SEARCH__TOKENIZER": "unicode61",
    "MEMTOMEM_STM_PROXY__ENABLED": "true"
  }
}
```

### STM 수동 설정 (자동 감지 안 되는 경우)

1. `uv run mm stm init`으로 `~/.memtomem/stm_proxy.json` 생성
2. 클라이언트 MCP 설정에서 memtomem 서버의 `env`에 추가:
   ```json
   {
     "MEMTOMEM_STM_PROXY__ENABLED": "true",
     "MEMTOMEM_STM_PROXY__CONFIG_PATH": "~/.memtomem/stm_proxy.json"
   }
   ```
3. 프록시되는 원본 서버는 클라이언트 설정에서 제거
4. 클라이언트 재시작

### 도구 모드 (tool mode)

memtomem은 기본적으로 **core 모드** (9개 도구 + `mem_do` 메타 도구)로 동작한다.
모든 도구를 개별 노출하려면:

```json
{
  "env": {
    "MEMTOMEM_TOOL_MODE": "full"
  }
}
```

| 모드 | 도구 수 | 설명 |
|------|--------|------|
| `core` (기본) | 9 + mem_do | 검색, 추가, 인덱싱, 상태 등 핵심 도구. 나머지는 mem_do로 접근 |
| `standard` | ~30 + mem_do | core + 자주 쓰는 팩 (namespace, tags, sessions, scratch 등) |
| `full` | 65+ | 모든 도구 개별 노출. mem_do 불필요 |

> **입문자 권장:** `core` 모드. 도구 수가 적어 에이전트의 도구 선택이 정확하고 토큰 소비가 적다.

Sources:
- [Gemini CLI MCP Server Integration](https://geminicli.com/docs/tools/mcp-server)
- [Google Antigravity MCP Integration](https://medium.com/google-developer-experts/google-antigravity-custom-mcp-server-integration-to-improve-vibe-coding-f92ddbc1c22d)
- [Cursor MCP Docs](https://cursor.com/docs/context/mcp)
- [Antigravity MCP Forum](https://discuss.ai.google.dev/t/support-for-per-workspace-mcp-config-on-antigravity/111952)
