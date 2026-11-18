import re

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.template.loader import get_template
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from .models import HealthArticle
from .utils import article_summary

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


def _main(response):
    """The page's <main> markup."""
    return response.content.decode().split('<main id="main"')[1].split("</main>")[0]


def _sources(names):
    """(name, template source) for each template name."""
    return [(name, get_template(name).template.source) for name in names]


class ArticleSummaryTests(TestCase):
    """Slice 8b-9b: the one-line summary on the list cards (no summary field)."""

    def _article(self, **parts):
        fields = {"title": "T", "description": "", **parts}
        return HealthArticle(**fields)

    def test_a_short_text_is_kept_whole(self):
        article = self._article(description="A short one.")
        self.assertEqual(article_summary(article), "A short one.")

    def test_a_long_text_is_cut_on_a_word_boundary_with_an_ellipsis(self):
        text = "word " * 60
        summary = article_summary(self._article(description=text))
        self.assertTrue(summary.endswith("word…"))
        self.assertLessEqual(len(summary), 111)
        self.assertGreater(len(summary), 100)

    def test_an_empty_overview_falls_back_to_the_next_part(self):
        article = self._article(causes="  ", symptoms="Sneezing\n and coughing.")
        self.assertEqual(article_summary(article), "Sneezing and coughing.")

    def test_no_text_at_all_is_empty(self):
        self.assertEqual(article_summary(self._article()), "")

    def test_one_very_long_word_is_cut_hard(self):
        summary = article_summary(self._article(description="x" * 300))
        self.assertEqual(summary, "x" * 110 + "…")


