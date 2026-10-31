from django.db import models


class HealthArticle(models.Model):
    """
    Pet health information article managed by Admin.
    Read-only for Pet Owners — only published articles are visible to them.
    """

    # ── Relationships ────────────────────────────────────────────────────────
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="health_articles",
    )

    # ── Core fields ──────────────────────────────────────────────────────────
    title = models.CharField(
        max_length=100, help_text="Condition name, e.g. Bordetella"
    )
    description = models.TextField(help_text="General overview")
    causes = models.TextField(blank=True)
    symptoms = models.TextField(blank=True)
    general_care = models.TextField(blank=True, help_text="Treatment guidance")

    # ── Flags ────────────────────────────────────────────────────────────────
    is_published = models.BooleanField(
        default=False,
        help_text="Unpublished articles are hidden from pet owners.",
    )

    # ── Timestamps ───────────────────────────────────────────────────────────
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title
