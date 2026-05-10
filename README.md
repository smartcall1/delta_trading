# DeltaBot — 통합 Delta-Neutral 양빵 봇

거래소 2개를 선택하면 자동으로 Delta-Neutral(양빵) 포지션을 잡아 펀딩레이트 차익을 수취하는 봇.

기존에 거래소 조합마다 별도 봇(dango_hibachi_bot, dango_standx_bot, delta_donemoji_bot, nado_grvt_bot)을 운영하던 것을 **하나의 통합 엔진**으로 일원화.

---

## 아키텍처

```
┌─────────────────────────────────────────────────┐
│                    main.py                       │
│         (Config 로드 → 거래소 생성 → 엔진 실행)    │
└────────────────────────┬────────────────────────┘
                         │
┌────────────────────────▼────────────────────────┐
│                   Engine                         │
│   IDLE → ANALYZE → ENTER → HOLD → EXIT → COOL  │
│                                                  │
│   self._maker: BaseExchange                     │
│   self._taker: BaseExchange                     │
└──────┬─────────────────────────────┬────────────┘
       │                             │
┌──────▼──────┐              ┌───────▼──────┐
│   Maker     │              │    Taker     │
│  (Dango)    │              │  (StandX)    │
│  (GRVT)    │               │  (Hibachi)   │
│  (Hibachi)  │              │  (Dango)     │
│  ...        │              │  ...         │
└─────────────┘              └──────────────┘
```

### 핵심 설계 원칙

1. **어댑터 패턴**: 모든 거래소가 `BaseExchange` ABC를 구현 → 엔진은 거래소 구체 구현을 모름
2. **팩토리 생성**: `.env`의 `MAKER_EXCHANGE`/`TAKER_EXCHANGE`만 바꾸면 조합 변경
3. **XEMM 전략**: Maker쪽 지정가(post-only) 체결 → Taker쪽 즉시(IOC/taker) 헷지
4. **볼륨모드 전용**: 별도 OI cap/볼륨 목표 없이 순수 펀딩 차익만 추구

---

## 지원 거래소

| ID | 거래소 | 프로토콜 | 인증 |
|----|--------|----------|------|
| `dango` | Dango Perps | GraphQL + EIP-712 | private_key + account_address |
| `standx` | StandX | REST + Ed25519 | jwt_token + private_key |
| `hibachi` | Hibachi | SDK (REST) | api_key + private_key + public_key + account_id |
| `grvt` | GRVT | GrvtCcxtWS SDK | api_key + private_key + trading_account_id |

---

## 설치 및 설정

### 1. 의존성 설치

```bash
pip install -r requirements.txt
```

GRVT 사용 시 추가로 `pysdk` 패키지가 필요 (GRVT에서 제공하는 별도 SDK).

### 2. 환경변수 설정

```bash
cp .env.example .env
```

`.env` 파일을 편집하여 거래소 선택 및 인증정보 입력:

```env
# 거래소 선택 (가능한 값: dango, standx, hibachi, grvt)
MAKER_EXCHANGE=dango
TAKER_EXCHANGE=standx

# Maker (Dango) 인증
MAKER_PRIVATE_KEY=<hex 또는 base64>
MAKER_ACCOUNT_ADDRESS=<dango 계정 주소>

# Taker (StandX) 인증
TAKER_JWT_TOKEN=<StandX JWT>
TAKER_PRIVATE_KEY=<Ed25519 키>
```

#### 심볼 매핑

각 거래소마다 심볼 표기가 다르므로 매핑 필수:

```env
# PAIR:거래소내부심볼 (콤마 구분)
SYMBOL_MAP_DANGO=ETH:ethusd,BTC:btcusd
SYMBOL_MAP_STANDX=ETH:ETH-PERP,BTC:BTC-PERP
```

#### Tick Size 설정

```env
TICK_SIZE_DANGO=ETH:0.01,BTC:0.1
TICK_SIZE_STANDX=ETH:0.01,BTC:0.1
```

### 3. 실행

```bash
python main.py
```

VPS/서버:
```bash
nohup python -u main.py > /dev/null 2>&1 &
```

