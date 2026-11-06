import json
import re
from urllib.request import Request, urlopen


def quote_shipping(base, sku, seller, cep, expected_price):
    if not re.fullmatch(r'\d{8}',cep or ''):
        return None
    payload = dict(items=[dict(id=str(sku),quantity=1,seller=str(seller))],country='BRA',postalCode=cep)
    request = Request(base + '/api/checkout/pub/orderForms/simulation?sc=1',
                      data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Accept':'application/json'},method='POST')
    try:
        with urlopen(request,timeout=8) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw)>1024*1024:
            return None
        data=json.loads(raw)
        items=data.get('items',[])
        if not items or items[0].get('availability')!='available' or items[0].get('sellingPrice') != expected_price:
            return None
        slas=[sla for info in data.get('logisticsInfo',[]) if info.get('itemIndex')==0
              for sla in info.get('slas',[]) if sla.get('deliveryChannel')=='delivery'
              and isinstance(sla.get('price'),int) and sla['price']>=0]
        if not slas:
            return None
        best=min(slas,key=lambda sla:sla['price'])
        tax=items[0].get('tax',0)
        if not isinstance(tax,int) or tax<0:
            return None
        return dict(shipping_cents=best['price'],tax_cents=tax,shipping_cep=cep,
                    delivery_estimate=str(best.get('shippingEstimate',''))[:100],
                    final_cents=expected_price+best['price']+tax,shipping_known=True)
    except Exception:
        return None
