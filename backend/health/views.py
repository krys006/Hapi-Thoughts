from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render

from .forms import HealthArticleForm
from .models import HealthArticle

# ── Admin — Health Library ──────────────────────────────────────────────────


@login_required
def admin_health_article_list(request):
    """
    Admin view: list all health articles (published and drafts), newest
    update first. ?q= matches the title; every word must appear in it.
    The three counts cover all articles and ignore the search.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    query = request.GET.get("q", "").strip()
    articles = HealthArticle.objects.order_by("-updated_at", "-pk")
    for word in query.split():
        articles = articles.filter(title__icontains=word)

    counts = HealthArticle.objects.aggregate(
        total=Count("pk"),
        published=Count("pk", filter=Q(is_published=True)),
    )
    return render(
        request,
        "admin/health/article_list.html",
        {
            "articles": articles,
            "query": query,
            "q": query,
            "is_searching": bool(query),
            "total_count": counts["total"],
            "published_count": counts["published"],
            "draft_count": counts["total"] - counts["published"],
        },
    )


@login_required
def admin_health_article_create(request):
    """Admin view: create a health article. It always starts as a draft."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    if request.method == "POST":
        form = HealthArticleForm(request.POST)
        if form.is_valid():
            article = form.save(commit=False)
            article.created_by = request.user
            article.is_published = False
            article.save()
            messages.success(request, "Saved as a draft. Publish it when it's ready.")
            return redirect("admin_health_article_list")
        messages.error(request, "Please correct the errors below.")
    else:
        form = HealthArticleForm()

    return render(
        request,
        "admin/health/article_form.html",
        {"form": form, "is_edit": False},
    )


@login_required
def admin_health_article_edit(request, pk):
    """Admin view: edit an article. The form never changes is_published."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    article = get_object_or_404(HealthArticle, pk=pk)

    if request.method == "POST":
        form = HealthArticleForm(request.POST, instance=article)
        if form.is_valid():
            form.save()
            messages.success(request, f"{article.title} saved.")
            return redirect("admin_health_article_list")
        messages.error(request, "Please correct the errors below.")
    else:
        form = HealthArticleForm(instance=article)

    return render(
        request,
        "admin/health/article_form.html",
        {"form": form, "is_edit": True, "article": article},
    )


@login_required
def admin_health_article_toggle_publish(request, pk):
    """
    Admin action: publish or unpublish an article (POST only; a GET just
    goes back to the list). Articles are never deleted, only unpublished.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    article = get_object_or_404(HealthArticle, pk=pk)

    if request.method == "POST":
        article.is_published = not article.is_published
        article.save()
        if article.is_published:
            messages.success(
                request, f"Published {article.title}. Pet owners can read it now."
            )
        else:
            messages.success(
                request,
                f"Unpublished {article.title}. Pet owners can't see it any more.",
            )

    return redirect("admin_health_article_list")


# ── Pet Owner — Health Library ───────────────────────────────────────────────


@login_required
def owner_health_article_list(request):
    """
    Pet Owner view — browse published health articles.
    Searchable by title. Unpublished articles never appear, regardless
    of search query.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    query = request.GET.get("q", "").strip()
    articles = HealthArticle.objects.filter(is_published=True)

    if query:
        articles = articles.filter(title__icontains=query)

    return render(
        request,
        "owner/health/article_list.html",
        {"articles": articles, "query": query},
    )


@login_required
def owner_health_article_detail(request, pk):
    """
    Pet Owner view — read a single published article.
    404s if the article is unpublished, even with a guessed/valid pk —
    unpublished articles are strictly admin-only.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    article = get_object_or_404(HealthArticle, pk=pk, is_published=True)

    return render(request, "owner/health/article_detail.html", {"article": article})
