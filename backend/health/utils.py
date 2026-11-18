from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models.functions import Lower
from django.urls import reverse

from appointments.utils import paging_links
from .models import HealthArticle

OWNER_PAGE_SIZE = 25
SUMMARY_LENGTH = 110


def article_summary(article, length=SUMMARY_LENGTH):
    """One line for the owner's list: the start of the first part that has
    text (overview, causes, signs, care), cut at a word boundary with "…"
    when it is longer than length. The model has no summary field."""
    parts = (
        article.description,
        article.causes,
        article.symptoms,
        article.general_care,
    )
    for part in parts:
        text = " ".join(part.split())
        if not text:
            continue
        if len(text) <= length:
            return text
        head = text[: length + 1]
        cut = head.rsplit(" ", 1)[0] if " " in head else text[:length]
        return f"{cut}…"
    return ""


def article_sections(article):
    """The article's non-empty parts as (heading, text), in reading order."""
    parts = (
        ("What it is", article.description),
        ("Causes", article.causes),
        ("Signs to watch for", article.symptoms),
        ("General care", article.general_care),
    )
    return [(heading, text) for heading, text in parts if text.strip()]


def owner_health_list_context(params):
    """The owner's Health library list: published articles only, A to Z by
    title, ?q= (every word must be in the title), 25 a page. 2 queries, plus
    one when nothing is shown, to tell "no match" from "no guides at all"."""
    q = " ".join(params.get("q", "").split())
    articles = HealthArticle.objects.filter(is_published=True)
    for word in q.split():
        articles = articles.filter(title__icontains=word)
    articles = articles.order_by(Lower("title"), "pk")
    page_obj = Paginator(articles, OWNER_PAGE_SIZE).get_page(params.get("page"))
    rows = list(page_obj.object_list)
    for article in rows:
        article.summary = article_summary(article)
    if rows or not q:
        has_guides = bool(rows)
    else:
        has_guides = HealthArticle.objects.filter(is_published=True).exists()
    return {
        "articles": rows,
        "query": q,
        "q": q,
        "page_obj": page_obj,
        "has_guides": has_guides,
        **paging_links(page_obj, urlencode({"q": q}) if q else ""),
    }


def owner_health_back_url(params):
    """Back to the list, keeping the search the owner came from."""
    q = " ".join(params.get("q", "").split())
    url = reverse("owner_health_article_list")
    return f"{url}?{urlencode({'q': q})}" if q else url
