import asyncio
import math
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.models import Knowledge, KnowledgeChunk
from app.services.retrieval import tokens


def chunks(text, size=700, overlap=100):
    text = text.strip()
    return [text[i : i + size] for i in range(0, len(text), size - overlap)]


def cosine(first, second):
    if len(first) != len(second) or not first:
        return 0.0
    den = math.sqrt(sum(x * x for x in first) * sum(x * x for x in second))
    return sum(a * b for a, b in zip(first, second)) / den if den else 0.0


class KnowledgeService:
    """Small local RAG: chunk vectors in PostgreSQL JSONB, bounded cosine search.

    No extension is required; this is not a pgvector ANN index.
    """

    def __init__(self, db, transport=None):
        self.db, self.settings, self.transport = db, db.settings, transport
        self.model = (
            self.settings.ark_embedding_model
            if self.settings.embedding_provider == "volcengine"
            else self.settings.embedding_model
        )

    async def embed(self, texts):
        from app.core.embedding_client import Embedder

        return await Embedder(self.settings, self.transport).embed(texts)

    async def index(self, document, *, replacement=None, expected=None):
        expected = expected or {
            k: getattr(document, k) for k in ("title", "body", "category", "source_url")
        }
        parts = chunks(document.body)
        vectors = (
            await self.embed(parts)
            if self.settings.embedding_provider != "disabled"
            else [None] * len(parts)
        )
        async with self.db.sessions() as session, session.begin():
            current = await session.scalar(
                select(Knowledge).where(Knowledge.id == document.id).with_for_update()
            )
            if current is None or current.owner_id != document.owner_id:
                raise HTTPException(404, "素材不存在")
            if any(getattr(current, k) != v for k, v in expected.items()):
                raise HTTPException(409, "素材已被其他操作修改，请刷新后重试")
            if replacement:
                for key, value in replacement.items():
                    setattr(current, key, value)
            await session.execute(
                delete(KnowledgeChunk).where(KnowledgeChunk.knowledge_id == document.id)
            )
            session.add_all(
                [
                    KnowledgeChunk(
                        knowledge_id=document.id,
                        owner_id=document.owner_id,
                        ordinal=i,
                        body=part,
                        embedding=vector,
                        embedding_model=self.model if vector else None,
                    )
                    for i, (part, vector) in enumerate(zip(parts, vectors))
                ]
            )
        return {"chunks": len(parts), "mode": "vector" if any(vectors) else "lexical"}

    async def search(self, owner_id, category, query, limit=5, session=None):
        async def run(session):
            rows = (
                await session.execute(
                    select(KnowledgeChunk, Knowledge)
                    .join(Knowledge)
                    .where(Knowledge.owner_id == owner_id, Knowledge.category == category)
                    .order_by(Knowledge.created_at.desc(), KnowledgeChunk.ordinal)
                    .limit(2000)
                )
            ).all()
            # Existing materials remain usable before their first explicit reindex.
            documents = (
                await session.scalars(
                    select(Knowledge)
                    .where(Knowledge.owner_id == owner_id, Knowledge.category == category)
                    .order_by(Knowledge.created_at.desc())
                    .limit(200)
                )
            ).all()
            indexed = {doc.id for _, doc in rows}
            candidates = [
                {
                    "id": c.id,
                    "title": d.title,
                    "body": c.body,
                    "source_url": d.source_url,
                    "embedding": c.embedding,
                    "model": c.embedding_model,
                }
                for c, d in rows
            ]
            candidates += [
                {
                    "id": d.id,
                    "title": d.title,
                    "body": d.body[:4000],
                    "source_url": d.source_url,
                    "embedding": None,
                    "model": None,
                }
                for d in documents
                if d.id not in indexed
            ]
            use_vectors = self.settings.embedding_provider != "disabled" and any(
                c["embedding"] and c["model"] == self.model for c in candidates
            )
            vector = (await self.embed([query]))[0] if use_vectors else None
            bag = tokens(query)
            ranked = []
            for c in candidates:
                words = tokens(c["title"] + " " + c["body"])
                lexical = sum(min(n, words.get(w, 0)) for w, n in bag.items()) / max(
                    sum(bag.values()), 1
                )
                semantic = (
                    cosine(vector, c["embedding"])
                    if vector and c["embedding"] and c["model"] == self.model
                    else 0
                )
                score = 0.7 * max(semantic, 0) + 0.3 * lexical if use_vectors else lexical
                if score > 0:
                    ranked.append(({k: c[k] for k in ("id", "title", "body", "source_url")}, score))
            selected = sorted(ranked, key=lambda item: item[1], reverse=True)[:limit]
            return {
                "mode": "vector_hybrid" if use_vectors else "lexical",
                "items": [{**c, "score": round(score, 4)} for c, score in selected],
            }

        if session is not None:
            return await run(session)
        async with self.db.sessions() as current:
            return await run(current)


async def public_url(url):
    import ipaddress
    import socket

    target = urlparse(url)
    if (
        target.scheme != "https"
        or not target.hostname
        or target.username
        or target.password
        or target.port not in (None, 443)
    ):
        raise HTTPException(422, "只支持公开HTTPS网页，不能带账号口令")
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, target.hostname, 443)
    except OSError:
        raise HTTPException(422, "网页主机无法解析") from None
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise HTTPException(422, "不能采集本机或私有网络地址")


class ArticleHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip, self.title, self.in_title = [], 0, [], False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav", "footer", "header", "noscript"}:
            self.skip += 1
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "footer", "header", "noscript"}:
            self.skip = max(0, self.skip - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, value):
        if self.in_title:
            self.title.append(value.strip())
        elif not self.skip and value.strip():
            self.parts.append(value.strip())


async def import_article(url):
    await public_url(url)
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        async with client.stream(
            "GET", url, headers={"User-Agent": "MediaDemo/0.2 (user-requested article import)"}
        ) as response:
            if response.status_code != 200 or not any(
                t in response.headers.get("content-type", "") for t in ("text/html", "text/plain")
            ):
                raise HTTPException(422, "网页不支持直接读取；可粘贴有权使用的正文，不自动跟随跳转")
            content = bytearray()
            async for part in response.aiter_bytes():
                content.extend(part)
                if len(content) > 2_000_000:
                    raise HTTPException(422, "网页超过2MB")
    parser = ArticleHTML()
    parser.feed(bytes(content).decode(response.encoding or "utf-8", errors="replace"))
    body = "\n\n".join(parser.parts)[:12000]
    if not body:
        raise HTTPException(422, "未读取到正文，请粘贴内容")
    return {"title": "".join(parser.title)[:200] or "导入网页", "body": body, "source_url": url}
