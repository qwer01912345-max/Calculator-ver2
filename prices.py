"""Yahoo Finance 공개 시세. 실패하면 None; 임의 가격이나 ETF 대체 가격을 쓰지 않는다."""
from datetime import datetime, timezone
import math
import logging
logging.getLogger('yfinance').setLevel(logging.CRITICAL)

def latest_quote(symbol, currency):
    if not symbol: return dict(ok=False,error='직접 입력 자산')
    try:
        import yfinance as yf
        ticker=yf.Ticker(symbol)
        for interval in ('1m','1d'):
            try:
                history=ticker.history(period='5d',interval=interval,auto_adjust=False,
                                       actions=False,prepost=False,timeout=6,raise_errors=True)
            except yf.exceptions.YFRateLimitError:
                return dict(ok=False,error='시세 제공사의 요청 제한 · 직접 입력하세요.')
            except Exception:
                continue
            if history.empty: continue
            series=history['Close'].dropna()
            if series.empty: continue
            value=float(series.iloc[-1]);time=series.index[-1]
            meta=ticker.get_history_metadata()
            if meta.get('currency')!=currency:
                return dict(ok=False,error='시세 통화를 확인할 수 없습니다. 직접 입력하세요.')
            if not math.isfinite(value) or value<=0: continue
            if time.tzinfo is None: return dict(ok=False,error='시세 시간대 미확인')
            age=(datetime.now(timezone.utc)-time.to_pydatetime().astimezone(timezone.utc)).total_seconds()
            if age>7*86400 or age < -300: return dict(ok=False,error='시세 날짜가 오래됐거나 유효하지 않습니다.')
            return dict(ok=True,price=value,asof=time.isoformat(),source='Yahoo Finance',
                        kind='최근 1분봉 가격' if interval=='1m' else '최근 일봉 종가',
                        fetched=datetime.now(timezone.utc).isoformat())
    except Exception:
        pass
    return dict(ok=False,error='조회 실패 · 현재가를 직접 입력하세요.')
