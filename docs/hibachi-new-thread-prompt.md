# Hibachi New Thread Prompt

아래 프롬프트를 새 Codex 창/스레드에 그대로 붙여 넣으면 됩니다.

```text
너는 `PerpDEX Farming Bot` 프로젝트 안에서 Hibachi 연동을 담당한다.

반드시 이 repo에서 작업한다:
C:\Users\USER\Documents\PerpDEX-Farming-Bot

중요:
- 이전 Codex 창은 실수로 `C:\Users\USER\Documents\PerpDEX-Farming-Bot 2`에서 열렸다.
- 지금부터는 ` 2` 없는 `C:\Users\USER\Documents\PerpDEX-Farming-Bot`만 기준으로 작업한다.
- `PerpDEX-Farming-Bot 2`는 이전 작업 복사본일 뿐이다.
- 실제 secret이 들어갈 수 있는 `.env`, `data/*.sqlite`, `.git`, `__pycache__`는 이전 폴더에서 복사하지 않는다.

프로젝트 범위:
- 이 프로젝트는 단일 거래소 안의 파밍/전략 봇이다.
- 지금 단계의 대상 거래소는 Hibachi다.
- 이종 거래소 간 거래, cross-exchange arbitrage, transfer/rebalance는 제외한다.
- cross-exchange 전략은 별도 `Cross-Exchange-Trading-Bot`에서만 다룬다.

현재까지 준비된 것:
- Phase 0 설계 문서가 있다.
- paper/dry-run 전용 전략 뼈대가 있다.
- Hibachi 기준 market-market paper cycle이 있다.
- Data Hub SQLite는 spread 신호만 read-only로 읽는 연결이 있다.
- 실제 orderbook depth는 Hibachi public SDK `get_orderbook`으로 조회하는 연결이 있다.
- 기간/라운드별 예산, 손실/거래량/포인트 성과 계산 뼈대가 있다.
- secret guard가 있어서 config에 api_key/private_key 같은 실제 값을 넣으면 막는다.
- `.env.example`에 Hibachi용 환경변수 이름이 준비되어 있다.
- `check_hibachi_env`는 키 존재 여부만 present/missing으로 확인한다.
- `hibachi_sdk_smoke`는 공식 `hibachi-xyz` SDK 기준 public/private-read-only smoke 준비 상태를 확인한다.
- `hibachi_paper_cycle`은 주문 없이 paper-only로 판단과 가상 체결만 검증한다.

절대 지킬 것:
- 실제 API key, private key, signature, session key를 채팅에 붙여넣지 않는다.
- 실제 키 값은 repo 파일에 저장하지 않는다.
- 실제 키 값은 로컬 `.env` 또는 환경변수에만 둔다.
- 처음에는 주문 API를 호출하지 않는다.
- 처음에는 public/read-only/private-read-only 확인까지만 한다.
- 실제 주문, 주문 취소, 포지션 변경은 명시 승인 전까지 금지한다.

Hibachi 환경변수:
`.env.example`을 참고해서 로컬 `.env`에 값을 채운다. 실제 값은 채팅에 보여주지 않는다.
account id는 대소문자를 신경 쓰지 않게 처리되어야 한다.
예: `hibachi_1`, `HIBACHI_1`, `Hibachi_1`은 같은 prefix로 인식한다.

첫 검증 명령:
```powershell
$env:PYTHONPATH="src"; python -m perpdex_farming_bot.cli.check_hibachi_env --account-id hibachi_1
$env:PYTHONPATH="src"; python -m perpdex_farming_bot.cli.hibachi_sdk_smoke --account-id hibachi_1
$env:PYTHONPATH="src"; python -m perpdex_farming_bot.cli.hibachi_paper_cycle --no-record
```

검증 기준:
- `python -m compileall src` 통과
- `.env`가 없어도 env check가 실패하지 않고 missing만 표시
- 키 값이 로그에 출력되지 않음
- public/read-only test와 private-read-only test가 주문/취소/포지션 변경을 호출하지 않음
- Data Hub DB는 spread 신호만 읽고, DB 열기 실패 시 no-trade
- 실시간 orderbook depth는 거래 봇이 Hibachi public SDK로 직접 조회
- 초보자도 이해할 수 있게 실행 방법을 쉽게 설명

지금 요청:
`C:\Users\USER\Documents\PerpDEX-Farming-Bot` 기준으로 현재 상태를 확인하고,
Hibachi read-only smoke와 paper-only cycle을 이어서 안전하게 진행하라.
실제 주문 기능은 만들지 말고, env check/public read-only/private read-only/paper-only 검증까지만 진행하라.
```
