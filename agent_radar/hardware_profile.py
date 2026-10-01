"""Business scope: supplies of servers and data storage, any manufacturer.

A lexical gate is deliberately broad within hardware, but never treats generic
software/1C activity as an opportunity. Uncertain price does not mean zero.
"""
from __future__ import annotations
import re
from decimal import Decimal, InvalidOperation

HARDWARE=re.compile(r"(?i)(?<!\w)(?:схд|систем\w* хранени\w* данн\w*|дисков\w* (?:массив\w*|полк\w*)|"
                    r"сервер(?:ы|ов|а|ами|ах|ное|ного|ному|ным|ный)?|servers?|"
                    r"(?:san|nas)[- ](?:storage|систем\w*|хранилищ\w*)|storage (?:array|system)|"
                    r"poweredge|proliant|thinksystem|primergy|oceanstor|powerstore|unity|"
                    r"eternus|netapp|infinidat|yadro|aquarius|аэродиск|аэродиск\w*|татлин|tatlin)(?!\w)")
NON_HARDWARE=re.compile(r"(?i)(?:мебел\w*|серверн\w* част\w* (?:приложени\w*|программ\w*)|"
                        r"sql server|windows server|exchange server|лицензи\w*|"
                        r"обслуживани\w*|технич\w* поддержк\w*|сервисн\w* сопровожд\w*|"
                        r"ремонт\w* сервер\w*|аренд\w* (?:виртуальн\w*|сервер\w*)|"
                        r"строительств\w*|пожарн\w*)")


def hardware_signal(card: dict) -> str | None:
    title=str(card.get("title") or "")
    description=str(card.get("description") or "")
    combined=title+"\n"+description
    evidence=re.sub(r"(?i)(?:sql|windows|exchange)\s+server(?:\s+\d+)?|виртуальн\w*\s+сервер\w*(?:\s+в\s+облак\w*)?|cloud servers?|облачн\w*\s+сервер\w*","",combined)
    matches=list(HARDWARE.finditer(evidence))
    if not matches:return None
    # Subject-title maintenance/licensing/room works is not a hardware supply.
    if re.search(r"(?i)(?:программ\w*\s+обеспечени\w*|разработк\w*|лицензи\w*)",title) and not HARDWARE.search(title):return None
    if re.search(r"(?i)(?:аренд\w*|сертификат\w*\s+тп|антивирус\w*|размещени\w*\s+сервер\w*|"
                 r"(?:сервисн\w*|техническ\w*)\s+(?:выезд\w*|работ\w*|обслуживани\w*)|"
                 r"обработк\w*\s+чат\w*|серверн\w*\s+шкаф\w*|видеокарт\w*)",title) and not re.search(r"(?i)(?:поставк\w*|приобретени\w*)\s+(?:сервер(?:ов|ы|а)?|схд|систем\w* хранени\w*)",title):return None
    if NON_HARDWARE.search(title):
        explicit=re.search(r"(?i)(?:поставк\w*|приобретени\w*|закупк\w*)\s+(?:нов\w*\s+)?(?:сервер(?:ов|ы|а)?|схд|систем\w* хранени\w*|оборудован\w*)",title)
        if not explicit or re.search(r"(?i)(лицензи\w*|мебел\w*|sql server|windows server|серверн\w* част\w*)",title):return None
    if re.search(r"(?i)без поставки (?:оборудования|серверов)",combined):return None
    match=next((m for m in matches if m.group().lower() not in ("unity","aquarius","yadro")),None)
    # Multi-product company names are not hardware product evidence.
    if match is None:return None
    return match.group()


def budget_decision(budget: dict | None, *, minimum_rub=5_000_000) -> dict:
    result={"decision":"unknown_include","minimum_rub":minimum_rub,"amount":None,"currency":None,"basis":"unknown"}
    if not isinstance(budget,dict):return result
    currency=str(budget.get("currency") or "").upper()
    basis=budget.get("basis")
    if basis not in ("procedure_total","contract_total") or currency not in ("RUB","RUR","643","РУБ","₽"):
        return result
    amount=budget.get("amount")
    if isinstance(amount,bool):return result
    try:
        number=Decimal(str(amount).replace(" ","").replace(",","."))
    except (InvalidOperation,ValueError,TypeError):return result
    if not number.is_finite() or number<=0 or number>Decimal("1000000000000000"):return result
    result.update(amount=str(number),currency="RUB",basis=basis,
                  decision="include" if number>=minimum_rub else "below_threshold")
    return result
