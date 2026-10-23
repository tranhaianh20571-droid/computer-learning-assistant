"""Deterministic evidence checks. Model summaries never count as page text."""
import hashlib
import re
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

DIMENSIONS = ("writings","conversations","expression","external","decisions","timeline")


def canonical_source(url):
    try:
        value=urlsplit(url)
        if (value.scheme!="https" or not value.hostname or value.port not in (None,443)
                or value.username or value.password):
            return ""
        host=value.hostname.lower().removeprefix("www.")
        query=[(k,v) for k,v in parse_qsl(value.query,keep_blank_values=True)
               if not k.lower().startswith("utm_") and k.lower() not in ("fbclid","gclid")]
        return urlunsplit(("https",host,value.path.rstrip("/") or "/",urlencode(sorted(query)),""))
    except (ValueError,TypeError,AttributeError):
        return ""


def page_status(text):
    if len(text.strip())<500:
        return "too_short"
    head=text[:4000].lower()
    if any(token in head for token in ("just a moment...", "enable javascript and cookies",
        "verify you are human", "checking your browser", "access denied",
        "javascript is disabled", "captcha", "please enable javascript")):
        return "challenge"
    return "readable"


def body_hash(text):
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def verified_assessment(source, assessment):
    value=assessment.model_dump(exclude={"source_id"})
    text=source.get("text","")
    readable=source.get("read_status") in ("full_text","partial_text") and page_status(text)=="readable"
    # Whitespace normalization only: punctuation, prefixes and translations are not quotes.
    quote=value["evidence"].strip()
    start=text.find(quote) if quote else -1
    if start<0 and quote:
        matches=list(re.finditer(r"\S+",text))
        normalized=" ".join(match.group() for match in matches)
        needle=" ".join(quote.split())
        found=normalized.find(needle)
        if found>=0:
            token_start=normalized[:found].count(" ")
            token_end=token_start+len(needle.split())-1
            start=matches[token_start].start()
            quote=text[start:matches[token_end].end()]
    if value["kind"]=="primary" and (not readable or start<0 or len(quote)<12 or not value["relevant"]):
        value.update(kind="unknown",evidence="",reason="原文定位或正文有效性未通过核验；不计入一手资料。")
    if value["kind"]=="primary":
        value.update(evidence=quote,evidence_start=start,evidence_end=start+len(quote),
                     body_sha256=body_hash(text))
    value["usable"]=readable and value["relevant"]
    value["dimensions"]=list(dict.fromkeys(value["dimensions"]))
    return {**source,**value}


def evidence_summary(sources):
    usable=[source for source in sources if source.get("usable")]
    return {"candidates":len(sources),"effective_documents":len(usable),
        "verified_primary":sum(source.get("kind")=="primary" for source in usable),
        "dimensions":{name:sum(name in source.get("dimensions",[]) for source in usable) for name in DIMENSIONS}}


def unmeasured_style_numbers(text):
    """A quantitative style claim needs a separate reproducible corpus measurement."""
    return bool(re.search(
        r"(?:平均句长|average sentence length|we\s*/\s*our|代词比例|疑问句比例|类比密度)"
        r"[^\n。]{0,35}\d+(?:\.\d+)?(?:\s*%|\s*词|\s*words)",text,re.I))


def evidence_checks(sources, policy):
    summary=evidence_summary(sources)
    return [
        {"name":"source_coverage","passed":summary["effective_documents"]>=policy.min_documents,
         "reason":f"有效正文 {summary['effective_documents']} / {policy.min_documents}；失败、无关与重复不计。"},
        {"name":"primary_source_ratio","passed":summary["verified_primary"]>=policy.min_primary,
         "reason":f"可逐字定位的一手正文 {summary['verified_primary']} / {policy.min_primary}。"},
        {"name":"dimension_coverage","passed":all(summary["dimensions"].values()),
         "reason":"六维均须有实际有效资料；共享来源可支持多个维度，维度缺口不得靠模型补造。"}]


async def read_public_source(url,deps):
    """Anonymous, bounded public page read through the existing DNS-pinned exit."""
    import socket
    from html.parser import HTMLParser
    import httpx2
    from ..agents.native import SafeModelTransport
    from ..net_policy import addresses_allowed, is_literal_address
    if not canonical_source(url):return ""
    def public_resolver(*args):
        host=args[0] if args else ""
        rows=socket.getaddrinfo(*args)
        if not addresses_allowed([row[4][0] for row in rows],
                from_hostname=not is_literal_address(host)):
            raise ValueError("nonpublic source")
        return rows
    class Text(HTMLParser):
        def __init__(self):super().__init__();self.hidden=0;self.parts=[]
        def handle_starttag(self,tag,attrs):
            if tag in ("script","style","noscript"):self.hidden+=1
        def handle_endtag(self,tag):
            if tag in ("script","style","noscript"):self.hidden=max(0,self.hidden-1)
        def handle_data(self,data):
            if not self.hidden:self.parts.append(data)
    visited=set()
    response=None
    for hop in range(4):
        canonical=canonical_source(url)
        if not canonical or canonical in visited:return ""
        visited.add(canonical)
        parsed=urlsplit(url)
        transport=SafeModelTransport("https://"+parsed.netloc,deps,max_bytes=512000,
            resolver=public_resolver,allow_redirect_response=True)
        async with httpx2.AsyncClient(transport=transport,timeout=15,follow_redirects=False,trust_env=False) as client:
            response=await client.get(url)
        if response.status_code in (301,302,303,307,308):
            location=response.headers.get("location","")
            if not location or hop==3:return ""
            url=urljoin(url,location)
            continue
        break
    if response is None or response.status_code!=200:return ""
    content_type=response.headers.get("content-type","").lower()
    is_pdf="application/pdf" in content_type or parsed.path.lower().endswith(".pdf")
    if is_pdf:
        try:
            import pypdfium2 as pdfium
            document=pdfium.PdfDocument(response.content)
            parts=[]
            for index in range(min(len(document),80)):
                page=document[index]
                text_page=page.get_textpage()
                parts.append(text_page.get_text_range())
                text_page.close()
                page.close()
            document.close()
            text="\n".join(parts)
            return " ".join(text.split())[:16000]
        except Exception:
            return ""
    if not any(kind in content_type for kind in ("text/html","text/plain")):return ""
    parser=Text();parser.feed(response.text[:300000])
    return " ".join(" ".join(parser.parts).split())[:16000]
