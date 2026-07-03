# Phase 0 Design - PerpDEX Farming Bot

## 1. Phase 0 목표

Phase 0의 목표는 실제 거래 봇을 바로 만드는 것이 아닙니다. 먼저 안전한 설계도를 만드는 단계입니다.

이 단계에서 결정할 것:

- 어떤 모듈이 어떤 책임을 가지는지
- Dashboard/Data Hub에서 어떤 데이터를 읽을지
- 이 봇이 자체적으로 저장해야 할 데이터가 무엇인지
- paper trading이 어떤 흐름으로 동작할지
- 실거래 전에 반드시 필요한 risk limit과 kill switch가 무엇인지

이 단계에서 하지 않을 것:

- 실제 주문 전송
- 실제 포지션 조회
- wallet/private key/API key/signature/session key 저장
- 자전거래, 거래량 조작, 탐지 회피, 약관 우회 전략
- 다중 계정끼리 사고파는 전략
- 이종 거래소 간 거래, arbitrage, transfer/rebalance 전략

이 레포의 범위는 단일 거래소 안의 전략입니다. 여러 지갑을 쓸 수는 있지만, 서로 다른 거래소를 동시에 엮는 전략은 `Cross-Exchange-Trading-Bot`으로 분리합니다.

## 2. 전체 구조

권장 구조는 아래와 같습니다.

```text
config
  bot.yaml
  risk.paper.yaml
  exchange.paper.yaml

src/perpdex_farming_bot
  connectors
    base.py
    data_hub.py
    exchange_public.py
  strategies
    base.py
    market_making_paper.py
  risk
    engine.py
    limits.py
    kill_switch.py
  brokers
    paper_broker.py
  storage
    events.py
    paper_ledger.py
  logs
    run_logger.py
  cli
    main.py

data
  paper_trading.sqlite

logs
  bot.log
```

아직 코드를 구현하지 않아도 됩니다. Phase 0에서는 이 구조를 기준으로 다음 단계의 작업 범위를 잡습니다.

## 3. 핵심 모듈 역할

### Exchange Connector

거래소 또는 데이터 소스와 연결되는 입구입니다.

Phase 0/1에서는 읽기 전용으로 제한합니다.

허용:

- orderbook 읽기
- funding rate 읽기
- mark price/index price 읽기
- market metadata 읽기

금지:

- 주문 생성
- 주문 취소
- 포지션 변경
- 잔고 조회
- private key/API key/signature/session key 처리

초보자식으로 말하면, connector는 "거래소 창문"입니다. Phase 0에서는 창문으로 보기만 하고, 버튼은 누르지 않습니다.

### Strategy Engine

전략을 계산하는 부분입니다.

예시 전략:

- 스프레드가 충분히 넓을 때만 소액 지정가 호가를 내는 paper 전략
- funding rate와 변동성이 너무 위험하면 쉬는 전략
- inventory limit이 넘으면 한쪽 방향 주문을 중단하는 전략

Strategy Engine은 직접 주문하지 않습니다. 대신 "이 가격에 이 정도 크기의 주문을 내면 어떨까?"라는 주문 의도(order intent)만 만듭니다.

```text
Strategy Engine -> Order Intent
```

### 계획 중인 전략 1: 시장가-시장가 paper 전략

사용자가 처음에 실험하려는 전략입니다.

아이디어:

- 해당 마켓의 현재 bid/ask spread가 `x시간/일 평균 spread`보다 작을 때만 실행 후보로 봅니다.
- Best Bid 수량과 Best Ask 수량 중 더 작은 쪽을 기준으로 합니다.
- 그 수량의 50%와 사용자가 정한 금액 cap 중 더 작은 값만 주문 후보로 만듭니다.
- Buy/Sell 시장가 주문 의도를 빠르게 만들고, paper broker에서만 가짜 체결을 기록합니다.
- 한 번 실행 후 3초 cooldown을 둔 뒤 다시 spread monitoring을 합니다.
- 추가 spread cap은 `max_spread_bps`로 둡니다. `0`이면 이 cap을 끄고, 양수이면 현재 spread가 이 값 이하일 때만 후보로 봅니다.

