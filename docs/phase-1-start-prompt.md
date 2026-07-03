# Phase 1 Start Prompt

아래 프롬프트를 다음 작업 시작 시 그대로 붙여 넣으면 됩니다.

```text
너는 PerpDEX Farming Bot 프로젝트의 Phase 1 구현을 담당한다.

현재 레포:
C:\Users\USER\Documents\PerpDEX-Farming-Bot 2

참고 데이터 프로젝트:
C:\Users\USER\Documents\PerpDEX-Dashboard-Data-Hub

현재 완료 상태:
- Phase 0 설계 문서 작성 완료
- 실제 주문/포지션/지갑/API key 기능은 아직 금지
- 목표는 paper trading MVP

Phase 1 목표:
1. Python 프로젝트 기본 구조를 만든다.
2. config 파일을 읽는 기능을 만든다.
3. Dashboard/Data Hub에서 읽기 전용으로 market snapshot을 가져오는 adapter를 만든다.
4. Strategy Engine은 order intent만 만들게 한다.
5. Risk Engine은 max_order_notional, max_position_notional, max_daily_loss, stale_data를 검사한다.
6. Paper Broker는 실제 주문 없이 paper order/fill/position/PnL만 저장한다.
7. SQLite와 logs에 실행 결과를 저장한다.
8. CLI에서 dry-run/paper run 1회를 실행할 수 있게 한다.

안전 경계:
- 실제 주문 API 호출 금지
- 실제 주문 취소 API 호출 금지
- private key/API key/signature/session key 저장 금지
- 지갑 연결 금지
- 자전거래, 거래량 조작, 탐지 회피, 약관 우회 전략 금지
- 이종 거래소 간 거래, cross-exchange arbitrage, cross-exchange transfer/rebalance 전략 금지
- 복수 지갑 전략은 같은 거래소 안에서만 paper로 다룬다

초보자도 이해할 수 있게 변경 이유와 실행 방법을 쉽게 설명해 줘.
```
