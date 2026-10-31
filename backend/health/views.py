from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .forms import HealthArticleForm
from .models import HealthArticle

# ── Admin — Health Library ──────────────────────────────────────────────────


@login_required
def admin_health_article_list(request):
    """
    Admin view — list all health articles (published and unpublished).
    Searchable by title (condition name).
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    query = request.GET.get("q", "").strip()
    articles = HealthArticle.objects.all()

    if query:
        articles = articles.filter(title__icontains=query)

    return render(
        request,
        "admin/health/article_list.html",
        {"articles": articles, "query": query},
    )


@login_required
def admin_health_article_create(request):
    """Admin view — create a new health article."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    if request.method == "POST":
        form = HealthArticleForm(request.POST)
        if form.is_valid():
            article = form.save(commit=False)
            article.created_by = request.user
            article.save()
            messages.success(request, f"Article '{article.title}' created.")
            return redirect("admin_health_article_list")
        else:
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
    """Admin view — edit an existing health article."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    article = get_object_or_404(HealthArticle, pk=pk)

    if request.method == "POST":
        form = HealthArticleForm(request.POST, instance=article)
        if form.is_valid():
            form.save()
            messages.success(request, f"Article '{article.title}' updated.")
            return redirect("admin_health_article_list")
        else:
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
    Admin action — toggle an article's published status.
    Non-destructive, matches the soft-delete/unpublish pattern used
    elsewhere (e.g. TestResultFile.is_archived).
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    article = get_object_or_404(HealthArticle, pk=pk)

    if request.method == "POST":
        article.is_published = not article.is_published
        article.save()
        status_label = "published" if article.is_published else "unpublished"
        messages.success(request, f"Article '{article.title}' {status_label}.")

    return redirect("admin_health_article_list")


@login_required
def admin_health_article_delete(request, pk):
    """
    Admin action — permanently delete a health article.
    True hard delete, by explicit decision — HealthArticle has no
    foreign-key dependents, so this is safe at the database level.
    A JS confirmation dialog is deferred to the 7.6 UI polish pass;
    for now this fires immediately on POST.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    article = get_object_or_404(HealthArticle, pk=pk)

    if request.method == "POST":
        title = article.title
        article.delete()
        messages.success(request, f"Article '{title}' permanently deleted.")

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
