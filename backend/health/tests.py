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
        # Slice 8b-1: the first sentence is bold, so the message is split
        # by the <strong> tag (was one string: "Published Dog&#x27;s cough.
        # Pet owners can read it now.")
        self.assertContains(
            response,
            '<strong class="font-semibold">Published Dog&#x27;s cough.</strong> '
            "Pet owners can read it now.",
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
        # The whole page: the layout no longer includes shared/modals.html
        self.assertNotIn("onclick", full)
        self.assertNotIn("openConfirmModal", full)
        self.assertIn(
            reverse("admin_health_article_toggle_publish", args=[article.pk]), full
        )


OLD_ASSETS = ("article_list.js", "article_list.css", "article_form.css")


class ListPageMarkupTests(HealthTestBase):
    """Slice 7b-1: the list page's dialogs, escaping and old-file checks."""

    url = "/admin/health/"

    def main_html(self, response):
        """The page's own <main> markup."""
        full = response.content.decode()
        start = full.index('<main id="main-content"')
        return full[start : full.index("</main>", start)]

    def test_one_dialog_per_article_with_the_right_toggle_url_and_wording(self):
        live = self.article("Live one", published=True)
        draft = self.article("Draft one")
        response = self.client.get(self.url)
        html = self.main_html(response)
        self.assertEqual(html.count("<dialog "), 2)
        live_url = reverse("admin_health_article_toggle_publish", args=[live.pk])
        draft_url = reverse("admin_health_article_toggle_publish", args=[draft.pk])
        # Published article: Unpublish dialog.
        unpublish = html[html.index(f'id="unpublish-dialog-{live.pk}"') :]
        unpublish = unpublish[: unpublish.index("</dialog>")]
        self.assertIn(f'action="{live_url}"', unpublish)
        self.assertIn("csrfmiddlewaretoken", unpublish)
        self.assertIn("Unpublish “Live one”?", unpublish)
        self.assertIn(
            "Pet owners will no longer see this article. It stays in the list, "
            "and you can publish it again at any time.",
            unpublish,
        )
        self.assertIn("Keep it published", unpublish)
        self.assertIn("Yes, unpublish", unpublish)
        self.assertIn("btn-danger", unpublish)
        # Draft: Publish dialog.
        publish = html[html.index(f'id="publish-dialog-{draft.pk}"') :]
        publish = publish[: publish.index("</dialog>")]
        self.assertIn(f'action="{draft_url}"', publish)
        self.assertIn("csrfmiddlewaretoken", publish)
        self.assertIn("Publish “Draft one”?", publish)
        self.assertIn("Pet owners will be able to read it.", publish)
        self.assertIn("Go back", publish)
        self.assertIn("Yes, publish", publish)
        # The row buttons open the matching dialog.
        self.assertIn(f'data-dialog-open="unpublish-dialog-{live.pk}"', html)
        self.assertIn(f'data-dialog-open="publish-dialog-{draft.pk}"', html)

    def test_title_with_apostrophe_and_quotes_is_escaped(self):
        title = 'Bantay\'s "first" visit'
        self.article(title, published=True)
        self.article(title + " two")
        response = self.client.get(self.url)
        html = self.main_html(response)
        escaped = "Bantay&#x27;s &quot;first&quot; visit"
        self.assertIn(f"Unpublish “{escaped}”?", html)
        self.assertIn(f"Publish “{escaped} two”?", html)
        self.assertIn(
            f'<p class="text-lg font-semibold text-gray-900">{escaped}</p>', html
        )
        self.assertNotIn(title, html)

    def test_no_onclick_and_no_openconfirmmodal_in_the_page_content(self):
        self.article("Live one", published=True)
        self.article("Draft one")
        # The whole page, not just <main>: the layout has no shared modal now
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("onclick", html)
        self.assertNotIn("openConfirmModal", html)

    def test_empty_and_no_results_states(self):
        response = self.client.get(self.url)
        self.assertContains(response, "No health articles yet")
        self.article("Kennel cough")
        response = self.client.get(self.url, {"q": "zzz"})
        self.assertContains(response, "No articles match your search")
        self.assertNotContains(response, "No health articles yet")

    def test_list_and_form_pages_load_none_of_the_old_files(self):
        article = self.article("Live one", published=True)
        pages = [
            self.url,
            reverse("admin_health_article_create"),
            reverse("admin_health_article_edit", args=[article.pk]),
        ]
        for url in pages:
            response = self.client.get(url)
            for name in OLD_ASSETS:
                with self.subTest(url=url, asset=name):
                    self.assertNotContains(response, name)


class FormPageMarkupTests(HealthTestBase):
    """Slice 7b-1: the form page has no Published field, and says the status."""

    def test_new_article_page_shows_the_draft_line_and_no_published_field(self):
        response = self.client.get(reverse("admin_health_article_create"))
        self.assertNotIn("is_published", response.context["form"].fields)
        self.assertNotContains(response, 'name="is_published"')
        self.assertNotContains(response, 'type="checkbox"')
        self.assertContains(response, "This article is a <b>draft</b>.")
        self.assertNotContains(response, "This article is <b>published</b>.")
        self.assertContains(response, "Save changes")
        self.assertContains(response, "Cancel")

    def test_edit_page_shows_the_status_line_for_draft_and_published(self):
        draft = self.article("Draft one")
        live = self.article("Live one", published=True)
        response = self.client.get(
            reverse("admin_health_article_edit", args=[draft.pk])
        )
        self.assertContains(response, "This article is a <b>draft</b>.")
        response = self.client.get(reverse("admin_health_article_edit", args=[live.pk]))
        self.assertContains(response, "This article is <b>published</b>.")
        self.assertNotContains(response, 'name="is_published"')

    def test_labels_help_and_errors_come_from_the_form(self):
        response = self.client.post(
            reverse("admin_health_article_create"), {"title": "", "description": ""}
        )
        self.assertContains(response, "Condition name")
        self.assertContains(response, "This is the title pet owners see.")
        self.assertContains(response, "Type the condition name.")
        self.assertNotContains(response, "Condition Name")
        self.assertNotContains(response, "General Care")


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
