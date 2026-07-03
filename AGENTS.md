# AGENTS.md instructions

사용자는 코딩 도구와 언어에 익숙하지 않으므로 쉽게 설명한다.
전문 용어를 쓸 때는 짧게 의미를 풀어서 설명한다.

## 프로젝트 범위

- 반드시 이 repo에서 작업한다:
  `C:\Users\USER\Documents\PerpDEX-Farming-Bot`
- `PerpDEX-Farming-Bot 2` 또는 다른 복사본에서 작업하지 않는다.
- 이 repo는 PerpDEX farming/trading bot 실행 쪽이다.
- Dashboard/Data Hub는 public market data 중심이며, 이 repo에서는 필요한 경우 read-only 데이터 소스로만 본다.
- Dashboard/Data Hub의 최신 기준은 서버컴과 GitHub에 있을 수 있다. 현재 본컴퓨터의 로컬 Dashboard repo는 outdated일 수 있으므로, 그 내용을 확정된 최신 상태로 가정하지 않는다.
- Cross-exchange 전략은 별도 repo 범위로 보고 섞지 않는다.

## 기본 작업 원칙

- 작업 시작 시 실제 repo 경로와 git 상태를 먼저 확인한다.
- 추측하지 말고 소스, 문서, 설정 파일을 확인한다.
- 필요한 파일만 최소 수정한다.
- 사용자가 요청하지 않으면 commit/push 하지 않는다.
- 사용자가 요청하지 않은 리팩터링이나 구조 변경은 하지 않는다.
- 이미 존재하는 사용자 변경사항은 함부로 되돌리지 않는다.
- Windows PowerShell 기준으로 실행 가능한 명령을 우선 제시한다.
- Python 실행 예시는 보통 `$env:PYTHONPATH="src"`를 포함한다.

## 비밀값과 데이터 보호

- `.env`, API key, private key, public key 원문, signature, session key, Telegram token, 실제 DB 파일은 절대 채팅에 노출하지 않는다.
- 실제 키 값은 repo 파일에 저장하지 않는다.
- 실제 키 값은 로컬 `.env` 또는 OS 환경변수에만 둔다.
- `.env.example`에는 빈 placeholder만 둔다.
- `data/`, `logs/`, 실제 SQLite DB는 커밋하지 않는다.
- 민감값 점검이 필요하면 값 자체가 아니라 `present`/`missing`처럼 존재 여부만 말한다.

## 거래 안전 경계

- 실제 주문, 주문 취소, 포지션 변경은 사용자의 명시 승인 전까지 금지한다.
- 기본 진행 순서는 다음과 같다:
  1. env check
  2. public read-only
  3. private read-only
  4. paper-only
  5. dry-run
  6. 명시 승인 후 초소액 live test
- paper-only와 live trading 경계를 흐리지 않는다.
- live 주문 경로는 항상 별도 CLI confirmation, volume cap, kill switch, 로그 확인을 요구한다.
- Farming Bot은 주문 직전 최신 orderbook/depth를 다시 확인해야 한다.

## 거래소 연동 원칙

- Hibachi 외 거래소를 추가할 때도 기존 안전 단계와 config 구조를 우선 재사용한다.
- 거래소별 API 인증, market naming, balance, position, order model은 추측하지 말고 공식 문서나 실제 SDK 코드로 확인한다.
- 최신 API 정보가 필요한 거래소는 반드시 공식 문서 또는 최신 자료를 확인한다.
- 새 거래소는 기존 Hibachi 코드를 망가뜨리지 않도록 전용 connector/config/CLI로 분리한다.
- 공통화는 실제 중복이 확인된 뒤에만 한다.

## Hibachi 관련 원칙

- Hibachi는 Crypto 지갑/API와 FX 지갑/API가 분리될 수 있음을 전제로 한다.
- FX phase와 Crypto phase의 주간 목표 거래량은 분리해서 본다.
- 여러 마켓이 켜져 있으면 마켓별 목표가 아니라 phase 전체 목표 안에서 spread 조건이 맞는 마켓을 선택한다.
- Spread gate는 기본적으로 다음 두 조건을 모두 만족해야 한다:
  - 현재 spread가 평균 spread 이하
  - 현재 spread가 hard threshold 이하
- Dashboard/Data Hub 평균 spread를 사용할 때도 DB는 read-only로만 연다.
- live ledger의 거래량은 로컬 기록 기준이므로, 거래소 공식 volume/point와 다를 수 있음을 설명한다.

## Telegram Bot 원칙

- Telegram token/chat id는 `.env` 또는 환경변수에만 둔다.
- Telegram 명령은 기본적으로 상태 조회와 안전 제어 중심으로 둔다.
- on/off, pause/resume, status, balance, volume 같은 기능은 실제 주문 실행과 분리한다.
- Telegram에서 live trading을 켜는 기능은 반드시 별도 안전장치와 명시 승인을 요구한다.

## 검증 원칙

- 코드 변경 후 가능한 범위에서 검증한다.
- 기본 Python 검증:
  ```powershell
  $env:PYTHONPATH="src"
  python -m compileall src
  ```
- 관련 CLI가 있으면 read-only 또는 no-record 모드로 우선 확인한다.
- 네트워크/API 호출이 필요한 검증은 실제 주문이 없는 read-only인지 먼저 확인한다.
- 검증하지 못한 항목은 숨기지 말고 이유를 말한다.

## 리뷰 모드

사용자가 "리뷰해줘", "리뷰 모드", "code-autopsy", "푸시 전 점검"이라고 하면 코드 리뷰 모드로 답한다.

리뷰 형식:

- 칭찬 요약보다 finding을 먼저 말한다.
- 모든 문제는 가능한 한 `file:line`을 포함한다.
- severity를 붙인다: `CRITICAL`, `HIGH`, `MEDIUM`, `LOW`.
- 문제 근거를 코드 기준으로 설명한다.
- 가능하면 수정 diff 또는 구체적 수정 방향을 제안한다.
- 보안, 안정성, 유지보수성, 운영성을 나눠 본다.
- 최종 판정은 `SHIP`, `FIX FIRST`, `RISKY`, `BLOCK` 중 하나로 한다.

## 답변 방식

- 사용자가 이해하기 쉽게 현재 상태, 바꾼 것, 검증한 것, 다음 행동을 짧게 정리한다.
- 명령어는 복사해서 쓸 수 있게 코드블록으로 제공한다.
- 실제 값이 들어갈 자리는 placeholder로 표시한다.
- 불확실한 내용은 확정처럼 말하지 말고 확인 방법을 같이 제시한다.
