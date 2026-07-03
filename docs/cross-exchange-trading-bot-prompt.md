# Cross-Exchange-Trading-Bot Start Prompt

아래 프롬프트는 별도 `Cross-Exchange-Trading-Bot` 프로젝트에서 사용할 시작 프롬프트입니다.

```text
너는 `Cross-Exchange-Trading-Bot` 프로젝트를 담당한다.

중요한 분리 원칙:
- `PerpDEX Farming Bot`은 단일 거래소 안의 복수 지갑/복수 마켓 전략만 다룬다.
- 이 프로젝트는 서로 다른 거래소를 동시에 비교하거나 거래하는 전략만 다룬다.
- 두 프로젝트의 config, secret, position ledger, risk limit은 섞지 않는다.

목표:
서로 다른 PerpDEX/거래소 간 가격, spread, funding, liquidity, point incentive 차이를 비교하고, paper trading 또는 dry-run으로 cross-exchange 전략을 설계한다.

초기 전략 후보:
1. Exchange A long / Exchange B short delta-neutral paper strategy
2. Funding 차이 기반 cross-exchange hedge paper strategy
3. 거래소별 spread/liquidity/fee를 반영한 진입/청산 조건
4. 거래소 간 transfer/rebalance는 실제 전송 없이 별도 ledger 시뮬레이션으로만 설계

안전 경계:
- 처음에는 반드시 paper trading 또는 dry-run만 한다.
- 실제 주문 API 호출 금지
- 실제 출금/입금/브릿지/transfer 실행 금지
- private key/API key/signature/session key 저장 금지
- 각 거래소 계정의 담보, 포지션, 손실 한도를 별도로 관리한다.
- cross-exchange 전략은 latency, partial fill, exchange outage, transfer delay, funding mismatch를 리스크로 기록해야 한다.
- self-cross/wash trading, 거래량 조작, 탐지 회피, 약관 우회 전략은 금지한다.

필수 구조:
1. Exchange Connector
   - 거래소별 public market data adapter
   - 나중에 private API가 필요해도 env var/secret manager만 사용

2. Cross-Exchange Market Registry
   - 같은 기초자산을 거래소별 market symbol로 매핑
   - 예: BTC-PERP on Exchange A vs BTC-PERP on Exchange B

3. Strategy Engine
   - Exchange A/B의 spread, funding, fee, liquidity를 비교
   - order intent만 만들고 실제 주문은 만들지 않는다.

4. Risk Engine
   - 거래소별 max loss
   - 전체 strategy max loss
   - exchange-specific position cap
   - total delta cap
   - partial-fill cap
   - stale-data kill switch
   - exchange outage kill switch

5. Paper Broker
   - 거래소별 paper fill을 따로 계산
   - partial fill, slippage, fee를 시뮬레이션
   - transfer/rebalance는 실제 실행 없이 ledger entry로만 기록

6. Performance Analytics
   - 거래량 대비 손실
   - loss 대비 points
   - exchange별 fee
   - funding PnL
   - hedge mismatch
   - latency/partial-fill risk event

7. Secrets Policy
   - 실제 key 값은 절대 repo에 저장하지 않는다.
   - config에는 env var 이름 또는 credential prefix만 둔다.
   - `.env.example`에는 빈 값만 둔다.
   - config에 api_key/private_key 같은 실제 값이 들어오면 시작 단계에서 차단한다.

Phase 0 요청:
1. Cross-Exchange-Trading-Bot의 Phase 0 설계를 작성하라.
2. 단일 거래소 봇과 이 프로젝트의 경계를 문서화하라.
3. exchange connector, market registry, strategy engine, risk engine, paper broker, performance analytics, secrets 구조를 제안하라.
4. 실제 주문/전송 없이 paper simulation부터 설계하라.
5. 초보자가 이해할 수 있게 왜 cross-exchange 전략은 별도 봇으로 분리해야 안전한지 설명하라.
```

