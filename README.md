# PerpDEX Farming Bot

단일 PerpDEX에서 포인트/인센티브를 얻기 위한 합법적이고 리스크 제한이 있는 전략 실행 봇 프로젝트입니다.

현재 단계는 **Phase 0: 설계와 안전 경계 확정**입니다. 이 단계에서는 실제 주문, 포지션, 지갑, API key, private key, signature, session key를 다루지 않습니다.

## 현재 문서

- [Phase 0 설계](docs/phase-0-design.md)
- [Phase 1 시작 프롬프트](docs/phase-1-start-prompt.md)
- [보안/다중 계정 설계](docs/security-and-multi-account.md)
- [Hibachi 안전 온보딩](docs/hibachi-onboarding.md)
- [Data Hub 읽기 전용 계약](docs/data-hub-readonly-contract.md)

## 현재 코드 상태

현재 코드는 실제 거래 봇이 아니라 **paper/dry-run 전용 전략 뼈대**입니다.

실행 흐름은 아래처럼 제한됩니다.

```text
mock market data -> strategy -> risk engine -> paper broker
```

여기서 `paper broker`는 실제 거래소에 주문하지 않고, "주문 의도"와 "가짜 체결 결과"만 기록합니다.

또한 기간별/라운드별 예산과 성과표 뼈대가 있습니다.

- 기간별 최대 손실
- 기간별 최대 거래량
- 라운드별 최대 손실
- 라운드별 최대 거래량
- 손실 대비 거래량
- 거래량 대비 포인트
- 손실 대비 포인트

포인트 계산은 거래소 API나 포인트 산식이 없으면 `unknown`으로 표시하고 넘어갑니다.

## 기본 원칙

1. 처음에는 반드시 paper trading 또는 dry-run부터 시작합니다.
2. 전략은 거래소에 직접 주문하지 않습니다.
3. 모든 주문 의도는 Risk Engine을 통과해야 합니다.
4. Phase 0에서는 실제 주문 기능을 구현하지 않습니다.
5. 내가 낸 지정가 주문을 내가 직접 시장가로 먹는 self-cross/wash trading은 금지합니다.
6. 외부 유동성을 상대로 실제 fee를 내는 시장가 taker flow는 별도 전략으로 허용하되, paper/dry-run에서 먼저 검증합니다.
7. 이 레포에서는 이종 거래소 간 거래를 제외합니다. Cross-exchange 전략은 별도 `Cross-Exchange-Trading-Bot`에서 다룹니다.

## 추천 실행 순서

```text
Phase 0: 설계와 안전 경계
Phase 1: Data Hub 읽기 + paper trading 로그 저장
Phase 2: Risk Engine + kill switch 강화
Phase 3: dry-run 주문 경로 검증
Phase 4: 명시 승인 후 초소액 실거래
```

초보자 기준으로 쉽게 말하면, 이 프로젝트는 먼저 "가짜 돈으로 연습장 만들기"부터 시작합니다. 그 다음 손실 제한 장치를 붙이고, 마지막에 아주 작은 실제 주문을 검토합니다.

## 로컬 paper 실행 예시

PowerShell에서 아래처럼 실행합니다.

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.main --strategy market-market
```

## Hibachi 안전 확인 예시

아래 명령은 실제 주문을 만들지 않습니다. 첫 번째 명령은 로컬 `.env`에 필요한 값이 있는지만 확인하고, 두 번째 명령은 read-only smoke test 준비 상태만 확인합니다.

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.check_hibachi_env
python -m perpdex_farming_bot.cli.hibachi_readonly_smoke
```

`hibachi_readonly_smoke`는 기본적으로 네트워크 요청도 보내지 않습니다. 공식 문서로 public market/orderbook 경로를 확인한 뒤에만 `--public-path`와 `--network`를 붙여 public GET 테스트를 실행합니다.

계정별 API key를 여러 개 쓸 때는 prefix를 붙입니다. 대소문자는 신경 쓰지 않아도 됩니다.

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.check_hibachi_env --account-id hibachi_1
python -m perpdex_farming_bot.cli.check_hibachi_env --account-id HIBACHI_1
```

위 두 명령은 둘 다 `.env`에서 아래 값을 찾습니다.

```text
HIBACHI_1_API_KEY_PRODUCTION=
HIBACHI_1_PUBLIC_KEY_PRODUCTION=
HIBACHI_1_PRIVATE_KEY_PRODUCTION=
HIBACHI_1_ACCOUNT_ID_PRODUCTION=
```

Hibachi 공식 Python SDK를 쓰려면 로컬에 패키지를 설치해야 합니다.

```powershell
python -m pip install hibachi-xyz
```

설치 후 public SDK smoke:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_sdk_smoke --network --public --symbol BTC/USDT-P
```