중요한 컴플라이언스 메모:

시장가-시장가 전략 자체는 금지 대상으로 보지 않습니다. 특히 재단/거래소와 확인된 조건에서, 외부 유동성을 상대로 실제 fee를 내는 taker flow는 정상 사용자 행동으로 볼 수 있습니다.

다만 아래는 계속 금지합니다.

- 내가 낸 지정가 주문을 내가 직접 시장가로 먹는 self-cross
- 내 계정끼리 사고파는 wash trading
- fee를 실제로 부담하지 않거나 약관을 우회하는 거래량 생성
- 탐지 회피를 목적으로 한 주문 패턴 숨기기

따라서 이 프로젝트의 경계는 "시장가-시장가 금지"가 아니라 "자기 주문 체결 금지, 외부 상대방과의 실제 fee 거래만 허용"입니다. Phase 0/1에서는 여전히 paper-only로 검증하고, live 단계에서는 self-cross 방지 장치를 먼저 붙입니다.

### 계획 중인 전략 2: 지정가-시장가 paper 전략

아이디어:

- Best Bid 또는 Best Ask와 차상위 호가의 gap을 봅니다.
- 그 gap이 해당 마켓의 bid/ask spread 대비 낮을 때만 실행 후보로 봅니다.
- Best Bid/Ask와 같은 가격에 지정가 주문 의도를 냅니다.
- 주문 크기는 best level 수량의 50%와 금액 cap 중 더 작은 값입니다.
- paper broker에서 해당 지정가 주문이 체결된 것으로 판단되면, 바로 시장가 청산 의도를 만듭니다.
- 이후 3초 cooldown 후 다시 monitoring합니다.

이 전략도 Phase 1에서는 실제 주문이 아니라 paper order와 paper fill만 기록합니다.

### 계획 중인 전략 3: 지정가 + 지정가/시장가 stoploss paper 전략

아이디어:

- 매수/매도 거래량이 갑자기 반복적으로 생성되는지 실시간 체결창을 감시합니다.
- 예를 들어 시장가-시장가 전략과 비슷한 봇이 도는 것처럼 보이면 trigger 후보로 봅니다.
- Best Bid/Ask와 같은 가격에 지정가 주문 의도를 냅니다.
- 주문 크기는 감지된 금액의 50%와 금액 cap 중 더 작은 값입니다.
- stoploss는 손실을 작게 제한하기 위한 paper-only 보호 주문으로 설계합니다.
- stoploss 기준 가격은 차상위 호가 가격과 tick size를 기준으로 계산합니다.

이 전략은 Dashboard/Data Hub의 느린 기록만으로는 부족할 수 있습니다. 별도의 실시간 trade tape watcher가 필요합니다.

```text
Data Hub historical data = 평균 spread, 변동성, 과거 상태 분석
Realtime trade tape watcher = 갑작스러운 반복 체결 trigger 감지
Bot paper ledger = 내 전략의 주문 의도와 paper 결과 기록
```

처음부터 모든 체결창 데이터를 DB에 저장할 필요는 없습니다. Phase 1/2에서는 trigger 판단에 필요한 최근 몇 초의 rolling buffer만 메모리에 들고 있어도 충분합니다.

### 계획 중인 전략 4: 복수 지갑 페어 델타뉴트럴 paper 전략

아이디어:

- 하나의 거래소에서 2개 이상의 지갑을 사용합니다.
- spread가 낮을 때 지갑 A는 long, 지갑 B는 short를 같은 notional로 쌓습니다.
- 두 포지션을 합치면 방향성 노출이 작아지는 delta-neutral 상태를 목표로 합니다.
- 특정 포지션 한도에 도달하면 더 쌓지 않습니다.
- 특정 시간 이상 유지한 뒤, spread가 다시 낮아질 때 양쪽 포지션을 동시에 해제합니다.
- 이후 예산과 손실 한도가 남아 있으면 반복합니다.