---

## 트레이딩 파라미터

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `LEVERAGE` | 3 | 양쪽 거래소 레버리지 |
| `PAIRS` | ETH | 거래 대상 (콤마 구분) |
| `ENTRY_CHUNKS` | 10 | 진입 분할 수 |
| `EXIT_CHUNKS` | 10 | 청산 분할 수 |
| `MIN_HOLD_MINUTES` | 30 | 최소 보유 시간 (EXIT 조건 무시) |
| `MAX_HOLD_DAYS` | 2 | 최대 보유 기간 (초과 시 강제 EXIT) |
| `COOLDOWN_MINUTES` | 5 | 사이클 간 쿨다운 |

### 리스크 파라미터

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `MARGIN_EMERGENCY_PCT` | 10 | 마진율 이하 시 긴급 청산 |
| `SPREAD_OPPORTUNISTIC_USD` | 30 | 이 이상 이익 시 기회 청산 |
| `PRINCIPAL_BUFFER_USD` | 45 | 이 이상 손실 시 원금 보호 청산 |
| `EMERGENCY_CLOSE_SLIPPAGE_PCT` | 0.01 | 긴급 청산 시 허용 슬리피지 |

### 메이커 주문 파라미터

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `MAKER_RETRY_LIMIT` | 5 | 청크당 리트라이 횟수 |
| `MAKER_FILL_TIMEOUT_SECONDS` | 30 | 체결 대기 시간 |
| `MAKER_PRICE_STEP_USD` | 0.5 | 미체결 시 가격 양보폭 |

---

## 상태머신

```
IDLE ──────► ANALYZE ──────► ENTER ──────► HOLD
  ▲                                          │
  │                                          ▼
COOLDOWN ◄────────────────────────────────  EXIT
                                             │
                                             ▼
                                    MANUAL_INTERVENTION
```

| 상태 | 동작 |
|------|------|
| **IDLE** | 펀딩레이트 조회 → 최적 페어/방향 선택 → Position 생성 |
| **ANALYZE** | 레버리지 설정 확인 |
| **ENTER** | XEMM 청킹 진입 (maker post-only → taker 헷지) |
| **HOLD** | 30초 주기 EXIT 조건 확인 (만기/이익/손실/펀딩역전) |
| **EXIT** | XEMM 청킹 청산 (체결 확인 폴링 포함) |
| **COOLDOWN** | 대기 후 IDLE 복귀 |
| **MANUAL** | 자동 청산 실패 시 텔레그램 알림, 자동 복구 시도 |

---

## XEMM 진입/청산 메커니즘

### 진입 (Enter)

```
1. Maker BBO 조회
2. bid/ask 밀착 가격으로 post-only 지정가 주문
3. wait_for_fill (타임아웃 30s)
4. 미체결 시 → 주문 취소 → concession 증가 → 재시도
5. 체결 시 → Taker에 slippage 포함 가격으로 즉시 헷지
6. Taker 체결 폴링 (3회, 3초 간격)
7. Taker 미체결 → Maker 롤백 (시장가)
8. 양쪽 체결 확인 → 청크 완료
```

### 청산 (Exit)

```
1. Maker BBO 밀착 지정가 (reduce_only, post_only)
2. 미체결 시 시장가 fallback
3. Taker 청산 + 3회 폴링으로 실체결 확인
4. 청크 완료 → 잔여 재조회 → 다음 청크
```

### 안전장치

- **불균형 조정**: 진입 완료 후 maker/taker 사이즈 5% 이상 불균형 시 자동 조정
- **실잔여 조회**: EXIT 루프마다 실제 포지션 재조회 (ghost fill 방지)
- **Dust 청산**: 사이클 종료 시 잔여 미세 포지션 시장가 정리
- **크래시 복구**: 재시작 시 기존 양방향 포지션 감지 → HOLD 자동 진입

---

## 텔레그램 커맨드

| 커맨드 | 동작 |
|--------|------|
| `/status` | 현재 상태, 잔고, PnL |
| `/funding` | 전체 페어 펀딩레이트 |
| `/history` | 최근 5개 사이클 기록 |
| `/positions` | 양쪽 거래소 포지션 |
| `/close` | 강제 청산 시작 |
| `/stop` | 봇 종료 |

