"""Business scope: supplies of servers and data storage, any manufacturer.

A lexical gate is deliberately broad within hardware, but never treats generic
software/1C activity as an opportunity. Price is not a criterion: the agent judges the customer.
"""
from __future__ import annotations
import re

HARDWARE=re.compile(r"(?i)(?<!\w)(?:схд|систем\w* хранени\w* данн\w*|дисков\w* (?:массив\w*|полк\w*)|"
                    r"сервер(?:ы|ов|а|ами|ах|ное|ного|ному|ным|ный|ных|ные|ными|ном)?|servers?|"
                    r"(?:san|nas)[- ](?:storage|систем\w*|хранилищ\w*)|storage (?:array|system)|"
                    r"poweredge|proliant|thinksystem|primergy|oceanstor|powerstore|unity|"
                    r"eternus|netapp|infinidat|yadro|aquarius|аэродиск|аэродиск\w*|татлин|tatlin|"
                    r"qnap|synology|l?rdimm)(?!\w)")
# Parts named only by their kind are a signal in the subject line; in a goods list they describe PCs and tills.
TITLE_PARTS=re.compile(r"(?i)(?<!\w)(?:суперкомпьютер\w*|жестк\w* диск\w*|оперативн\w* памят\w*|модул\w* памят\w*|"
                       r"(?:ssd|hdd|nvme)[- ]?(?:накопител\w*|диск\w*)|(?:накопител\w*|диск\w*) (?:ssd|hdd|nvme))(?!\w)")
GATE=4  # bump when the gate widens: the source then re-reads its whole window once
NON_HARDWARE=re.compile(r"(?i)(?:мебел\w*|серверн\w* част\w* (?:приложени\w*|программ\w*)|"
                        r"sql server|windows server|exchange server|лицензи\w*|"
                        r"обслуживани\w*|технич\w* поддержк\w*|сервисн\w* сопровожд\w*|"
                        r"ремонт\w* сервер\w*|аренд\w* (?:виртуальн\w*|сервер\w*)|"
                        r"строительств\w*|пожарн\w*)")


def hardware_signal(card: dict) -> str | None:
    title=str(card.get("title") or "")
    description=str(card.get("description") or "")
    combined=title+"\n"+description
    evidence=re.sub(r"(?i)(?:sql|windows|exchange)\s+server(?:\s+\d+)?|виртуальн\w*\s+сервер\w*(?:\s+в\s+облак\w*)?|cloud servers?|облачн\w*\s+сервер\w*|серверн\w*\s+(?:помещени\w*|комнат\w*)","",combined)
    matches=list(HARDWARE.finditer(evidence))+list(TITLE_PARTS.finditer(title))
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