중요한 경계:

- 두 지갑이 서로의 주문을 체결하면 안 됩니다.
- 각 지갑은 외부 유동성을 상대로 거래해야 합니다.
- live 단계에서는 reduce-only, position cap, hold time, self-cross 방지 장치가 먼저 필요합니다.

현재 코드는 이 전략도 paper intent만 만듭니다.

이 전략은 같은 거래소 안의 복수 지갑 전략입니다. 거래소 A와 거래소 B를 동시에 엮는 cross-exchange delta-neutral 전략은 이 레포에서 제외합니다.

Sizing 설정:

- `delta_neutral_total_collateral_usd`: paper 기준 전체 담보
- `delta_neutral_notional_cap_usd`: 한 번에 포지션 잡을 고정 USD 금액
- `delta_neutral_notional_pct_of_collateral`: 한 번에 포지션 잡을 담보 대비 비율
- `delta_neutral_max_pair_position_usd`: 최대 페어 포지션 고정 USD 금액
- `delta_neutral_max_pair_position_pct_of_collateral`: 최대 페어 포지션 담보 대비 비율

고정 USD와 담보 대비 비율이 둘 다 있으면 더 작은 금액을 사용합니다. 예를 들어 담보가 1000달러이고 `delta_neutral_notional_cap_usd=20`, `delta_neutral_notional_pct_of_collateral=0.02`이면 20달러와 20달러 중 작은 값인 20달러를 한 번 진입 금액으로 씁니다.

### Risk Engine

전략이 만든 주문 의도를 검사하는 안전장치입니다.

검사 항목:

- 주문 1개당 최대 금액
- 하루 최대 주문 수
- 하루 최대 손실
- 최대 포지션 크기
- 최대 inventory imbalance
- 가격이 너무 오래된 데이터인지
- 스프레드가 너무 좁은지
- 변동성이 너무 큰지
- kill switch가 켜졌는지

Risk Engine이 거절하면 paper broker도 실행하지 않습니다.

```text
Order Intent -> Risk Engine -> Approved/Rejected
```

### Paper Broker

실제 거래소가 아니라 가짜 장부에서 주문과 체결을 흉내 내는 모듈입니다.

Paper Broker가 하는 일:

- 주문 의도를 paper order로 기록
- orderbook 기준으로 체결 가능성을 시뮬레이션
- paper fill 기록
- paper position 계산
- paper PnL 계산
- 수수료와 슬리피지 가정 적용

중요한 점: Paper Broker는 진짜 돈을 움직이지 않습니다.

### Config

설정 파일은 전략과 리스크 값을 코드 밖에서 조정하게 해줍니다.

예시:

```yaml
mode: paper
market: BTC-PERP
max_order_notional_usd: 20
max_daily_loss_usd: 5
max_position_notional_usd: 100
max_orders_per_day: 50
kill_switch_enabled: false
```

초보자식으로 말하면, config는 "봇의 조절판"입니다. 코드를 뜯지 않고 숫자를 바꿀 수 있게 해줍니다.

### Budget and Performance

Budget은 "이번 기간 또는 이번 라운드에 얼마까지 쓸 수 있는가"를 정하는 장치입니다.

예시:

```yaml
budget:
  period_name: daily
  max_period_loss_usd: 5
  max_period_volume_usd: 500
  max_round_loss_usd: 1
  max_round_volume_usd: 40
```

Performance는 "쓴 돈 대비 결과가 어땠는가"를 보는 성적표입니다.

계산할 지표:

- gross volume: 전체 거래량
- realized PnL: paper 기준 손익
- realized loss: 손실만 양수로 표현한 값
- loss per volume: 거래량 1달러당 손실
- points estimate: 포인트 API나 추정식이 있을 때의 예상 포인트
- points per volume: 거래량 1달러당 포인트
- points per loss: 손실 1달러당 포인트