class OwnerHealthListPageTests(HealthTestBase):
    """Slice 8b-9b: the owner's Health library list on layouts/owner.html."""

    PAGE_QUERIES = 4
    PARTS = (
        "owner/health/article_list.html",
        "owner/health_parts/search.html",
        "owner/health_parts/card.html",
        "owner/health_parts/pager.html",
    )

    def setUp(self):
        super().setUp()
        self.rabies = HealthArticle.objects.create(
            title="Rabies",
            description="A deadly virus spread by bites.",
            is_published=True,
        )
        self.draft = HealthArticle.objects.create(
            title="Draft secret", description="Hidden.", is_published=False
        )
        self.url = reverse("owner_health_article_list")
        self.client.force_login(self.owner)

    def _main(self, **params):
        return _main(self.client.get(self.url, params))

    def test_layout_account_tab_and_back_bar(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "layouts/owner.html")
        self.assertTemplateNotUsed(response, "base_dashboard.html")
        html = response.content.decode()
        back = reverse("owner_account")
        self.assertRegex(html, rf'(?s)<a href="{back}"[^>]*>.*?Back to Account</a>')
        self.assertRegex(html, rf'<a href="{back}" class="[^"]*" aria-current="page">')
        self.assertIn(">Health library</h1>", html)
        self.assertIn("Short guides from the clinic about common illnesses.", html)

    def test_only_published_articles_never_a_draft_title(self):
        draft_link = reverse("owner_health_article_detail", args=[self.draft.pk])
        for params in ({}, {"q": "draft"}, {"q": "secret"}, {"q": "Draft secret"}):
            with self.subTest(params=params):
                response = self.client.get(self.url, params)
                # (the typed search is echoed in the input, so check the card)
                self.assertNotContains(response, draft_link)
                self.assertNotIn(self.draft, response.context["articles"])
        self.assertNotContains(self.client.get(self.url), "Draft secret")
        self.assertEqual(self.client.get(self.url).context["articles"], [self.rabies])

    def test_the_search_form(self):
        main = self._main()
        self.assertIn('<form method="get" action="' + self.url + '"', main)
        self.assertIn('<span class="sr-only">Search by illness name</span>', main)
        self.assertIn('type="search" name="q" value=""', main)
        self.assertIn('placeholder="Search by illness, e.g. rabies"', main)
        button = r'(?s)<button type="submit"[^>]*min-h-12[^>]*>.*?Search</button>'
        self.assertRegex(main, button)
        self.assertNotIn("Clear search", main)

    def test_q_matches_every_word_and_is_echoed_in_the_input(self):
        HealthArticle.objects.create(
            title="Bordetella (kennel cough)", description="x", is_published=True
        )
        response = self.client.get(self.url, {"q": "  kennel   BORDETELLA "})
        titles = [a.title for a in response.context["articles"]]
        self.assertEqual(titles, ["Bordetella (kennel cough)"])
        main = _main(response)
        self.assertIn('name="q" value="kennel BORDETELLA"', main)
        self.assertNotIn("Rabies", main)
        self.assertIn("Clear search</a>", main)

    def test_a_search_is_escaped_in_the_input(self):
        main = self._main(q='"><script>alert(1)</script>')
        self.assertNotIn("<script>alert(1)", main)
        self.assertIn("&lt;script&gt;", main)

    def test_no_result_box_with_a_clear_search_link(self):
        main = self._main(q="zzz")
        self.assertIn("No guide about that yet", main)
        self.assertIn("Worried about your pet? Ask Dr. Aga at your next visit.", main)
        self.assertEqual(main.count("Clear search</a>"), 1)
        self.assertIn(f'<a href="{self.url}"', main)
        self.assertIn('name="q" value="zzz"', main)
        self.assertNotIn("No guides yet", main)

    def test_no_guides_at_all_is_the_empty_page_without_the_form(self):
        HealthArticle.objects.filter(is_published=True).delete()
        main = self._main()
        self.assertIn("No guides yet", main)
        self.assertIn("The clinic is still writing these. Check back soon.", main)
        self.assertIn(">Health library</h1>", main)
        self.assertNotIn("<form", main)
        self.assertNotIn('name="q"', main)
        # A search with nothing published is the same empty page, not "no match"
        self.assertNotIn("No guide about that yet", self._main(q="rabies"))

    def test_cards_link_to_the_article_with_title_summary_and_chevron(self):
        main = self._main()
        href = reverse("owner_health_article_detail", args=[self.rabies.pk])
        card = re.search(rf'(?s)<a href="{href}"[^>]*>.*?</a>', main).group(0)
        self.assertIn('class="block break-words text-lg font-bold', card)
        self.assertIn(">Rabies</span>", card)
        self.assertIn("A deadly virus spread by bites.", card)
        self.assertIn("bg-violet-50", card)
        self.assertIn("text-violet-700", card)
        self.assertEqual(card.count("<svg"), 2)  # the book tile and the chevron

    def test_a_card_keeps_the_search_for_the_articles_back_link(self):
        main = self._main(q="rabies")
        href = reverse("owner_health_article_detail", args=[self.rabies.pk])
        self.assertIn(f'<a href="{href}?q=rabies"', main)

    def test_alphabetical_by_title_ignoring_case(self):
        for title in ("banana flu", "Apple scab", "cherry pox"):
            HealthArticle.objects.create(
                title=title, description="x", is_published=True
            )
        titles = [a.title for a in self.client.get(self.url).context["articles"]]
        self.assertEqual(titles, ["Apple scab", "banana flu", "cherry pox", "Rabies"])

    def test_paged_at_25_and_the_pager_keeps_the_search(self):
        HealthArticle.objects.bulk_create(
            HealthArticle(title=f"Guide {n:02d}", description="x", is_published=True)
            for n in range(29)
        )
        response = self.client.get(self.url)
        self.assertEqual(len(response.context["articles"]), 25)
        main = _main(response)
        self.assertIn('aria-label="Pages of guides"', main)
        self.assertIn("Page 1 of 2", main)
        self.assertIn('<a href="?page=2"', main)
        second = self.client.get(self.url, {"page": 2})
        self.assertEqual(len(second.context["articles"]), 5)
        self.assertIn('<a href="?page=1"', _main(second))
        found = self.client.get(self.url, {"q": "guide"})
        self.assertIn('<a href="?q=guide&amp;page=2"', _main(found))

    def test_one_page_has_no_pager(self):
        self.assertNotIn("Pages of guides", self._main())

    def test_no_violet_button_on_the_page(self):
        for params in ({}, {"q": "rabies"}, {"q": "zzz"}):
            with self.subTest(params=params):
                main = self._main(**params)
                self.assertNotIn("bg-violet-600", main)
                self.assertNotIn("bg-violet-700", main)

    def test_every_link_is_on_one_line(self):
        for name, source in _sources(self.PARTS):
            for link in re.findall(r"(?s)<a\b.*?</a>", source):
                with self.subTest(file=name, link=link[:50]):
                    self.assertNotIn("\n", link)

    def test_no_inline_script_handler_or_html_comment(self):
        main = self._main()
        self.assertNotIn("<script", main)
        self.assertNotIn("onclick", main)
        self.assertNotIn("<!--", main)
        for name, source in _sources(self.PARTS):
            with self.subTest(file=name):
                self.assertNotIn("<!--", source)

    def test_query_total_is_flat_for_3_and_30_articles(self):
        for total in (3, 30):
            keep = [self.rabies.pk, self.draft.pk]
            HealthArticle.objects.exclude(pk__in=keep).delete()
            HealthArticle.objects.bulk_create(
                HealthArticle(title=f"Guide {n}", description="x", is_published=True)
                for n in range(total - 1)
            )
            with self.subTest(guides=total):
                with self.assertNumQueries(self.PAGE_QUERIES):
                    self.client.get(self.url)

    def test_the_admin_is_sent_away(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertRedirects(
            response, reverse("admin_dashboard"), fetch_redirect_response=False
        )


class OwnerHealthArticlePageTests(HealthTestBase):
    """Slice 8b-9b: one guide on layouts/owner.html (frame 7)."""

    PAGE_QUERIES = 3
    PARTS = ("owner/health/article_detail.html",)

    def setUp(self):
        super().setUp()
        self.article_ = HealthArticle.objects.create(
            title="Parvovirus",
            description="A contagious virus.\n\nPuppies are most at risk.",
            causes="",
            symptoms="Vomiting and being very tired.",
            general_care="  ",
            is_published=True,
        )
        self.url = reverse("owner_health_article_detail", args=[self.article_.pk])
        self.client.force_login(self.owner)

    def test_layout_account_tab_title_and_label(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "layouts/owner.html")
        self.assertTemplateNotUsed(response, "base_dashboard.html")
        html = response.content.decode()
        account = reverse("owner_account")
        lit = rf'<a href="{account}" class="[^"]*" aria-current="page">'
        self.assertRegex(html, lit)
        main = _main(response)
        self.assertIn('text-violet-700">Health library</p>', main)
        self.assertIn(
            '<h1 class="break-words text-3xl font-bold text-gray-900">Parvovirus</h1>',
            main,
        )

    HEADING = r'<h2 class="text-lg font-bold text-gray-900">(.*?)</h2>'

    def test_only_the_non_empty_parts_are_headings(self):
        main = _main(self.client.get(self.url))
        headings = re.findall(self.HEADING, main)
        self.assertEqual(headings, ["What it is", "Signs to watch for"])
        self.assertIn("<p>A contagious virus.</p>", main)
        self.assertIn("<p>Puppies are most at risk.</p>", main)
        self.assertIn("<p>Vomiting and being very tired.</p>", main)
        self.assertIn("text-base leading-7", main)

    def test_all_four_parts_use_the_real_names_in_order(self):
        self.article_.causes = "Contact with stool."
        self.article_.general_care = "See the vet."
        self.article_.save()
        main = _main(self.client.get(self.url))
        headings = re.findall(self.HEADING, main)
        self.assertEqual(
            headings, ["What it is", "Causes", "Signs to watch for", "General care"]
        )

    def test_the_text_is_autoescaped(self):
        self.article_.description = "<script>alert(1)</script> & <b>bold</b>"
        self.article_.save()
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("<script>alert(1)", html)
        self.assertNotIn("<b>bold</b>", html)
        escaped = "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;"
        self.assertIn(escaped, html)

    def test_the_not_a_diagnosis_box_and_the_one_violet_button(self):
        main = _main(self.client.get(self.url))
        self.assertIn(
            "This guide is not a diagnosis. If your pet shows these signs, "
            "book a visit or call the clinic.",
            main,
        )
        violet = re.findall(r"(?s)<a [^>]*bg-violet-600[^>]*>.*?</a>", main)
        self.assertEqual(len(violet), 1)
        self.assertIn(f'href="{reverse("owner_book_appointment")}"', violet[0])
        self.assertIn("Book a visit</a>", violet[0])
        self.assertIn("bg-violet-50", main)
        self.assertEqual(len(re.findall(r"<button", main)), 0)

    def test_unpublished_and_missing_are_404(self):
        draft = HealthArticle.objects.create(title="Draft", description="x")
        for pk in (draft.pk, 999999):
            with self.subTest(pk=pk):
                response = self.client.get(
                    reverse("owner_health_article_detail", args=[pk])
                )
                self.assertEqual(response.status_code, 404)

    def test_the_back_link_goes_to_the_list_and_keeps_the_search(self):
        listing = reverse("owner_health_article_list")
        html = self.client.get(self.url).content.decode()
        self.assertRegex(
            html, rf'(?s)<a href="{listing}"[^>]*>.*?Back to Health library</a>'
        )
        html = self.client.get(self.url, {"q": "parvo virus"}).content.decode()
        self.assertIn(f'<a href="{listing}?q=parvo+virus"', html)

    def test_every_link_is_on_one_line(self):
        for name, source in _sources(self.PARTS):
            for link in re.findall(r"(?s)<a\b.*?</a>", source):
                with self.subTest(file=name, link=link[:50]):
                    self.assertNotIn("\n", link)
                    self.assertNotIn("|safe", source)
                    self.assertNotIn("<!--", source)

    def test_no_inline_script_or_handler(self):
        main = _main(self.client.get(self.url))
        self.assertNotIn("<script", main)
        self.assertNotIn("onclick", main)

    def test_query_total_is_flat_for_a_short_and_a_long_article(self):
        long_text = "More text. " * 500
        for text in ("Short.", long_text):
            self.article_.description = text
            self.article_.causes = text
            self.article_.general_care = text
            self.article_.save()
            with self.subTest(size=len(text)):
                with self.assertNumQueries(self.PAGE_QUERIES):
                    self.client.get(self.url)
