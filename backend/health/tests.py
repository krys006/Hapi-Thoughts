from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from .models import HealthArticle

User = get_user_model()

ADMIN_URLS = [
    ("admin_health_article_list", None),
    ("admin_health_article_create", None),
    ("admin_health_article_edit", "pk"),
    ("admin_health_article_toggle_publish", "pk"),
]


class HealthTestBase(TestCase):
    """An admin, a pet owner and helpers; the admin is logged in."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="unused-pass-123",
            role=User.ADMIN,
            is_onboarded=True,
        )
        self.owner = User.objects.create_user(
            username="owner",
            email="owner@example.com",
            password="unused-pass-123",
            role=User.PET_OWNER,
            is_onboarded=True,
        )
        self.client.force_login(self.admin)

    def article(self, title="Kennel cough", published=False, **extra):
        return HealthArticle.objects.create(
            title=title,
            description="Overview.",
            is_published=published,
            created_by=self.admin,
            **extra,
        )

    def flashes(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]


class RoleTests(HealthTestBase):
    def test_owner_is_redirected_from_every_admin_health_url(self):
        article = self.article()
        self.client.force_login(self.owner)
        for name, needs_pk in ADMIN_URLS:
            url = reverse(name, args=[article.pk] if needs_pk else [])
            for method in (self.client.get, self.client.post):
                with self.subTest(name=name, method=method.__name__):
                    response = method(url)
                    self.assertRedirects(
                        response,
                        reverse("owner_dashboard"),
                        fetch_redirect_response=False,
                    )
        article.refresh_from_db()
        self.assertFalse(article.is_published)

    def test_admin_can_open_list_create_and_edit(self):
        article = self.article()
        for name, args in [
            ("admin_health_article_list", []),
            ("admin_health_article_create", []),
            ("admin_health_article_edit", [article.pk]),
        ]:
            with self.subTest(name=name):
                self.assertEqual(
                    self.client.get(reverse(name, args=args)).status_code, 200
                )


class DeleteRemovedTests(HealthTestBase):
    def test_delete_url_name_no_longer_exists(self):
        with self.assertRaises(NoReverseMatch):
            reverse("admin_health_article_delete", args=[1])

    def test_post_to_old_delete_path_is_404_and_keeps_the_article(self):
        article = self.article()
        response = self.client.post(f"/admin/health/{article.pk}/delete/")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(HealthArticle.objects.filter(pk=article.pk).exists())


class TogglePublishTests(HealthTestBase):
    def url(self, article):
        return reverse("admin_health_article_toggle_publish", args=[article.pk])

    def test_post_publishes_a_draft(self):
        article = self.article("Rabies basics")
        response = self.client.post(self.url(article))
        self.assertRedirects(
            response,
            reverse("admin_health_article_list"),
            fetch_redirect_response=False,
        )
        article.refresh_from_db()
        self.assertTrue(article.is_published)
        self.assertEqual(
            self.flashes(response),
            ["Published Rabies basics. Pet owners can read it now."],
        )

    def test_post_unpublishes_a_published_article(self):
        article = self.article("Rabies basics", published=True)
        response = self.client.post(self.url(article))
        article.refresh_from_db()
        self.assertFalse(article.is_published)
        self.assertEqual(
            self.flashes(response),
            ["Unpublished Rabies basics. Pet owners can't see it any more."],
        )

    def test_get_changes_nothing_and_redirects_to_the_list(self):
        for published in (False, True):
            with self.subTest(published=published):
                article = self.article(f"A{published}", published=published)
                response = self.client.get(self.url(article))
                self.assertRedirects(
                    response,
                    reverse("admin_health_article_list"),
                    fetch_redirect_response=False,
                )
                article.refresh_from_db()
                self.assertEqual(article.is_published, published)
                self.assertEqual(self.flashes(response), [])

    def test_apostrophe_title_works_and_is_escaped_in_the_flash(self):
        article = self.article("Dog's cough")
        response = self.client.post(self.url(article), follow=True)
        article.refresh_from_db()
        self.assertTrue(article.is_published)
        self.assertContains(
            response, "Published Dog&#x27;s cough. Pet owners can read it now."
        )
        self.assertNotContains(response, "Published Dog's cough")


class CreateTests(HealthTestBase):
    url = "/admin/health/create/"

    def test_posted_is_published_is_ignored_and_it_saves_a_draft(self):
        response = self.client.post(
            self.url,
            {"title": "Mange", "description": "Skin.", "is_published": "on"},
        )
        self.assertRedirects(
            response,
            reverse("admin_health_article_list"),
            fetch_redirect_response=False,
        )
        article = HealthArticle.objects.get()
        self.assertFalse(article.is_published)
        self.assertEqual(article.created_by, self.admin)
        self.assertEqual(
            self.flashes(response), ["Saved as a draft. Publish it when it's ready."]
        )

    def test_missing_title_shows_the_message_and_saves_nothing(self):
        response = self.client.post(self.url, {"title": "", "description": "Skin."})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Type the condition name.")
        self.assertEqual(HealthArticle.objects.count(), 0)

    def test_form_has_no_published_field_and_new_labels(self):
        response = self.client.get(self.url)
        self.assertNotIn("is_published", response.context["form"].fields)
        self.assertNotContains(response, "is_published")
        form = response.context["form"]
        self.assertEqual(form.fields["title"].label, "Condition name")
        self.assertEqual(
            form.fields["title"].help_text, "This is the title pet owners see."
        )
        self.assertEqual(form.fields["description"].label, "Description")
        self.assertEqual(
            form.fields["description"].help_text,
            "One or two sentences. Shown on the list.",
        )
        self.assertEqual(form.fields["general_care"].label, "General care")
        for name in ("causes", "symptoms", "general_care"):
            self.assertFalse(form.fields[name].required)


class EditTests(HealthTestBase):
    def edit(self, article, **extra):
        data = {"title": "New title", "description": "New text."}
        data.update(extra)
        return self.client.post(
            reverse("admin_health_article_edit", args=[article.pk]), data
        )

    def check_edit_keeps(self, published):
        article = self.article("Old title", published=published)
        response = self.edit(article)
        article.refresh_from_db()
        self.assertEqual(article.is_published, published)
        self.assertEqual(article.title, "New title")
        self.assertEqual(self.flashes(response), ["New title saved."])

    def test_edit_keeps_a_draft_a_draft(self):
        self.check_edit_keeps(False)

    def test_edit_keeps_a_published_article_published(self):
        self.check_edit_keeps(True)

    def test_posted_is_published_is_ignored(self):
        draft = self.article("Draft")
        self.edit(draft, is_published="on")
        draft.refresh_from_db()
        self.assertFalse(draft.is_published)
        live = self.article("Live", published=True)
        self.edit(live, is_published="")
        live.refresh_from_db()
        self.assertTrue(live.is_published)

    def test_edit_redirects_to_the_list(self):
        response = self.edit(self.article())
        self.assertRedirects(
            response,
            reverse("admin_health_article_list"),
            fetch_redirect_response=False,
        )


class AdminListTests(HealthTestBase):
    url = "/admin/health/"

    def test_order_is_newest_update_first_then_highest_pk(self):
        a = self.article("A")
        b = self.article("B")
        c = self.article("C")
        # Same updated_at for b and c: the higher pk goes first.
        HealthArticle.objects.filter(pk=a.pk).update(updated_at="2026-11-10T10:00:00Z")
        HealthArticle.objects.filter(pk__in=[b.pk, c.pk]).update(
            updated_at="2026-11-12T10:00:00Z"
        )
        HealthArticle.objects.filter(pk=a.pk).update(updated_at="2026-11-15T10:00:00Z")
        response = self.client.get(self.url)
        self.assertEqual(list(response.context["articles"]), [a, c, b])

    def test_search_one_word_two_words_and_no_match(self):
        cough = self.article("Dog's kennel cough")
        self.article("Cat flu")
        found = self.client.get(self.url, {"q": "COUGH"}).context["articles"]
        self.assertEqual(list(found), [cough])
        found = self.client.get(self.url, {"q": "cough dog"}).context["articles"]
        self.assertEqual(list(found), [cough])
        found = self.client.get(self.url, {"q": "cough cat"}).context["articles"]
        self.assertEqual(list(found), [])
        response = self.client.get(self.url, {"q": "zzz"})
        self.assertEqual(list(response.context["articles"]), [])
        self.assertTrue(response.context["is_searching"])

    def test_counts_ignore_the_search(self):
        self.article("A", published=True)
        self.article("B", published=True)
        self.article("C")
        for params in ({}, {"q": "A"}, {"q": "nothing"}):
            with self.subTest(params=params):
                ctx = self.client.get(self.url, params).context
                self.assertEqual(ctx["total_count"], 3)
                self.assertEqual(ctx["published_count"], 2)
                self.assertEqual(ctx["draft_count"], 1)

    def test_context_keys(self):
        self.article("A")
        ctx = self.client.get(self.url, {"q": " a "}).context
        for key in (
            "articles",
            "query",
            "q",
            "is_searching",
            "total_count",
            "published_count",
            "draft_count",
        ):
            self.assertIn(key, ctx)
        self.assertEqual(ctx["q"], "a")
        self.assertEqual(ctx["query"], "a")
        self.assertTrue(ctx["is_searching"])
        self.assertFalse(self.client.get(self.url).context["is_searching"])

    def test_page_has_no_delete_url_and_no_onclick(self):
        article = self.article("Dog's cough", published=True)
        self.article("Draft one")
        full = self.client.get(self.url).content.decode()
        self.assertNotIn("/delete/", full)
        # The layout still includes shared/modals.html (its own onclick,
        # removed in 7b-5), so check the page's own content only: from the
        # page wrapper to the end of the article table.
        start = full.index('<div class="admin-page">')
        html = full[start : full.index("</table>", start)]
        self.assertNotIn("onclick", html)
        self.assertNotIn("openConfirmModal", html)
        self.assertIn(
            reverse("admin_health_article_toggle_publish", args=[article.pk]), html
        )


class OwnerSideTests(HealthTestBase):
    def setUp(self):
        super().setUp()
        self.live = self.article("Live kennel cough", published=True)
        self.draft = self.article("Draft kennel cough")
        self.client.force_login(self.owner)

    def test_list_shows_only_published(self):
        response = self.client.get(reverse("owner_health_article_list"))
        self.assertEqual(list(response.context["articles"]), [self.live])

    def test_detail_of_a_draft_is_404(self):
        response = self.client.get(
            reverse("owner_health_article_detail", args=[self.draft.pk])
        )
        self.assertEqual(response.status_code, 404)
        response = self.client.get(
            reverse("owner_health_article_detail", args=[self.live.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_search_never_finds_a_draft(self):
        response = self.client.get(
            reverse("owner_health_article_list"), {"q": "kennel"}
        )
        self.assertEqual(list(response.context["articles"]), [self.live])
        self.assertNotContains(response, "Draft kennel cough")
