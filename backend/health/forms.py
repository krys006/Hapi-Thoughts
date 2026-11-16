from django import forms

from .models import HealthArticle


class HealthArticleForm(forms.ModelForm):
    """
    Admin form for creating and editing health articles.
    Shared between create and edit views.
    """

    class Meta:
        model = HealthArticle
        fields = [
            "title",
            "description",
            "causes",
            "symptoms",
            "general_care",
        ]
        labels = {
            "title": "Condition name",
            "description": "Description",
            "causes": "Causes",
            "symptoms": "Symptoms",
            "general_care": "General care",
        }
        help_texts = {
            "title": "This is the title pet owners see.",
            "description": "One or two sentences. Shown on the list.",
        }
        error_messages = {
            "title": {"required": "Type the condition name."},
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "causes": forms.Textarea(attrs={"rows": 3}),
            "symptoms": forms.Textarea(attrs={"rows": 3}),
            "general_care": forms.Textarea(attrs={"rows": 4}),
        }