`--granularity`를 생략하면 SDK inventory에서 해당 마켓의 orderbook granularity를 자동 선택합니다.

설치 후 private read-only smoke:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_sdk_smoke --network --private-readonly
```

이 명령은 account info와 capital balance를 읽기 전용으로 확인하지만, 실제 balance 숫자는 기본 출력하지 않습니다.

특정 계정 prefix를 쓰려면:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_sdk_smoke --account-id hibachi_1 --network --private-readonly
```

## Hibachi paper cycle 예시

아래 명령은 Hibachi 기준 설정을 읽어서 여러 마켓의 market-market paper 기회를 한 번 훑습니다. 아직 실제 주문은 보내지 않고, mock/dashboard형 시장 스냅샷으로 paper 체결과 주간 거래량 장부만 기록합니다.

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_paper_cycle
```

기록 DB 기본 위치:

```text
data/hibachi_paper.sqlite
```

현재 지원되는 Hibachi paper 규칙:

- 현재 spread가 평균 spread보다 낮거나 같은 경우만 후보로 봅니다.
- `max_spread_bps`가 `0`이면 별도 spread cap을 끕니다.
- `max_spread_bps`가 양수이면 현재 spread가 그 값보다 낮거나 같아야 합니다.
- Best Bid/Ask 중 작은 호가 수량의 50%와 설정된 최대 주문 금액 중 더 작은 값을 사용합니다.
- 매수 시장가 paper intent를 만든 뒤, 바로 매도 시장가 paper intent를 만들어 포지션을 닫는 것으로 시뮬레이션합니다.
- UTC 기준 주간 거래량과 paper loss를 SQLite에 기록합니다.
- 여러 마켓은 `config/hibachi.paper.json`의 `strategy_assignments`에 켜진 순서대로 확인합니다.

특정 마켓만 확인하려면:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_paper_cycle --market BTC/USDT-P
```

이번 실행에서만 설정 spread cap을 바꿔 보려면:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_paper_cycle --max-spread-bps 2.5
```

DB에 기록하지 않고 계산만 보려면:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_paper_cycle --no-record
```

서버 컴퓨터의 Dashboard/Data Hub SQLite를 읽어서 paper 판단을 하려면:

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.hibachi_paper_cycle `
  --data-source data-hub `
  --orderbook-source hibachi-sdk `
  --network `
  --data-hub-db "C:\Users\redmi\Documents\PerpDEX-Dashboard-Data-Hub\data\live-5m-server.sqlite"
```

이 명령은 Data Hub DB를 `mode=ro`로만 열어 spread 신호만 읽습니다. 실제 best bid/ask 수량과 depth는 Hibachi public orderbook API에서 실시간으로 다시 읽습니다.

다른 전략 뼈대도 확인할 수 있습니다.

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.main --strategy limit-market
python -m perpdex_farming_bot.cli.main --strategy limit-stoploss
python -m perpdex_farming_bot.cli.main --strategy paired-delta-neutral --paired-phase entry
```

이미 사용한 기간 예산을 넣어서 실행할 수도 있습니다.

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.main --strategy market-market --period-volume-usd 200 --period-loss-usd 1.5
```

페어 델타뉴트럴 청산 조건도 paper로 확인할 수 있습니다.

```powershell
$env:PYTHONPATH="src"
python -m perpdex_farming_bot.cli.main --strategy paired-delta-neutral --paired-phase exit --held-seconds 7200 --current-pair-position-usd 20
```

페어 델타뉴트럴 전략은 고정 USD 금액과 전체 담보 대비 퍼센트를 함께 지원합니다.

```json
"delta_neutral_total_collateral_usd": 1000,
"delta_neutral_notional_cap_usd": 20,
"delta_neutral_notional_pct_of_collateral": 0.02,
"delta_neutral_max_pair_position_usd": 100,
"delta_neutral_max_pair_position_pct_of_collateral": 0.1
```

위 예시는 전체 담보를 1000달러로 보고, 한 번에 잡을 금액은 `20달러`와 `담보의 2%` 중 작은 값, 최대 페어 포지션은 `100달러`와 `담보의 10%` 중 작은 값을 사용합니다.
