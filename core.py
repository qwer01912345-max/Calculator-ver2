"""현금 포함, 계좌·통화별 예산 제한이 있는 밴드 리밸런싱 순수 계산 모듈."""
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

D = Decimal

DEFAULT_CASH_POLICY = dict(target=60, lower=50, upper=70, include_rp=False, fx_cost_pct=0)

def validate_cash_policy(policy=None):
    policy=dict(DEFAULT_CASH_POLICY, **(policy or {}))
    lo,tar,hi=(dec(policy[k],k) for k in ('lower','target','upper'))
    if not lo <= tar <= hi <= 100:
        raise ValueError('현금 원화 비중: 하단 ≤ 목표 ≤ 상단 ≤ 100이어야 합니다.')
    if dec(policy['fx_cost_pct'])>=100:
        raise ValueError('환전 비용 여유는 100% 미만이어야 합니다.')
    if type(policy['include_rp']) is not bool:
        raise ValueError('RP 포함 여부는 true/false여야 합니다.')
    policy.update(target=float(tar),lower=float(lo),upper=float(hi),
                  fx_cost_pct=float(dec(policy['fx_cost_pct'])))
    return policy

def cash_rebalance(krw, usd, fx, available_krw=None, available_usd=None,
                   rp_krw=0, rp_usd=0, policy=None):
    """매매·결제 완료 후 잔여 현금의 환전 제안. 모든 결과는 계산이며 실제 환전하지 않는다.
    목표/밴드는 현금끼리의 원화 평가액 비중. 비용 반영 후의 비율로 환전량을 풀고
    USD 0.01 단위로 내림한다. RP/예비 현금은 환전 재원이 되지 않는다.
    """
    policy=validate_cash_policy(policy)
    krw,usd,fx=dec(krw),dec(usd),dec(fx)
    if fx<=0: raise ValueError('환율은 0보다 커야 합니다.')
    ak=min(krw,dec(krw if available_krw is None else available_krw))
    au=min(usd,dec(usd if available_usd is None else available_usd))
    rk=dec(rp_krw) if policy['include_rp'] else D(0)
    ru=dec(rp_usd) if policy['include_rp'] else D(0)
    k_value=krw+rk; u_value=(usd+ru)*fx; total=k_value+u_value
    r=dict(status='현금 없음',direction='유지',total_krw=total,krw=krw,usd=usd,
           krw_pct=None,usd_pct=None,destination_pct=None,needed_usd=D(0),proposed_usd=D(0),
           krw_amount=D(0),unfilled_usd=D(0),post_krw=krw,post_usd=usd,
           post_krw_pct=None,post_usd_pct=None,estimated_fx_cost=D(0),policy=policy)
    if total==0: return r
    weight=k_value/total*100
    r.update(status='밴드 내 유지',krw_pct=weight,usd_pct=100-weight,
             post_krw_pct=weight,post_usd_pct=100-weight)
    lo,tar,hi=(dec(policy[k]) for k in ('lower','target','upper'))
    if lo<=weight<=hi: return r
    target=(lo+tar)/200 if weight<lo else (tar+hi)/200
    cost=dec(policy['fx_cost_pct'])/100
    r['destination_pct']=target*100
    if weight>hi:
        r['direction']='원화 → 달러'
        needed=(k_value-target*total)/(fx*(1+cost*(1-target)))
        capacity=ak/(fx*(1+cost))
    else:
        r['direction']='달러 → 원화'
        needed=(target*total-k_value)/(fx*(1-cost*(1-target)))
        capacity=au
    desired=needed.quantize(D('0.01'),rounding=ROUND_FLOOR)
    quantity=min(desired,capacity.quantize(D('0.01'),rounding=ROUND_FLOOR))
    if weight>hi:
        amount=(quantity*fx*(1+cost)).quantize(D(1),rounding=ROUND_CEILING)
        if amount>ak:
            quantity=max(D(0),quantity-D('0.01'))
            amount=(quantity*fx*(1+cost)).quantize(D(1),rounding=ROUND_CEILING)
        post_k,post_u=krw-amount,usd+quantity
        fx_cost=amount-quantity*fx
    else:
        amount=(quantity*fx*(1-cost)).quantize(D(1),rounding=ROUND_FLOOR)
        post_k,post_u=krw+amount,usd-quantity
        fx_cost=quantity*fx-amount
    post_total=post_k+rk+(post_u+ru)*fx
    post_weight=(post_k+rk)/post_total*100 if post_total else None
    r.update(status='환전 필요' if quantity==desired and quantity>0 else
             '환전 단위 미만' if desired==0 else '환전 재원 부족',
             needed_usd=desired,proposed_usd=quantity,krw_amount=amount,
             unfilled_usd=desired-quantity,post_krw=post_k,post_usd=post_u,
             post_krw_pct=post_weight,post_usd_pct=None if post_weight is None else 100-post_weight,
             estimated_fx_cost=fx_cost)
    return r

DEFAULT_ASSETS = [
    ('SP500','TIGER 미국S&P500','360750.KS','KRW',30,25,35,'주'),
    ('VXUS','VXUS','VXUS','USD',25,20,30,'주'),
    ('AVUV','AVUV','AVUV','USD',5,4,6,'주'),
    ('UPRO','UPRO','UPRO','USD',5,4,6,'주'),
    ('KR3Y','TIGER 국채3년','114820.KS','KRW',10,8,12,'주'),
    ('TIPS','KIWOOM 물가채KIS','430500.KS','KRW',2.5,2,3,'주'),
    ('KMLM','KMLM','KMLM','USD',5,4,6,'주'),
    ('CTA','CTA','CTA','USD',5,4,6,'주'),
    ('GOLD','KRX 금현물 99.99 1kg','','KRW',5,4,6,'g'),
    ('UST30','KODEX 미국30년국채액티브(H)','484790.KS','KRW',2.5,2,3,'주'),
    ('ILS','Brookmont Catastrophic Bond ETF','ILS','USD',5,4,6,'주'),
]

def defaults():
    return [dict(id=i,name=n,ticker=t,currency=c,target=w,lower=l,upper=u,
                 unit=unit,lot=1,account='통합') for i,n,t,c,w,l,u,unit in DEFAULT_ASSETS]

def dec(v, label='숫자'):
    try:
        if isinstance(v,bool): raise ValueError()
        n = D(str(v))
        if not n.is_finite() or n < 0: raise ValueError()
        return n
    except Exception:
        raise ValueError(f'{label}: 0 이상의 유효한 숫자를 입력하세요.') from None