---

## 거래소 추가 방법

새 거래소 `newex`를 추가하려면:

### 1. Raw 클라이언트 작성 (또는 복사)

`exchanges/_newex_raw.py`에 해당 거래소 REST/WS 클라이언트 작성.

### 2. 어댑터 작성

`exchanges/newex.py`:

```python
from exchanges.base import BaseExchange, Balance, OrderResult

class NewExExchange(BaseExchange):
    name = "newex"

    async def start(self): ...
    async def stop(self): ...
    async def get_bbo(self, symbol) -> dict: ...
    async def get_mark_price(self, symbol) -> float: ...
    async def get_funding_rate(self, symbol) -> float: ...
    async def get_balance(self) -> Balance: ...
    async def get_position_signed_size(self, symbol) -> float: ...
    async def place_limit_order(self, ...) -> OrderResult: ...
    async def place_market_order(self, ...) -> OrderResult: ...
    async def cancel_all_orders(self, symbol) -> int: ...
```

### 3. 팩토리 등록

`exchanges/factory.py`에 추가:

```python
elif exchange_id == "newex":
    from exchanges.newex import NewExExchange
    return NewExExchange(**kwargs)
```

### 4. Config + .env

```env
SYMBOL_MAP_NEWEX=ETH:ETH-USD-PERP
TICK_SIZE_NEWEX=ETH:0.01
```

`config.py`에 해당 필드 추가.

---

## 파일 구조

```
deltabot/
├── main.py                 # 엔트리포인트
├── engine.py               # 통합 상태머신 엔진
├── config.py               # .env 기반 설정
├── models.py               # Position, BotState, Cycle
├── strategy.py             # 페어 선택 + EXIT 조건
├── telegram_ui.py          # 텔레그램 알림/커맨드
├── exchanges/
│   ├── __init__.py         # 공개 API
│   ├── base.py             # BaseExchange ABC
│   ├── factory.py          # 팩토리 (create_exchange)
│   ├── dango.py            # Dango 어댑터
│   ├── standx.py           # StandX 어댑터
│   ├── hibachi.py          # Hibachi 어댑터
│   ├── grvt.py             # GRVT 어댑터
│   ├── base_client.py      # GRVT SDK 호환 shim
│   ├── _dango_raw.py       # Dango raw 클라이언트
│   ├── _standx_raw.py      # StandX raw 클라이언트
│   ├── _hibachi_raw.py     # Hibachi raw 클라이언트
│   └── _grvt_raw.py        # GRVT raw 클라이언트
├── logs/                   # 런타임 로그 + 상태 파일
├── .env.example            # 설정 템플릿
├── requirements.txt        # 의존성
└── .gitignore
```

---

## 거래소 조합 예시

| Maker | Taker | 설명 |
|-------|-------|------|
| dango | standx | Dango maker + StandX taker (현재 검증 대상) |
| dango | hibachi | Dango maker + Hibachi taker |
| dango | grvt | Dango maker + GRVT taker |
| grvt | standx | GRVT maker + StandX taker |

Maker는 post-only 지정가 체결을 지원하는 거래소가 유리하고, Taker는 IOC/시장가가 빠른 거래소가 유리.

---

## 로그 및 상태

- `logs/bot.log` — 전체 런타임 로그
- `logs/bot_state.json` — 현재 상태머신 상태 (크래시 복구용)
- `logs/cycles.jsonl` — 완료된 사이클 기록 (PnL 집계용)

---

## 주의사항

1. **같은 거래소를 maker/taker 양쪽에 쓸 수 없음** (self-trade 위험)
2. **심볼 매핑 필수**: 거래소마다 심볼 표기가 다르므로 반드시 `SYMBOL_MAP_*` 설정
3. **tick size 설정**: 잘못된 tick size는 주문 거부 원인
4. **충분한 마진**: 양쪽 거래소에 레버리지 대비 충분한 증거금 필요
5. **API 키 권한**: 거래(trade) 권한이 있는 키만 사용