포인트는 거래소마다 계산 방식이 다를 수 있습니다. API 또는 공식 산식이 있으면 계산하고, 없으면 `unknown`으로 남깁니다.

### Logs

Logs는 나중에 "왜 봇이 이 결정을 했는지" 확인하는 기록입니다.

반드시 남길 로그:

- 어떤 데이터로 판단했는지
- 전략이 어떤 order intent를 만들었는지
- Risk Engine이 승인했는지 거절했는지
- paper broker가 체결했다고 봤는지
- paper PnL이 어떻게 변했는지
- kill switch가 언제 켜졌는지

## 4. Paper Trading 흐름

Phase 1에서 구현할 paper trading 흐름은 아래 순서가 좋습니다.

```text
1. Dashboard/Data Hub 또는 public connector에서 시장 데이터 읽기
2. Market Snapshot 생성
3. Strategy Engine이 Order Intent 생성
4. Risk Engine이 Order Intent 검사
5. 승인된 경우 Paper Broker가 가짜 주문 기록
6. orderbook 기준으로 가짜 체결 계산
7. paper position, paper PnL, risk event 저장
8. CLI 또는 dashboard에서 결과 확인
```

중요한 원칙:

```text
Strategy -> Risk -> Paper Broker
```

이 순서를 건너뛰는 코드는 만들지 않습니다.

## 5. Dashboard/Data Hub와 봇 자체 데이터 경계

이 프로젝트에는 참고 데이터 프로젝트가 있습니다.

```text
C:\Users\USER\Documents\PerpDEX-Dashboard-Data-Hub
```

### Data Hub에서 읽을 데이터

Data Hub는 "시장의 관찰 기록"을 제공하는 역할입니다.

읽을 수 있는 데이터:

- orderbook snapshot
- spread
- funding rate
- mark price
- index price
- historical price
- volatility estimate
- market metadata
- collector health/status

Bot은 이 데이터를 읽기 전용으로 사용합니다.

### Bot이 자체 저장해야 할 데이터

Bot은 "내 전략이 무엇을 했는지"를 저장합니다.

Bot 자체 데이터:

- strategy run
- order intent
- risk approval/rejection
- paper order
- paper fill
- paper position
- paper PnL
- kill switch event
- config snapshot
- run log

### 경계 정리

```text
Data Hub = 시장이 실제로 어땠는지
Bot      = 내 전략이 그 시장에서 무엇을 하려고 했는지
```

Data Hub는 원자료와 시장 상태를 담당하고, Bot은 paper trading 의사결정과 결과를 담당합니다.

서버 운영에서는 봇이 Data Hub SQLite를 직접 읽을 수 있습니다. 단, 반드시 read-only 연결을 사용합니다.

```text
file:C:/Users/redmi/Documents/PerpDEX-Dashboard-Data-Hub/data/live-5m-server.sqlite?mode=ro
```

첫 Hibachi 전략에서 Data Hub는 spread 신호로만 사용합니다. 봇은 아래 조건이면 거래하지 않습니다.

- Data Hub DB를 열 수 없음
- spread snapshot이 없음
- Data Hub spread가 평균 spread 기준보다 높음
- `max_spread_bps`가 0보다 클 때 Data Hub spread가 그 cap보다 높음

orderbook depth는 Data Hub DB가 아니라 거래 직전에 Hibachi public orderbook API로 다시 읽어서 판단합니다.

Data Hub DB에는 절대 write하지 않습니다. 봇 판단, paper 결과, 실제 주문/체결 로그는 모두 봇 자신의 DB나 로그에 저장합니다.

### 마지막 전략의 실시간 데이터 경계

세 번째 전략은 실시간 체결창이 필요합니다. 이 데이터는 다음처럼 나누는 것이 좋습니다.

- Data Hub: 평균 spread, 과거 orderbook, funding, volatility 같은 느린 분석 데이터
- Bot realtime watcher: 최근 몇 초 체결 흐름만 보는 trigger 데이터
- Bot storage: trigger가 발생했는지, 어떤 paper order intent를 만들었는지, risk engine이 승인/거절했는지

