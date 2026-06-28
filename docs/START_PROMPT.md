# Codex Start Prompt - PerpDEX Farming Bot

너는 `PerpDEX Farming Bot` 프로젝트를 담당한다.

## 목표
단일 PerpDEX에서 포인트/인센티브를 얻기 위한 합법적이고 리스크 제한이 있는 전략 실행 봇을 설계한다. 처음에는 반드시 paper trading 또는 dry-run부터 시작한다.

## 코드 레포
- 새 레포: `C:\Users\USER\Documents\PerpDEX-Farming-Bot`
- 데이터 참고 프로젝트: `C:\Users\USER\Documents\PerpDEX-Dashboard-Data-Hub`

## Obsidian HQ
- `C:\Users\USER\Desktop\Pagu's Works\06-Coding\PerpDEX Farming Bot`

## 안전 경계
- 자전거래, 자기 계정끼리 사고팔기, 거래량 조작, 탐지 회피, 약관 우회 전략은 금지한다.
- 실거래 전에는 paper trading, dry-run, risk limit, kill switch가 먼저다.
- private key/API key/signature/session key는 코드에 저장하지 않는다.
- 주문/포지션 기능은 명시적으로 승인된 단계에서만 추가한다.

## 가능한 전략 방향
- 소액 지정가 호가 제공
- 스프레드/펀딩/변동성 조건 기반 진입과 청산
- inventory limit이 있는 market making
- 손실 제한, 주문 수 제한, 포지션 한도, 일일 중단 조건

## 다음 작업 요청
1. 이 레포의 Phase 0 설계를 작성하라.
2. exchange connector, strategy engine, risk engine, paper broker, config, logs 구조를 제안하라.
3. 실제 주문은 구현하지 말고, paper trading 시뮬레이션부터 설계하라.
4. Dashboard/Data Hub에서 데이터를 읽는 방식과 이 봇이 자체적으로 필요한 데이터의 경계를 구분하라.
5. 초보자가 이해할 수 있게 왜 이 순서가 안전한지 설명하라.