즉, 체결창 전체를 Dashboard/Data Hub에 모두 저장할 필요는 없습니다. "지금 trigger가 켜졌는가?"를 판단하는 작은 실시간 감시 모듈만 먼저 만들면 됩니다.

이 경계를 나누면 좋은 이유:

- Data Hub를 망가뜨리지 않고 봇을 실험할 수 있습니다.
- 같은 시장 데이터를 여러 전략이 공유할 수 있습니다.
- paper trading 결과와 원시장 데이터를 분리해서 비교할 수 있습니다.
- 나중에 실거래 단계로 가도 감사 추적이 쉬워집니다.

## 6. Phase 0 안전 경계

Phase 0의 안전 규칙은 다음과 같습니다.

### 반드시 허용되는 것

- 설계 문서 작성
- mock/paper 데이터 구조 설계
- 읽기 전용 public data connector 설계
- paper broker 설계
- risk limit 설계
- kill switch 설계
- 로그 구조 설계

### 아직 금지되는 것

- 실제 주문 API 호출
- 실제 주문 취소 API 호출
- 실제 포지션 변경
- private endpoint 호출
- private key/API key 저장
- 지갑 연결
- 여러 계정 사이 거래
- 거래량을 만들기 위한 반복 매매
- 탐지 회피 목적의 랜덤화

## 7. 초기 Risk Limit 제안

초기값은 보수적으로 시작합니다.

```yaml
mode: paper
max_order_notional_usd: 20
max_position_notional_usd: 100
max_daily_loss_usd: 5
max_orders_per_day: 50
min_spread_bps: 5
max_price_age_seconds: 10
max_inventory_imbalance_usd: 50
kill_switch_on_data_stale: true
kill_switch_on_daily_loss: true
```

실제 숫자는 나중에 Data Hub의 실제 spread, funding, volatility 데이터를 보고 조정합니다. 지금은 "봇이 손실 제한 장치를 통과해야 한다"는 구조를 먼저 만드는 것이 중요합니다.

## 8. 왜 이 순서가 안전한가

초보자 기준으로 아주 단순하게 말하면:

```text
설계 -> 가짜 거래 -> 위험 제한 -> dry-run -> 초소액 실거래
```

이 순서가 안전한 이유:

1. 설계 단계에서 위험한 기능을 미리 금지할 수 있습니다.
2. paper trading은 실제 돈을 쓰지 않기 때문에 실수 비용이 낮습니다.
3. Risk Engine을 먼저 만들면 전략이 과하게 움직여도 중간에서 막을 수 있습니다.
4. Logs를 먼저 남기면 봇이 왜 그런 판단을 했는지 나중에 추적할 수 있습니다.
5. dry-run은 실제 API 연결 직전의 마지막 예행연습입니다.
6. 초소액 실거래는 모든 장치가 작동한 뒤에만 검토합니다.

즉, 이 프로젝트는 "수익부터"가 아니라 "통제부터" 시작합니다.

## 9. Phase 0 완료 기준

Phase 0은 아래가 준비되면 완료로 봅니다.

- 안전 경계 문서화
- 모듈 구조 확정
- Data Hub와 Bot 자체 데이터 경계 확정
- paper trading 흐름 확정
- 초기 risk limit 목록 확정
- 실제 주문 미구현 상태 유지
- 다음 Phase 1 작업 범위 확정

## 10. Phase 1 추천 작업

Phase 1에서는 코드를 아주 작게 시작합니다.

추천 구현 순서:

1. 프로젝트 기본 Python 구조 생성
2. config 파일 로더 생성
3. Data Hub 읽기 전용 adapter 생성
4. Market Snapshot 타입 생성
5. Strategy Engine 기본 인터페이스 생성
6. Risk Engine 기본 limit 검사 생성
7. Paper Broker 장부 생성
8. CLI에서 paper run 1회 실행
9. logs와 SQLite에 결과 저장

Phase 1에서도 실제 주문 기능은 만들지 않습니다.
